#
# @author: Allan
#

import torch
import torch.nn as nn
import numpy as np

from src.model.module.bilstm_encoder import BiLSTMEncoder
from src.model.module.linear_crf_inferencer import LinearCRF
from src.model.module.linear_encoder import LinearEncoder
from src.model.embedder import TransformersEmbedder
from src.model.module.deplabel_gcn import DepLabeledGCN
from src.model.module.classifier import MultiNonLinearClassifier, SingleLinearClassifier
from typing import Tuple, Union
from src.config.config import DepModelType, PaserModeType
from torch.nn import CrossEntropyLoss, functional
# from allennlp.modules.span_extractors import EndpointSpanExtractor
from src.model.module.spanextractor import EndpointSpanExtractor, SelfAttentiveSpanExtractor
from src.data.data_utils import START_TAG, STOP_TAG, PAD, head_to_adj, head_to_adj_label


class TransformersCRF(nn.Module):


    def __init__(self, config):
        super(TransformersCRF, self).__init__()
        self.transformer = TransformersEmbedder(transformer_model_name=config.embedder_type)
        if config.hidden_dim > 0:
            self.encoder = BiLSTMEncoder(label_size=config.label_size, input_dim=self.transformer.get_output_dim(),
                                         hidden_dim=config.hidden_dim, drop_lstm=config.dropout)
        else:
            if config.dep_model == DepModelType.dggcn:
                self.encoder = LinearEncoder(label_size=config.label_size, input_dim=config.dggcn_outputsize)
            else:
                self.encoder = LinearEncoder(label_size=config.label_size, input_dim=self.transformer.get_output_dim())
        self.dep_model = config.dep_model
        self.gcn = DepLabeledGCN(config, config.dggcn_outputsize, self.transformer.get_output_dim(),
                                     self.transformer.get_output_dim())  ### lstm hidden size
        self.label_size = config.label_size
        self.parser_mode = config.parser_mode
        if self.dep_model != DepModelType.none:
            self.root_dep_label_id = config.root_dep_label_id
        if self.parser_mode == PaserModeType.crf:
            self.inferencer = LinearCRF(label_size=config.label_size, label2idx=config.label2idx, add_iobes_constraint=config.add_iobes_constraint,
                                        idx2labels=config.idx2labels)
            # self.pad_idx = config.label2idx[PAD]
        else:
            # import span-length embedding
            self.max_span_width = config.max_entity_length # max span length
            self.tokenLen_emb_dim = 50 # the embedding dim of a span
            self.spanLen_emb_dim = 50 # the embedding dim of a span length
            self.span_combination_mode = 'x,y' # Train data in format defined by --data-io param
            #  bucket_widths: Whether to bucket the span widths into log-space buckets. If `False`, the raw span widths are used.
            if self.dep_model == DepModelType.dggcn:
                self._endpoint_span_extractor = EndpointSpanExtractor(config.dggcn_outputsize,
                                                                      combination=self.span_combination_mode,
                                                                      num_width_embeddings=self.max_span_width,
                                                                      span_width_embedding_dim=self.tokenLen_emb_dim,
                                                                      bucket_widths=True)
                self.attentive_span_extractor = SelfAttentiveSpanExtractor(config.dggcn_outputsize)
                # input_dim = config.dggcn_outputsize * 2 + self.tokenLen_emb_dim + self.spanLen_emb_dim
                input_dim = self._endpoint_span_extractor.get_output_dim() + self.attentive_span_extractor.get_output_dim()
            else:
                self._endpoint_span_extractor = EndpointSpanExtractor(self.transformer.get_output_dim(),
                                                                      combination=self.span_combination_mode,
                                                                      num_width_embeddings=self.max_span_width,
                                                                      span_width_embedding_dim=self.tokenLen_emb_dim,
                                                                      bucket_widths=True)
                self.attentive_span_extractor = SelfAttentiveSpanExtractor(self.transformer.get_output_dim())
                # input_dim = self.transformer.get_output_dim() * 2 + self.tokenLen_emb_dim + self.spanLen_emb_dim
                input_dim = self._endpoint_span_extractor.get_output_dim() + self.attentive_span_extractor.get_output_dim()
            # self.linear = nn.Linear(10, 1)
            # self.score_func = nn.Softmax(dim=-1)
            self.span_classifier = MultiNonLinearClassifier(input_dim, config.label_size, 0.2) # model_dropout = 0.2
            # self.spanLen_embedding = nn.Embedding(self.max_span_width + 1, self.spanLen_emb_dim, padding_idx=0)
            self.classifier = nn.Softmax(dim=-1)
            self.cross_entropy = nn.CrossEntropyLoss(reduction='none', ignore_index=-1)

    def forward(self, subword_input_ids: torch.Tensor,
                    word_seq_lens: torch.Tensor,
                    orig_to_tok_index: torch.Tensor,
                    attention_mask: torch.Tensor,
                    depheads: torch.Tensor,
                    deplabels: torch.Tensor,
                    all_span_lens: torch.Tensor,  all_span_ids: torch.Tensor,
                    all_span_weight:torch.Tensor, real_span_mask: torch.Tensor,
                    labels: torch.Tensor = None,
                    is_train: bool = True) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        Calculate the negative loglikelihood.
        :param subword_input_ids: (batch_size x max_seq_len)
        :param word_seq_lens: (batch_size) note: not subword
        :param context_emb: (batch_size x max_seq_len x context_emb_size)
        :param chars: (batch_size x max_seq_len x max_char_len)
        :param char_seq_lens: (batch_size x max_seq_len)
        :param labels: (batch_size x max_seq_len)
        :param is_train: whether to calculate the loss or not.
                        We use this for distributed training, so that we don't have to
                        add another function for `decode`
        :return: the total negative log-likelihood loss
        """
        bz, _ = subword_input_ids.size()
        max_seq_len = word_seq_lens.max()
        word_rep = self.transformer(subword_input_ids, orig_to_tok_index, attention_mask)
        if self.dep_model == DepModelType.dggcn:
            adj_matrixs = [head_to_adj(max_seq_len, orig_to_tok_index[i], depheads[i]) for i in range(bz)]
            adj_matrixs = np.stack(adj_matrixs, axis=0)
            adj_matrixs = torch.from_numpy(adj_matrixs)
            dep_label_adj = [head_to_adj_label(max_seq_len, orig_to_tok_index[i], depheads[i], deplabels[i], self.root_dep_label_id) for i
                             in range(bz)]
            dep_label_adj = torch.from_numpy(np.stack(dep_label_adj, axis=0)).long()
            feature_out = self.gcn(word_rep, word_seq_lens, adj_matrixs, dep_label_adj)
            if self.parser_mode == PaserModeType.crf:
                encoder_scores = self.encoder(feature_out, word_seq_lens)
                batch_size = word_rep.size(0)
                sent_len = word_rep.size(1)
                maskTemp = torch.arange(1, sent_len + 1, dtype=torch.long, device=word_rep.device).view(1,
                                                                                                        sent_len).expand(
                    batch_size, sent_len)
                mask = torch.le(maskTemp, word_seq_lens.view(batch_size, 1).expand(batch_size, sent_len))
                if is_train:
                    unlabed_score, labeled_score = self.inferencer(encoder_scores, word_seq_lens, labels, mask)
                    return unlabed_score - labeled_score
                else:
                    bestScores, decodeIdx = self.inferencer.decode(encoder_scores, word_seq_lens)
                    return decodeIdx
            else: # span and hidden states begin and end tcat
                all_span_rep = self._endpoint_span_extractor(feature_out, all_span_ids.long())  # [batch, n_span, hidden]
                att_span_emb = self.attentive_span_extractor(feature_out, all_span_ids.long())  # (batch_size, span_num, bert_dim)
                all_span_rep = torch.cat((all_span_rep, att_span_emb), dim=-1)
                # spanlen_rep = self.spanLen_embedding(all_span_lens)  # (bs, n_span, len_dim)
                # spanlen_rep = functional.relu(spanlen_rep)
                # all_span_rep = torch.cat((all_span_rep, spanlen_rep), dim=-1)
                all_span_rep = self.span_classifier(all_span_rep)  # (batch,n_span,n_class)
                if is_train:
                    _, n_span = labels.size()
                    all_span_rep = all_span_rep.view(-1, self.label_size)
                    span_label = labels.view(-1)
                    loss = self.cross_entropy(all_span_rep, span_label)
                    loss = loss.view(bz, n_span) * all_span_weight
                    loss = torch.masked_select(loss, real_span_mask.bool())
                    return torch.mean(loss)
                else:
                    predicts = self.classifier(all_span_rep)
                    return predicts
        else:
            if self.parser_mode == PaserModeType.crf:
                encoder_scores = self.encoder(word_rep, word_seq_lens)
                batch_size = word_rep.size(0)
                sent_len = word_rep.size(1)
                maskTemp = torch.arange(1, sent_len + 1, dtype=torch.long, device=word_rep.device).view(1,
                                                                                                        sent_len).expand(
                    batch_size, sent_len)
                mask = torch.le(maskTemp, word_seq_lens.view(batch_size, 1).expand(batch_size, sent_len))
                if is_train:
                    unlabed_score, labeled_score = self.inferencer(encoder_scores, word_seq_lens, labels, mask)
                    return unlabed_score - labeled_score
                else:
                    bestScores, decodeIdx = self.inferencer.decode(encoder_scores, word_seq_lens)
                    return decodeIdx
                    # return bestScores, decodeIdx
            else:
                all_span_rep = self._endpoint_span_extractor(word_rep, all_span_ids.long())  # [batch, n_span, hidden]
                att_span_emb = self.attentive_span_extractor(word_rep, all_span_ids.long())  # (batch_size, span_num, bert_dim)
                all_span_rep = torch.cat((all_span_rep, att_span_emb), dim=-1)
                # spanlen_rep = self.spanLen_embedding(all_span_lens)  # (bs, n_span, len_dim)
                # spanlen_rep = functional.relu(spanlen_rep)
                # all_span_rep = torch.cat((all_span_rep, spanlen_rep), dim=-1)
                all_span_rep = self.span_classifier(all_span_rep)
                if is_train:
                    _, n_span = labels.size()
                    all_span_rep = all_span_rep.view(-1, self.label_size)
                    span_label_ltoken = labels.view(-1)
                    loss = self.cross_entropy(all_span_rep, span_label_ltoken)
                    loss = loss.view(bz, n_span) * all_span_weight
                    loss = torch.masked_select(loss, real_span_mask.bool())
                    return torch.mean(loss)
                else:
                    predicts = self.classifier(all_span_rep)
                    return predicts
