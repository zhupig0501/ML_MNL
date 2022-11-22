import math
import pandas as pd
import torch
from torch import nn
from d2l import torch as d2l
from torch.optim.lr_scheduler import _LRScheduler
import math
import numpy as np
import os
from torch.autograd import Variable


def masked_softmax(x, valid_lens):
    """softmax operations are performed by masking the element on the last axis"""
    # x:3D张量，valid_lens:1D或2D张量
    if valid_lens is None:
        return nn.functional.softmax(x, dim=-1)
    else:
        shape = x.shape
        if valid_lens.dim() == 1:
            valid_lens = torch.repeat_interleave(valid_lens, shape[1])
        else:
            valid_lens = valid_lens.reshape(-1)
        # 最后一轴上被掩蔽的元素使用一个非常大的负值替换，从而其softmax输出为0
        x = sequence_mask(x.reshape(-1, shape[-1]), valid_lens,
                              value=-1e6)
        return nn.functional.softmax(x.reshape(shape), dim=-1)


def transpose_qkv(x, num_heads):
    """Transform shape for parallel computation of multi-head attention"""
    # 输入x的形状:(batch_size，查询或者“键－值”对的个数，num_hiddens)
    # 输出x的形状:(batch_size，查询或者“键－值”对的个数，num_heads，
    # num_hiddens/num_heads)
    x = x.reshape(x.shape[0], x.shape[1], num_heads, -1)
#     print(x.shape)
    # 输出x的形状:(batch_size，num_heads，查询或者“键－值”对的个数,
    # num_hiddens/num_heads)
    x = x.permute(0, 2, 1, 3)

    # 最终输出的形状:(batch_size*num_heads,查询或者“键－值”对的个数,
    # num_hiddens/num_heads)
    x = x.reshape(-1, x.shape[2], x.shape[3])
#     print(x.shape)
    return x


#@save
def transpose_output(x, num_heads):
    """Reverses the operation of the transpose_qkv function"""
    x = x.reshape(-1, num_heads, x.shape[1], x.shape[2])
    x = x.permute(0, 2, 1, 3)
    return x.reshape(x.shape[0], x.shape[1], -1)


class DotProductAttention(nn.Module):
    """Scaling dot product attention"""
    def __init__(self, dropout, **kwargs):
        super(DotProductAttention, self).__init__(**kwargs)
        self.dropout = nn.Dropout(dropout)
        self.attention_weights = 0

    # queries的形状：(batch_size，查询的个数，d)
    # keys的形状：(batch_size，“键－值”对的个数，d)
    # values的形状：(batch_size，“键－值”对的个数，值的维度)
    # valid_lens的形状:(batch_size，)或者(batch_size，查询的个数)
    def forward(self, queries, keys, values, valid_lens=None):
        d = queries.shape[-1]
        # 设置transpose_b=True为了交换keys的最后两个维度
        scores = torch.bmm(queries, keys.transpose(1, 2)) / math.sqrt(d)
        self.attention_weights = masked_softmax(scores, valid_lens)
#         print(self.attention_weights[0][1])
#         print(valid_lens[0])
        return torch.bmm(self.dropout(self.attention_weights), values)


class PositionWiseFFN(nn.Module):
    """PositionWiseFFN"""
    def __init__(self, ffn_num_input, ffn_num_hiddens, ffn_num_outputs,
                 **kwargs):
        super(PositionWiseFFN, self).__init__(**kwargs)
        self.dense1 = nn.Linear(ffn_num_input, ffn_num_hiddens)
        self.relu = nn.ReLU()
        self.dense2 = nn.Linear(ffn_num_hiddens, ffn_num_outputs)

    def forward(self, x):
        return self.dense2(self.relu(self.dense1(x)))


class AddNorm(nn.Module):
    """Dropout + Layernorm"""
    def __init__(self, normalized_shape, dropout, **kwargs):
        super(AddNorm, self).__init__(**kwargs)
        self.dropout = nn.Dropout(dropout)
        self.ln = nn.LayerNorm(normalized_shape)

    def forward(self, x, y):
        return self.ln(self.dropout(y) + x)


class MultiHeadAttention(nn.Module):
    """multi-head attention"""
    def __init__(self, key_size, query_size, value_size, num_hiddens,
                 num_heads, dropout, bias=False, **kwargs):
        super(MultiHeadAttention, self).__init__(**kwargs)
        self.num_heads = num_heads
        self.attention = DotProductAttention(dropout)
        self.W_q = nn.Linear(query_size, num_hiddens, bias=bias)
        self.W_k = nn.Linear(key_size, num_hiddens, bias=bias)
        self.W_v = nn.Linear(value_size, num_hiddens, bias=bias)
        self.W_o = nn.Linear(num_hiddens, num_hiddens, bias=bias)

    def forward(self, queries, keys, values, valid_lens):
        # the shape of queries, keys, values:
        # (batch_size, num of query or "key-value" pairs, num_hiddens)
        # valid_lens shape:
        # (batch_size,) or (batch_size, num of queries)
        # The output size of queries, keys, and values is as follows:
        # (batch_size*num_heads, num of query or key-value pairs,
        # num_hiddens/num_heads)
        queries = transpose_qkv(self.W_q(queries), self.num_heads)
        keys = transpose_qkv(self.W_k(keys), self.num_heads)
        values = transpose_qkv(self.W_v(values), self.num_heads)

        if valid_lens is not None:
            valid_lens = torch.repeat_interleave(
                valid_lens, repeats=self.num_heads, dim=0)

        # output shape :(batch_size*num_heads, num of queries, num_hiddens/num_heads)
        output = self.attention(queries, keys, values, valid_lens)

        # Shape of output_concat :(batch_size, num of queries, num_hiddens)
        output_concat = transpose_output(output, self.num_heads)
        return self.W_o(output_concat)


class EncoderBlock(nn.Module):
    """single encoder of transformer"""
    def __init__(self, key_size, query_size, value_size, num_hiddens,
                 norm_shape, ffn_num_input, ffn_num_hiddens, num_heads,
                 dropout, use_bias=False, **kwargs):
        super(EncoderBlock, self).__init__(**kwargs)
        self.attention = MultiHeadAttention(
            key_size, query_size, value_size, num_hiddens, num_heads, dropout,
            use_bias)
        self.addnorm1 = AddNorm(norm_shape, dropout)
        self.ffn = PositionWiseFFN(
            ffn_num_input, ffn_num_hiddens, num_hiddens)
        self.addnorm2 = AddNorm(norm_shape, dropout)

    def forward(self, x, valid_lens):
        y = self.addnorm1(x, self.attention(x, x, x, valid_lens))
        return self.addnorm2(y, self.ffn(y))


class TransformerEncoder(d2l.Encoder):
    """transformer whole encoder part"""
    def __init__(self, key_size, query_size, value_size,
                 num_hiddens, norm_shape, ffn_num_input, ffn_num_hiddens,
                 num_heads, num_layers, dropout, feature_sizes, k, embedding_sizes, norm_shape_init, use_bias=False, **kwargs):
        super(TransformerEncoder, self).__init__(**kwargs)
        self.bn = nn.BatchNorm1d(norm_shape_init)
        self.num_hiddens = num_hiddens
        self.feature_sizes = feature_sizes
        self.embedding_sizes = embedding_sizes
        self.k = k
        self.embeddings = \
            nn.ModuleList([nn.Embedding(feature_sizes[i], embedding_sizes[i]) for i in range(len(feature_sizes))])
        self.blks = nn.Sequential()
        for i in range(num_layers):
            self.blks.add_module("block"+str(i),
                                 EncoderBlock(key_size, query_size, value_size, num_hiddens,
                                 norm_shape, ffn_num_input, ffn_num_hiddens,
                                 num_heads, dropout, use_bias))
        self.dense = nn.Linear(num_hiddens, 1)
        self.maskedsoftmax = masked_softmax

    def forward(self, x, *args):
        cat, conti, valid_lens = x
        batch_size, choice_size, cat_size = cat.shape
        _, _, conti_size = conti.shape
        cat_all = cat.reshape([-1, cat_size])
        conti = conti.reshape([-1, conti_size])
        x_conti = self.bn(conti)
        x_emb_array = [emb(cat_all[:, i]) for i, emb in enumerate(self.embeddings)]
        x_emb = torch.cat(x_emb_array, 1)
        x_all_array = [x_emb, x_conti]
        x_all = torch.cat(x_all_array, 1)
        embedding_size = sum(self.embedding_sizes) + conti_size
        x_all = x_all.reshape(batch_size, choice_size, embedding_size)
        x_all = x_all * math.sqrt(self.num_hiddens)
        for i, blk in enumerate(self.blks):
            x_all = blk(x_all, valid_lens)
        x_final = self.dense(x_all)
        x_final = x_final.permute(0, 2, 1)
        x_final = self.maskedsoftmax(x_final, valid_lens)
        x_final = x_final.permute(0, 2, 1)
        return x_final
        # x:[batch_size, choice_size, feature_size]


def sequence_mask(X, valid_len, value=0):
    """Mask irrelevant entries in sequences"""
    maxlen = X.size(1)
    mask = torch.arange((maxlen), dtype=torch.float32,
                        device=X.device)[None, :] < valid_len[:, None]
    X[~mask] = value
    return X


def validate_loss(pred, label, weight=None, pos_weight=None):
    # 处理正负样本不均衡问题
    if pos_weight is None:
        label_size = pred.size()[1]
        pos_weight = torch.ones(label_size)
    # 处理多标签不平衡问题
    if weight is None:
        label_size = pred.size()[1]
        weight = torch.ones(label_size)
    unweighted_loss = pos_weight[0] * label * torch.log(pred[:, :, 0]) + (1 - label) * torch.log(1-pred[:, :, 0])
    return unweighted_loss


class MaskedSoftmaxCELoss(nn.Module):
    """The softmax cross-entropy loss with masks."""
    def __init__(self, weight=None, **kwargs):
        super(MaskedSoftmaxCELoss, self).__init__(**kwargs)
        self.weight = weight
        self.validate_loss = validate_loss
        self.sig = nn.Sigmoid()
    # `pred` shape: (`batch_size`, `num_steps`, `vocab_size`)
    # `label` shape: (`batch_size`, `num_steps`)
    # `valid_len` shape: (`batch_size`,)

    def forward(self, pred, label, valid_len):
        pred = self.sig(pred)
        weights = torch.ones_like(label)
        weights = sequence_mask(weights, valid_len)
        unweighted_loss = self.validate_loss(pred, label, pos_weight=self.weight)
        weighted_loss = -sum((unweighted_loss * weights).mean(dim=1))
        return weighted_loss

class EarlyStopping:
    """Early stops the training if validation loss doesn't improve after a given patience."""
    def __init__(self, save_path, patience=7, verbose=False, delta=0):
        """
        Args:
            save_path : save_path
            patience (int): How long to wait after last time validation loss improved.
                            Default: 7
            verbose (bool): If True, prints a message for each validation loss improvement.
                            Default: False
            delta (float): Minimum change in the monitored quantity to qualify as an improvement.
                            Default: 0
        """
        self.save_path = save_path
        self.patience = patience
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_loss_min = np.Inf
        self.delta = delta

    def __call__(self, val_loss, model):

        score = -val_loss

        if self.best_score is None:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
        elif score < self.best_score + self.delta:
            self.counter += 1
            print(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
            self.counter = 0

    def save_checkpoint(self, val_loss, model):
        '''Saves model when validation loss decrease.'''
        if self.verbose:
            print(f'Validation loss decreased ({self.val_loss_min:.6f} --> {val_loss:.6f}).  Saving model ...')
        path = os.path.join(self.save_path, 'best_network.pth')
        torch.save(model.state_dict(), path)
        self.val_loss_min = val_loss
