# -*- coding: utf-8 -*-

# Copyright 2020 Patrick Lumban Tobing (Nagoya University)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

from __future__ import division

import logging
import sys
import time
import math

import torch
import torch.nn.functional as F
import torch.fft
from torch import nn
from torch.autograd import Function

from torch.distributions.one_hot_categorical import OneHotCategorical

from torch import linalg as LA

import numpy as np

CLIP_1E12 = -14.162084148244246758816564788835 #laplace var. = 2*scale^2; log(scale) = (log(var)-log(2))/2; for var. >= 1e-12

# from softmax limit on 32-dim & for exp, also acceptable for sigmoid/tanh, but in C implementation they are [-17,89]/[-10,10]
MIN_CLAMP = -103
MAX_CLAMP = 85


def initialize(m):
    """FUNCTION TO INITILIZE CONV WITH XAVIER

    Arg:
        m (torch.nn.Module): torch nn module instance
    """
    if isinstance(m, nn.ConvTranspose2d) or isinstance(m, nn.ConvTranspose1d):
        nn.init.constant_(m.weight, 1.0)
        if m.bias is not None:
            nn.init.constant_(m.bias, 0.0)
    else:
        for name, param in m.named_parameters():
            if 'weight' in name and len(param.shape) > 1:
                nn.init.xavier_uniform_(param)
            elif 'bias' in name:
                nn.init.constant_(param, 0.0)


def encode_mu_law(x, mu=1024):
    """FUNCTION TO PERFORM MU-LAW ENCODING

    Args:
        x (ndarray): audio signal with the range from -1 to 1
        mu (int): quantized level

    Return:
        (ndarray): quantized audio signal with the range from 0 to mu - 1
    """
    mu = mu - 1
    fx = np.sign(x) * np.log(1 + mu * np.abs(x)) / np.log(1 + mu)
    return np.floor((fx + 1) / 2 * mu + 0.5).astype(np.int64)


def encode_mu_law_torch(x, mu=1024):
    """FUNCTION TO PERFORM MU-LAW ENCODING

    Args:
        x (ndarray): audio signal with the range from -1 to 1
        mu (int): quantized level

    Return:
        (ndarray): quantized audio signal with the range from 0 to mu - 1
    """
    mu = mu - 1
    fx = torch.sign(x) * torch.log1p(mu * torch.abs(x)) / np.log(1 + mu) # log1p(x) = log_e(1+x)
    return torch.floor((fx + 1) / 2 * mu + 0.5).long()


def decode_mu_law(y, mu=1024):
    """FUNCTION TO PERFORM MU-LAW DECODING

    Args:
        x (ndarray): quantized audio signal with the range from 0 to mu - 1
        mu (int): quantized level

    Return:
        (ndarray): audio signal with the range from -1 to 1
    """
    #fx = 2 * y / (mu - 1.) - 1.
    mu = mu - 1
    fx = y / mu * 2 - 1
    x = np.sign(fx) / mu * ((1 + mu) ** np.abs(fx) - 1)
    return np.clip(x, a_min=-1, a_max=0.999969482421875)


def decode_mu_law_torch(y, mu=1024):
    """FUNCTION TO PERFORM MU-LAW DECODING

    Args:
        x (ndarray): quantized audio signal with the range from 0 to mu - 1
        mu (int): quantized level

    Return:
        (ndarray): audio signal with the range from -1 to 1
    """
    #fx = 2 * y / (mu - 1.) - 1.
    mu = mu - 1
    fx = y / mu * 2 - 1
    x = torch.sign(fx) / mu * ((1 + mu) ** torch.abs(fx) - 1)
    return torch.clamp(x, min=-1, max=0.999969482421875)


class ConvTranspose2d(nn.ConvTranspose2d):
    """Conv1d module with customized initialization."""

    def __init__(self, *args, **kwargs):
        """Initialize Conv1d module."""
        super(ConvTranspose2d, self).__init__(*args, **kwargs)

    def reset_parameters(self):
        """Reset parameters."""
        torch.nn.init.constant_(self.weight, 1.0)
        if self.bias is not None:
            torch.nn.init.constant_(self.bias, 0.0)


class UpSampling(nn.Module):
    """UPSAMPLING LAYER WITH DECONVOLUTION

    Arg:
        upsampling_factor (int): upsampling factor
    """

    def __init__(self, upsampling_factor, bias=True):
        super(UpSampling, self).__init__()
        self.upsampling_factor = upsampling_factor
        self.bias = bias
        self.conv = ConvTranspose2d(1, 1,
                                       kernel_size=(1, self.upsampling_factor),
                                       stride=(1, self.upsampling_factor),
                                       bias=self.bias)

    def forward(self, x):
        """Forward calculation

        Arg:
            x (Variable): float tensor variable with the shape  (B x C x T)

        Return:
            (Variable): float tensor variable with the shape (B x C x T')
                        where T' = T * upsampling_factor
        """
        return self.conv(x.unsqueeze(1)).squeeze(1)


class SkewedConv1d(nn.Module):
    """1D SKEWED CONVOLUTION"""

    def __init__(self, in_dim=39, kernel_size=7, right_size=1, seg_conv=False, nonlinear=False, pad_first=False):
        super(SkewedConv1d, self).__init__()
        self.in_dim = in_dim
        self.kernel_size = kernel_size
        self.right_size = right_size
        self.rec_field = self.kernel_size
        self.left_size = self.kernel_size - 1 - self.right_size
        self.pad_first = pad_first
        if self.right_size < self.left_size:
            self.padding = self.left_size
            self.skew_left = True
            self.padding_1 = self.padding-self.right_size
        else:
            self.padding = self.right_size
            self.skew_left = False
            self.padding_1 = self.padding-self.left_size
        self.seg_conv = seg_conv
        if not self.seg_conv:
            self.out_dim = 128
        if nonlinear:
            if not self.pad_first:
                module_list = [nn.Conv1d(self.in_dim, self.out_dim, self.kernel_size, padding=self.padding),\
                                nn.PReLU(out_chn)]
            else:
                module_list = [nn.Conv1d(self.in_dim, self.out_dim, self.kernel_size), nn.PReLU(out_chn)]
            self.conv = nn.Sequential(*module_list)
        else:
            if not self.seg_conv:
                if not self.pad_first:
                    self.conv = nn.Conv1d(self.in_dim, self.out_dim, self.kernel_size, padding=self.padding)
                else:
                    self.conv = nn.Conv1d(self.in_dim, self.out_dim, self.kernel_size)
            else:
                if not self.pad_first:
                    self.conv = nn.Conv1d(self.in_dim, self.in_dim*self.rec_field, self.kernel_size, padding=self.padding)
                else:
                    self.conv = nn.Conv1d(self.in_dim, self.in_dim*self.rec_field, self.kernel_size)

    def forward(self, x):
        """Forward calculation

        Arg:
            x (Variable): float tensor variable with the shape  (B x C x T)

        Return:
            (Variable): float tensor variable with the shape (B x C x T)
        """

        if not self.pad_first:
            if self.padding_1 > 0:
                if self.skew_left:
                    return self.conv(x)[:,:,:-self.padding_1]
                else:
                    return self.conv(x)[:,:,self.padding_1:]
            else:
                return self.conv(x)
        else:
            return self.conv(x)


class TwoSidedDilConv1d(nn.Module):
    """1D TWO-SIDED DILATED CONVOLUTION"""

    def __init__(self, in_dim=39, kernel_size=3, layers=2, seg_conv=False, nonlinear=False, pad_first=False):
        super(TwoSidedDilConv1d, self).__init__()
        self.in_dim = in_dim
        self.kernel_size = kernel_size
        self.layers = layers
        self.rec_field = self.kernel_size**self.layers
        self.padding = int((self.rec_field-1)/2)
        self.pad_first = pad_first
        self.seg_conv = seg_conv
        module_list = []
        if not self.seg_conv:
            self.out_dim = 128
        if nonlinear:
            for i in range(self.layers):
                if i > 0:
                    in_chn = self.in_dim*(self.kernel_size**(i))
                    out_chn = self.in_dim*(self.kernel_size**(i+1))
                    module_list += [nn.Conv1d(in_chn, out_chn, self.kernel_size, dilation=self.kernel_size**i), \
                                    nn.PReLU(out_chn)]
                else:
                    out_chn = self.in_dim*(self.kernel_size**(i+1))
                    if not self.pad_first:
                        module_list += [nn.Conv1d(self.in_dim, out_chn, self.kernel_size, padding=self.padding),\
                                        nn.PReLU(out_chn)]
                    else:
                        module_list += [nn.Conv1d(self.in_dim, out_chn, self.kernel_size), nn.PReLU(out_chn)]
        else:
            if not self.seg_conv:
                for i in range(self.layers):
                    if i > 0:
                        module_list += [nn.Conv1d(self.in_dim,
                                        self.out_dim, self.kernel_size, \
                                            dilation=self.kernel_size**i)]
                    else:
                        if not self.pad_first:
                            module_list += [nn.Conv1d(self.in_dim, self.out_dim, \
                                            self.kernel_size, padding=self.padding)]
                        else:
                            module_list += [nn.Conv1d(self.in_dim, self.out_dim, self.kernel_size)]
            else:
                for i in range(self.layers):
                    if i > 0:
                        module_list += [nn.Conv1d(self.in_dim*(self.kernel_size**(i)), \
                                        self.in_dim*(self.kernel_size**(i+1)), self.kernel_size, \
                                            dilation=self.kernel_size**i)]
                    else:
                        if not self.pad_first:
                            module_list += [nn.Conv1d(self.in_dim, self.in_dim*(self.kernel_size**(i+1)), \
                                            self.kernel_size, padding=self.padding)]
                        else:
                            module_list += [nn.Conv1d(self.in_dim, self.in_dim*(self.kernel_size**(i+1)), self.kernel_size)]
        self.conv = nn.Sequential(*module_list)

    def forward(self, x):
        """Forward calculation

        Arg:
            x (Variable): float tensor variable with the shape  (B x C x T)

        Return:
            (Variable): float tensor variable with the shape (B x C x T)
        """

        return self.conv(x)


class CausalDilConv1d(nn.Module):
    """1D Causal DILATED CONVOLUTION"""

    def __init__(self, in_dim=11, kernel_size=2, layers=2, seg_conv=False, nonlinear=False, pad_first=False):
        super(CausalDilConv1d, self).__init__()
        self.in_dim = in_dim
        self.kernel_size = kernel_size
        self.layers = layers
        self.padding_list = [self.kernel_size**(i+1)-self.kernel_size**(i) for i in range(self.layers)]
        self.padding = sum(self.padding_list)
        self.rec_field = self.padding + 1
        self.pad_first = pad_first
        self.seg_conv = seg_conv
        if not self.seg_conv:
            self.out_dim = 128
        module_list = []
        if nonlinear:
            for i in range(self.layers):
                if i > 0:
                    in_chn = self.in_dim*(sum(self.padding_list[:i])+1)
                    out_chn = self.in_dim*(sum(self.padding_list[:i+1])+1)
                    module_list += [nn.Conv1d(in_chn, out_chn, self.kernel_size, dilation=self.kernel_size**i), \
                                    nn.PReLU(out_chn)]
                else:
                    out_chn = self.in_dim*(sum(self.padding_list[:i+1])+1)
                    if not self.pad_first:
                        module_list += [nn.Conv1d(self.in_dim, out_chn, self.kernel_size, padding=self.padding), \
                                        nn.PReLU(out_chn)]
                    else:
                        module_list += [nn.Conv1d(self.in_dim, out_chn, self.kernel_size), nn.PReLU(out_chn)]
        else:
            if not self.seg_conv:
                for i in range(self.layers):
                    if i > 0:
                        module_list += [nn.Conv1d(self.in_dim,
                                        self.out_dim, self.kernel_size, \
                                            dilation=self.kernel_size**i)]
                    else:
                        if not self.pad_first:
                            module_list += [nn.Conv1d(self.in_dim, self.out_din,
                                            self.kernel_size, padding=self.padding)]
                        else:
                            module_list += [nn.Conv1d(self.in_dim, self.out_dim,
                                            self.kernel_size)]
            else:
                for i in range(self.layers):
                    if i > 0:
                        module_list += [nn.Conv1d(self.in_dim*(sum(self.padding_list[:i])+1), \
                                        self.in_dim*(sum(self.padding_list[:i+1])+1), self.kernel_size, \
                                            dilation=self.kernel_size**i)]
                    else:
                        if not self.pad_first:
                            module_list += [nn.Conv1d(self.in_dim, self.in_dim*(sum(self.padding_list[:i+1])+1), \
                                            self.kernel_size, padding=self.padding)]
                        else:
                            module_list += [nn.Conv1d(self.in_dim, self.in_dim*(sum(self.padding_list[:i+1])+1), \
                                            self.kernel_size)]
        self.conv = nn.Sequential(*module_list)

    def forward(self, x):
        """Forward calculation

        Arg:
            x (Variable): float tensor variable with the shape  (B x C x T)

        Return:
            (Variable): float tensor variable with the shape (B x C x T)
        """

        if not self.pad_first:
            return self.conv(x)[:,:,:-self.padding]
        else:
            return self.conv(x)


class DualFC_(nn.Module):
    """Compact Dual Fully Connected layers based on LPCNet"""

    def __init__(self, in_dim=32, out_dim=32, lpc=6, bias=True, n_bands=5, mid_out=32):
        super(DualFC_, self).__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.lpc = lpc
        self.n_bands = n_bands
        self.lpc2 = self.lpc*2
        self.bias = bias
        self.lpc4bands = self.lpc2*2*self.n_bands
        self.mid_out = mid_out

        self.mid_out_bands = self.mid_out*self.n_bands
        self.mid_out_bands2 = self.mid_out_bands*2
        self.conv = nn.Conv1d(self.in_dim, self.mid_out_bands2+self.lpc4bands, 1, bias=self.bias)
        self.fact = EmbeddingZero(1, self.mid_out_bands2+self.lpc4bands)
        self.out = nn.Conv1d(self.lpc2+self.mid_out, self.lpc2+self.out_dim, 1, bias=self.bias)

    def forward(self, x):
        """Forward calculation

        Arg:
            x (Variable): float tensor variable with the shape  (B x C_in x T)

        Return:
            (Variable): float tensor variable with the shape (B x T x C_out)
        """

        # out = fact_1 o tanh(conv_1 * x) + fact_2 o tanh(conv_2 * x)
        if self.n_bands > 1:
            if self.lpc > 0:
                conv_out = F.relu(self.conv(x)).transpose(1,2) # B x T x n_bands*(K*2+K*2+mid_dim*2)
                fact_weight = 0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)) # K*2+K*2+mid_dim*2
                B = x.shape[0]
                T = x.shape[2]
                # B x T x n_bands x (K+K+mid_dim)*2 --> B x (K+K+mid_dim) x (T x n_bands) --> B x T x n_bands x (K+K+out_dim)
                out = torch.clamp(self.out(torch.sum((conv_out*fact_weight).reshape(B,T,self.n_bands,2,-1), 3).reshape(B,T*self.n_bands,-1).transpose(1,2)),
                                        min=MIN_CLAMP, max=MAX_CLAMP).transpose(1,2).reshape(B,T,self.n_bands,-1)
                return torch.tanh(out[:,:,:,:self.lpc]), torch.exp(out[:,:,:,self.lpc:-self.out_dim]), F.tanhshrink(out[:,:,:,-self.out_dim:])
                # lpc_signs, lpc_mags, logits
            else:
                # B x T x n_bands x mid*2 --> B x (T x n_bands) x mid --> B x mid x (T x n_bands) --> B x T x n_bands x out_dim
                B = x.shape[0]
                T = x.shape[2]
                return F.tanhshrink(torch.clamp(self.out(torch.sum((F.relu(self.conv(x).transpose(1,2))
                            *(0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)))).reshape(B,T,self.n_bands,2,-1), 3).reshape(B,T*self.n_bands,-1).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2).reshape(B,T,self.n_bands,-1)
                # logits
        else:
            if self.lpc > 0:
                conv = F.relu(self.conv(x)).transpose(1,2)
                fact_weight = 0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP))
                # B x T x (K+K+mid_dim)*2 --> B x (K+K+mid_dim) x T --> B x T x (K+K+out_dim)
                out = torch.clamp(self.out(torch.sum((out*fact_weight).reshape(x.shape[0],x.shape[2],2,-1), 2).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP).transpose(1,2)
                return torch.tanh(out[:,:,:self.lpc]), torch.exp(out[:,:,self.lpc:-self.out_dim]), F.tanshrink(out[:,:,-self.out_dim:])
                # lpc_signs, lpc_mags, logits
            else:
                # B x T x mid*2 --> B x T x mid --> B x mid x T --> B x T x out_dim
                return F.tanhshrink(torch.clamp(self.out(torch.sum((F.relu(self.conv(x).transpose(1,2))*(0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)))).reshape(x.shape[0],x.shape[2],2,-1), 2).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2)
                # logits


class DualFC(nn.Module):
    """Compact Dual Fully Connected layers based on LPCNet"""

    def __init__(self, in_dim=32, out_dim=512, lpc=6, bias=True, n_bands=5, mid_out=32, lin_flag=False):
        super(DualFC, self).__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.lpc = lpc
        self.n_bands = n_bands
        self.lpc_out_dim = self.lpc+self.out_dim
        self.lpc_out_dim_lpc = self.lpc_out_dim+self.lpc
        self.lpc2 = self.lpc*2
        self.out_dim2 = self.out_dim*2
        self.bias = bias
        self.lpc4 = self.lpc2*2
        self.lpc2bands = self.lpc2*self.n_bands
        self.lpc4bands = self.lpc4*self.n_bands
        self.mid_out = mid_out
        self.lin_flag = lin_flag
        if self.lin_flag:
            self.lpc6 = self.lpc2*3
            self.lpc6bands = self.lpc6*self.n_bands

        if self.mid_out is not None:
            self.mid_out_bands = self.mid_out*self.n_bands
            self.mid_out_bands2 = self.mid_out_bands*2
            if self.lin_flag:
                self.conv = nn.Conv1d(self.in_dim, self.mid_out_bands2+self.lpc6bands, 1, bias=self.bias)
                self.fact = EmbeddingZero(1, self.mid_out_bands2+self.lpc6bands)
            else:
                self.conv = nn.Conv1d(self.in_dim, self.mid_out_bands2+self.lpc4bands, 1, bias=self.bias)
                self.fact = EmbeddingZero(1, self.mid_out_bands2+self.lpc4bands)
            self.out = nn.Conv1d(self.mid_out, self.out_dim, 1, bias=self.bias)
        else:
            if self.lin_flag:
                self.conv = nn.Conv1d(self.in_dim, self.out_dim2*self.n_bands+self.lpc6bands, 1, bias=self.bias)
                self.fact = EmbeddingZero(1, self.out_dim2+self.lpc6)
            else:
                self.conv = nn.Conv1d(self.in_dim, self.out_dim2*self.n_bands+self.lpc4bands, 1, bias=self.bias)
                self.fact = EmbeddingZero(1, self.out_dim2+self.lpc4)

    def forward(self, x):
        """Forward calculation

        Arg:
            x (Variable): float tensor variable with the shape  (B x C_in x T)

        Return:
            (Variable): float tensor variable with the shape (B x T x C_out)
        """

        # out = fact_1 o tanh(conv_1 * x) + fact_2 o tanh(conv_2 * x)
        if self.n_bands > 1:
            if self.mid_out is not None:
                if self.lpc > 0:
                    conv = self.conv(x).transpose(1,2) # B x T x n_bands*(K*4+mid_dim*2)
                    fact_weight = 0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)) # K*4+256*2
                    B = x.shape[0]
                    T = x.shape[2]
                    # B x T x n_bands x K*2 --> B x T x n_bands x K
                    if self.lin_flag:
                        return torch.sum((torch.tanh(torch.clamp(conv[:,:,:self.lpc2bands], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[:self.lpc2bands]).reshape(B,T,self.n_bands,2,-1), 3), \
                                torch.sum((torch.exp(torch.clamp(conv[:,:,self.lpc2bands:self.lpc4bands], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[self.lpc2bands:self.lpc4bands]).reshape(B,T,self.n_bands,2,-1), 3), \
                                torch.sum((conv[:,:,self.lpc4bands:self.lpc6bands]*fact_weight[self.lpc4bands:self.lpc6bands]).reshape(B,T,self.n_bands,2,-1), 3), \
                                    F.tanhshrink(torch.clamp(self.out(torch.sum((F.relu(conv[:,:,self.lpc6bands:])
                                        *fact_weight[self.lpc6bands:]).reshape(B,T,self.n_bands,2,-1), 3).reshape(B,T*self.n_bands,-1).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2).reshape(B,T,self.n_bands,-1)
                    else:
                        return torch.sum((torch.tanh(torch.clamp(conv[:,:,:self.lpc2bands], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[:self.lpc2bands]).reshape(B,T,self.n_bands,2,-1), 3), \
                                torch.sum((torch.exp(torch.clamp(conv[:,:,self.lpc2bands:self.lpc4bands], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[self.lpc2bands:self.lpc4bands]).reshape(B,T,self.n_bands,2,-1), 3), \
                                    F.tanhshrink(torch.clamp(self.out(torch.sum((F.relu(conv[:,:,self.lpc4bands:])
                                        *fact_weight[self.lpc4bands:]).reshape(B,T,self.n_bands,2,-1), 3).reshape(B,T*self.n_bands,-1).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2).reshape(B,T,self.n_bands,-1)
                    # B x T x n_bands x mid*2 --> B x (T x n_bands) x mid --> B x mid x (T x n_bands) --> B x T x n_bands x 32
                else:
                    # B x T x n_bands x mid*2 --> B x (T x n_bands) x mid --> B x mid x (T x n_bands) --> B x T x n_bands x 32
                    B = x.shape[0]
                    T = x.shape[2]
                    return F.tanhshrink(torch.clamp(self.out(torch.sum((F.relu(self.conv(x).transpose(1,2))
                                *(0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)))).reshape(B,T,self.n_bands,2,-1), 3).reshape(B,T*self.n_bands,-1).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2).reshape(B,T,self.n_bands,-1)
            else:
                if self.lpc > 0:
                    conv = self.conv(x).transpose(1,2) # B x T x n_bands*(K*4+256*2)
                    fact_weight = 0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)) # K*4+256*2
                    B = x.shape[0]
                    T = x.shape[2]
                    # B x T x n_bands x K*2 --> B x T x n_bands x K
                    if self.lin_flag:
                        return torch.sum((torch.tanh(torch.clamp(conv[:,:,:self.lpc2bands], min=MIN_CLAMP, max=MAX_CLAMP)).reshape(B,T,self.n_bands,-1)*fact_weight[:self.lpc2]).reshape(B,T,self.n_bands,2,-1), 3), \
                                torch.sum((torch.exp(torch.clamp(conv[:,:,self.lpc2bands:self.lpc4bands], min=MIN_CLAMP,max=MAX_CLAMP)).reshape(B,T,self.n_bands,-1)*fact_weight[self.lpc2:self.lpc4]).reshape(B,T,self.n_bands,2,-1), 3), \
                                torch.sum((conv[:,:,self.lpc4bands:self.lpc6bands].reshape(B,T,self.n_bands,-1)*fact_weight[self.lpc4:self.lpc6]).reshape(B,T,self.n_bands,2,-1), 3), \
                                    torch.sum((F.tanhshrink(torch.clamp(conv[:,:,self.lpc6bands:], min=MIN_CLAMP, max=MAX_CLAMP)).reshape(B,T,self.n_bands,-1)*fact_weight[self.lpc6:]).reshape(B,T,self.n_bands,2,-1), 3)
                    else:
                        return torch.sum((torch.tanh(torch.clamp(conv[:,:,:self.lpc2bands], min=MIN_CLAMP, max=MAX_CLAMP)).reshape(B,T,self.n_bands,-1)*fact_weight[:self.lpc2]).reshape(B,T,self.n_bands,2,-1), 3), \
                                torch.sum((torch.exp(torch.clamp(conv[:,:,self.lpc2bands:self.lpc4bands], min=MIN_CLAMP,max=MAX_CLAMP)).reshape(B,T,self.n_bands,-1)*fact_weight[self.lpc2:self.lpc4]).reshape(B,T,self.n_bands,2,-1), 3), \
                                    torch.sum((F.tanhshrink(torch.clamp(conv[:,:,self.lpc4bands:], min=MIN_CLAMP, max=MAX_CLAMP)).reshape(B,T,self.n_bands,-1)*fact_weight[self.lpc4:]).reshape(B,T,self.n_bands,2,-1), 3)
                    # B x T x n_bands x 32*2 --> B x T x n_bands x 32
                else:
                    # B x T x n_bands x 32*2 --> B x T x n_bands x 32
                    B = x.shape[0]
                    T = x.shape[2]
                    return torch.sum((F.tanhshrink(torch.clamp(self.conv(x).transpose(1,2), min=MIN_CLAMP, max=MAX_CLAMP)).reshape(B,T,self.n_bands,-1)*(0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)))).reshape(B,T,self.n_bands,2,-1), 3)
        else:
            if self.mid_out is not None:
                if self.lpc > 0:
                    conv = self.conv(x).transpose(1,2)
                    fact_weight = 0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP))
                    if self.lin_flag:
                        return torch.sum((torch.tanh(torch.clamp(conv[:,:,:self.lpc2], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[:self.lpc2]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                torch.sum((torch.exp(torch.clamp(conv[:,:,self.lpc2:self.lpc4], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[self.lpc2:self.lpc4]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                torch.sum((conv[:,:,self.lpc4:self.lpc6]*fact_weight[self.lpc4:self.lpc6]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                F.tanhshrink(torch.clamp(self.out(torch.sum((F.relu(conv[:,:,self.lpc6:])*fact_weight[self.lpc6:]).reshape(x.shape[0],x.shape[2],2,-1), 2).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2)
                    else:
                        return torch.sum((torch.tanh(torch.clamp(conv[:,:,:self.lpc2], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[:self.lpc2]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                torch.sum((torch.exp(torch.clamp(conv[:,:,self.lpc2:self.lpc4], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[self.lpc2:self.lpc4]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                F.tanhshrink(torch.clamp(self.out(torch.sum((F.relu(conv[:,:,self.lpc4:])*fact_weight[self.lpc4:]).reshape(x.shape[0],x.shape[2],2,-1), 2).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2)
                else:
                    return F.tanhshrink(torch.clamp(self.out(torch.sum((F.relu(self.conv(x).transpose(1,2))*(0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)))).reshape(x.shape[0],x.shape[2],2,-1), 2).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2)
            else:
                if self.lpc > 0:
                    conv = self.conv(x).transpose(1,2)
                    fact_weight = 0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP))
                    if self.lin_flag:
                        return torch.sum((torch.tanh(torch.clamp(conv[:,:,:self.lpc2], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[:self.lpc2]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                torch.sum((torch.exp(torch.clamp(conv[:,:,self.lpc2:self.lpc4], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[self.lpc2:self.lpc4]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                torch.sum((conv[:,:,self.lpc4:self.lpc6]*fact_weight[self.lpc4:self.lpc6]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                    torch.sum((F.tanhshrink(torch.clamp(conv[:,:,self.lpc6:], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[self.lpc6:]).reshape(x.shape[0],x.shape[2],2,-1), 2)
                    else:
                        return torch.sum((torch.tanh(torch.clamp(conv[:,:,:self.lpc2], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[:self.lpc2]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                torch.sum((torch.exp(torch.clamp(conv[:,:,self.lpc2:self.lpc4], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[self.lpc2:self.lpc4]).reshape(x.shape[0],x.shape[2],2,-1), 2), \
                                    torch.sum((F.tanhshrink(torch.clamp(conv[:,:,self.lpc4:], min=MIN_CLAMP, max=MAX_CLAMP))*fact_weight[self.lpc4:]).reshape(x.shape[0],x.shape[2],2,-1), 2)
                else:
                    return torch.sum((F.tanhshrink(torch.clamp(self.conv(x).transpose(1,2), min=MIN_CLAMP, max=MAX_CLAMP))*(0.5*torch.exp(torch.clamp(self.fact.weight[0], min=MIN_CLAMP, max=MAX_CLAMP)))).reshape(x.shape[0],x.shape[2],2,-1), 2)


class EmbeddingZero(nn.Embedding):
    """Conv1d module with customized initialization."""

    def __init__(self, *args, **kwargs):
        """Initialize Conv1d module."""
        super(EmbeddingZero, self).__init__(*args, **kwargs)

    def reset_parameters(self):
        """Reset parameters."""
        torch.nn.init.constant_(self.weight, 0)


class EmbeddingOne(nn.Embedding):
    """Conv1d module with customized initialization."""

    def __init__(self, *args, **kwargs):
        """Initialize Conv1d module."""
        super(EmbeddingOne, self).__init__(*args, **kwargs)

    def reset_parameters(self):
        """Reset parameters."""
        torch.nn.init.constant_(self.weight, 1)


class EmbeddingHalf(nn.Embedding):
    """Conv1d module with customized initialization."""

    def __init__(self, *args, **kwargs):
        """Initialize Conv1d module."""
        super(EmbeddingHalf, self).__init__(*args, **kwargs)

    def reset_parameters(self):
        """Reset parameters."""
        torch.nn.init.constant_(self.weight, 0.5)


def nn_search(encoding, centroids):
    T = encoding.shape[0]
    K = centroids.shape[0]
    dist2 = torch.sum((encoding.unsqueeze(1).repeat(1,K,1)-centroids.unsqueeze(0).repeat(T,1,1)).abs(),2) # TxK
    ctr_ids = torch.argmin(dist2, dim=-1)

    return ctr_ids


def nn_search_batch(encoding, centroids):
    B = encoding.shape[0]
    T = encoding.shape[1]
    K = centroids.shape[0]
    dist2 = torch.sum((encoding.unsqueeze(2).repeat(1,1,K,1)-\
                    centroids.unsqueeze(0).unsqueeze(0).repeat(B,T,1,1)).abs(),3) # B x T x K
    ctr_ids = torch.argmin(dist2, dim=-1) # B x T

    return ctr_ids


def cross_entropy_with_logits(logits, probs):
    logsumexp = torch.log(torch.sum(torch.exp(logits), -1, keepdim=True)) # B x T x K --> B x T x 1

    return torch.sum(-probs * (logits - logsumexp), -1) # B x T x K --> B x T


def kl_categorical_categorical_logits(p, logits_p, logits_q):
    """ sum_{k=1}^K q_k * (ln q_k - ln p_k) """

    return -cross_entropy_with_logits(logits_p, p) + cross_entropy_with_logits(logits_q, p) # B x T x K --> B x T


def sampling_laplace_wave(loc, scale):
    #eps = torch.empty_like(loc).uniform_(torch.finfo(loc.dtype).eps-1,1)
    small_zero = torch.finfo(loc.dtype).eps
    eps = torch.empty_like(loc).uniform_(small_zero-1,1-small_zero)

    return loc - scale * eps.sign() * torch.log1p(-eps.abs()) # scale

 
def sampling_normal(mu, var):
    eps = torch.randn(mu.shape).cuda()

    return mu + torch.sqrt(var) * eps # var


def kl_normal(mu_q, var_q):
    """ 1/2 [µ_i^2 + σ^2_i − 1 - ln(σ^2_i) ] """

    var_q = torch.clamp(var_q, min=1e-9)

    return torch.mean(torch.sum(0.5*(torch.pow(mu_q, 2) + var_q - 1 - torch.log(var_q)), -1)) # B x T x C --> B x T --> 1


def kl_normal_normal(mu_q, var_q, p):
    """ 1/2*σ^2_j [(µ_i − µ_j)^2 + σ^2_i − σ^2_j] + ln σ_j/σ_i """

    var_q = torch.clamp(var_q, min=1e-9)

    mu_p = p[:mu_q.shape[-1]]
    var_p = p[mu_q.shape[-1]:]
    var_p = torch.clamp(var_p, min=1e-9)

    return torch.mean(torch.sum(0.5*(torch.pow(mu_q-mu_p, 2)/var_p + var_q/var_p - 1 + torch.log(var_p/var_q)), -1)) # B x T x C --> B x T --> 1


def neg_entropy_laplace(log_b):
    #-ln(2be) = -((ln(2)+1) + ln(b))
    return -(1.69314718055994530941723212145818 + log_b)


def sum_gauss_dist(param_x, param_y):
    dim = param_x.shape[-1]

    if len(param_x.shape) > 2:
        mean_z = param_x[:,:,:dim] + param_y[:,:,:dim]
        var_z = param_x[:,:,dim:] + param_y[:,:,dim:]
    else:
        mean_z = param_x[:,dim:] + param_y[:,dim:]
        var_z = param_x[:,dim:] + param_y[:,dim:]

    return torch.cat((mean_z, var_z), -1)


def kl_laplace(param):
    """ - ln(λ_i) + |θ_i| + λ_i * exp(−|θ_i|/λ_i) − 1 """

    k = param.shape[-1]//2
    if len(param.shape) > 2:
        mu_q = param[:,:,:k]
        scale_q = torch.exp(param[:,:,k:])
    else:
        mu_q = param[:,:k]
        scale_q = torch.exp(param[:,k:])

    scale_q = torch.clamp(scale_q, min=1e-12)

    mu_q_abs = torch.abs(mu_q)

    return -torch.log(scale_q) + mu_q_abs + scale_q*torch.exp(-mu_q_abs/scale_q) - 1 # B x T x C / T x C


def kl_laplace_param(mu_q, sigma_q):
    """ - ln(λ_i) + |θ_i| + λ_i * exp(−|θ_i|/λ_i) − 1 """

    scale_q = torch.clamp(sigma_q.exp(), min=1e-12)
    mu_q_abs = torch.abs(mu_q)

    return torch.mean(torch.sum(-torch.log(scale_q) + mu_q_abs + scale_q*torch.exp(-mu_q_abs/scale_q) - 1, -1), -1) # B / 1


def sampling_gauss(mu, var, temp=None):
    #return mu + torch.log(var)*torch.randn_like(mu)
    if temp is not None:
        return mu + temp*torch.sqrt(var)*torch.randn_like(mu)
    else:
        return mu + torch.sqrt(var)*torch.randn_like(mu)
 

def sampling_laplace(param, log_scale=None):
    if log_scale is not None:
        mu = param
        scale = torch.exp(log_scale)
    else:
        k = param.shape[-1]//2
        mu = param[:,:,:k]
        scale = torch.exp(param[:,:,k:])
    small_zero = torch.finfo(mu.dtype).eps
    eps = torch.empty_like(mu).uniform_(small_zero-1,1-small_zero)

    return mu - scale * eps.sign() * torch.log1p(-eps.abs()) # scale
 

def kl_laplace_laplace_param(mu_q, sigma_q, mu_p, sigma_p):
    """ ln(λ_j/λ_i) + |θ_i-θ_j|/λ_j + λ_i/λ_j * exp(−|θ_i-θ_j|/λ_i) − 1 """

    scale_q = torch.clamp(sigma_q.exp(), min=1e-12)
    scale_p = torch.clamp(sigma_p.exp(), min=1e-12)

    mu_abs = torch.abs(mu_q-mu_p)

    return torch.mean(torch.sum(torch.log(scale_p/scale_q) + mu_abs/scale_p + (scale_q/scale_p)*torch.exp(-mu_abs/scale_q) - 1, -1), -1) # B / 1


def kl_laplace_laplace(q, p, sum_flag=True):
    """ ln(λ_j/λ_i) + |θ_i-θ_j|/λ_j + λ_i/λ_j * exp(−|θ_i-θ_j|/λ_i) − 1 """

    D = q.shape[-1] // 2
    if len(q.shape) > 2:
        scale_q = torch.clamp(torch.exp(q[:,:,D:]), min=1e-12)
        scale_p = torch.clamp(torch.exp(p[:,:,D:]), min=1e-12)

        mu_abs = torch.abs(q[:,:,:D]-p[:,:,:D])

        if sum_flag:
            return torch.mean(torch.sum(torch.log(scale_p/scale_q) + mu_abs/scale_p + (scale_q/scale_p)*torch.exp(-mu_abs/scale_q) - 1, -1), -1) # B x T x C --> B x T --> B
        else:
            return torch.mean(torch.mean(torch.log(scale_p/scale_q) + mu_abs/scale_p + (scale_q/scale_p)*torch.exp(-mu_abs/scale_q) - 1, -1), -1) # B x T x C --> B x T --> B
    else:
        scale_q = torch.clamp(torch.exp(q[:,D:]), min=1e-12)
        scale_p = torch.clamp(torch.exp(p[:,D:]), min=1e-12)

        mu_abs = torch.abs(q[:,:D]-p[:,:D])

        if sum_flag:
            return torch.mean(torch.sum(torch.log(scale_p/scale_q) + mu_abs/scale_p + (scale_q/scale_p)*torch.exp(-mu_abs/scale_q) - 1, -1)) # T x C --> T --> 1
        else:
            return torch.mean(torch.mean(torch.log(scale_p/scale_q) + mu_abs/scale_p + (scale_q/scale_p)*torch.exp(-mu_abs/scale_q) - 1, -1)) # T x C --> T --> 1


class GRU_VAE_ENCODER(nn.Module):
    def __init__(self, in_dim=80, lat_dim=96, hidden_layers=1, hidden_units=512, kernel_size=5, s_conv_flag=False,
            dilation_size=1, do_prob=0, use_weight_norm=True, causal_conv=False, right_size=0, seg_conv_flag=True,
                pad_first=True, scale_out_flag=False, n_spk=None, cont=True, scale_in_flag=True):
        super(GRU_VAE_ENCODER, self).__init__()
        self.in_dim = in_dim
        self.lat_dim = lat_dim
        self.hidden_layers = hidden_layers
        self.hidden_units = hidden_units
        self.kernel_size = kernel_size
        self.dilation_size = dilation_size
        self.do_prob = do_prob
        self.causal_conv = causal_conv
        self.right_size = right_size
        self.pad_first = pad_first
        self.use_weight_norm = use_weight_norm
        self.s_conv_flag = s_conv_flag
        self.seg_conv_flag = seg_conv_flag
        if self.s_conv_flag:
            self.s_dim = 320
        self.cont = cont
        if self.cont:
            self.out_dim = self.lat_dim*2
        else:
            self.out_dim = self.lat_dim
        self.n_spk = n_spk
        if self.n_spk is not None:
            self.out_dim += self.n_spk
        self.scale_in_flag = scale_in_flag
        self.scale_out_flag = scale_out_flag

        # Normalization layer
        if self.scale_in_flag:
            self.scale_in = nn.Conv1d(self.in_dim, self.in_dim, 1)

        # Conv. layers
        if self.right_size <= 0:
            if not self.causal_conv:
                self.conv = TwoSidedDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = self.conv.padding
            else:
                self.conv = CausalDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = 0
        else:
            self.conv = SkewedConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                        right_size=self.right_size, pad_first=self.pad_first)
            self.pad_left = self.conv.left_size
            self.pad_right = self.conv.right_size
        if self.s_conv_flag:
            if self.seg_conv_flag:
                conv_s_c = [nn.Conv1d(self.in_dim*self.conv.rec_field, self.s_dim, 1), nn.ReLU()]
            else:
                conv_s_c = [nn.Conv1d(self.conv.out_dim, self.s_dim, 1), nn.ReLU()]
            self.conv_s_c = nn.Sequential(*conv_s_c)
            self.in_dim = self.s_dim
        else:
            self.in_dim = self.in_dim*self.conv.rec_field
        if self.do_prob > 0:
            self.conv_drop = nn.Dropout(p=self.do_prob)

        # GRU layer(s)
        if self.do_prob > 0 and self.hidden_layers > 1:
            self.gru = nn.GRU(self.in_dim, self.hidden_units, self.hidden_layers,
                                dropout=self.do_prob, batch_first=True)
        else:
            self.gru = nn.GRU(self.in_dim, self.hidden_units, self.hidden_layers,
                                batch_first=True)
        if self.do_prob > 0:
            self.gru_drop = nn.Dropout(p=self.do_prob)

        # Output layers
        self.out = nn.Conv1d(self.hidden_units, self.out_dim, 1)
        if self.scale_out_flag:
            self.scale_out = nn.Conv1d(self.lat_dim, self.lat_dim, 1)

        # apply weight norm
        if use_weight_norm:
            self.apply_weight_norm()
        else:
            self.apply(initialize)

    def forward(self, x, h=None, do=False, sampling=True, outpad_right=0):
        if self.scale_in_flag:
            if self.s_conv_flag:
                x_in = self.conv_s_c(self.conv(self.scale_in(x.transpose(1,2)))).transpose(1,2)
            else:
                x_in = self.conv(self.scale_in(x.transpose(1,2))).transpose(1,2)
        else:
            if self.s_conv_flag:
                x_in = self.conv_s_c(self.conv(self.conv_mid(x.transpose(1,2)))).transpose(1,2)
            else:
                x_in = self.conv(self.conv_mid(x.transpose(1,2))).transpose(1,2)
        # Input s layers
        if self.do_prob > 0 and do:
            s = self.conv_drop(x_in) # B x C x T --> B x T x C
        else:
            s = x_in # B x C x T --> B x T x C
        if outpad_right > 0:
            # GRU s layers
            if h is None:
                out, h = self.gru(s[:,:-outpad_right]) # B x T x C
            else:
                out, h = self.gru(s[:,:-outpad_right], h) # B x T x C
            out_, _ = self.gru(s[:,-outpad_right:], h) # B x T x C
            s = torch.cat((out, out_), 1)
        else:
            # GRU s layers
            if h is None:
                s, h = self.gru(s) # B x T x C
            else:
                s, h = self.gru(s, h) # B x T x C
        # Output s layers
        if self.do_prob > 0 and do:
            s = torch.clamp(self.out(self.gru_drop(s).transpose(1,2)).transpose(1,2), min=MIN_CLAMP, max=MAX_CLAMP) # B x T x C -> B x C x T -> B x T x C
        else:
            s = torch.clamp(self.out(s.transpose(1,2)).transpose(1,2), min=MIN_CLAMP, max=MAX_CLAMP) # B x T x C -> B x C x T -> B x T x C

        if self.n_spk is not None: #with speaker posterior
            if self.cont: #continuous latent
                spk_logits = F.selu(s[:,:,:self.n_spk])
                if self.scale_out_flag:
                    mus = self.scale_out(F.tanhshrink(s[:,:,self.n_spk:-self.lat_dim]).transpose(1,2)).transpose(1,2)
                else:
                    mus = F.tanhshrink(s[:,:,self.n_spk:-self.lat_dim])
                log_scales = F.logsigmoid(s[:,:,-self.lat_dim:])
                if sampling:
                    if do:
                        return spk_logits, torch.cat((mus, torch.clamp(log_scales, min=CLIP_1E12)), 2), \
                                sampling_laplace(mus, log_scales), h.detach()
                    else:
                        return spk_logits, torch.cat((mus, log_scales), 2), \
                                sampling_laplace(mus, log_scales), h.detach()
                else:
                    return spk_logits, torch.cat((mus, log_scales), 2), mus, h.detach()
            else: #discrete latent
                if self.scale_out_flag:
                    return F.selu(s[:,:,:self.n_spk]), \
                        self.scale_out(F.tanhshrink(s[:,:,-self.lat_dim:]).transpose(1,2)).transpose(1,2), \
                            h.detach()
                else:
                    return F.selu(s[:,:,:self.n_spk]), F.tanhshrink(s[:,:,-self.lat_dim:]), h.detach()
        else: #without speaker posterior
            if self.cont: #continuous latent
                if self.scale_out_flag:
                    mus = self.scale_out(F.tanhshrink(s[:,:,:self.lat_dim]).transpose(1,2)).transpose(1,2)
                else:
                    mus = F.tanhshrink(s[:,:,:self.lat_dim])
                log_scales = F.logsigmoid(s[:,:,self.lat_dim:])
                if sampling:
                    if do:
                        return torch.cat((mus, torch.clamp(log_scales, min=CLIP_1E12)), 2), \
                                sampling_laplace(mus, log_scales), h.detach()
                    else:
                        return torch.cat((mus, log_scales), 2), \
                                sampling_laplace(mus, log_scales), h.detach()
                else:
                    return torch.cat((mus, log_scales), 2), mus, h.detach()
            else: #discrete latent
                if self.scale_out_flag:
                    return self.scale_out(F.tanhshrink(s).transpose(1,2)).transpose(1,2), h.detach()
                else:
                    return F.tanhshrink(s), h.detach()

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:
                return

        self.apply(_remove_weight_norm)


class GRU_SPEC_DECODER(nn.Module):
    def __init__(self, feat_dim=158, out_dim=80, hidden_layers=1, hidden_units=640, causal_conv=True,
            kernel_size=5, dilation_size=1, do_prob=0, n_spk=14, use_weight_norm=True, scale_out_flag=True,
                excit_dim=None, pad_first=True, right_size=None, pdf=False, scale_in_flag=False, s_conv_flag=False,
                    seg_conv_flag=True, aux_dim=None, red_dim=None, red_dim_upd=None, post_layer=False, pdf_gauss=False):
        super(GRU_SPEC_DECODER, self).__init__()
        self.n_spk = n_spk
        self.feat_dim = feat_dim
        self.aux_dim = aux_dim
        if self.n_spk is not None:
            self.in_dim = self.n_spk+self.feat_dim
        else:
            self.in_dim = self.feat_dim
        if self.aux_dim is not None:
            self.in_dim += self.aux_dim
        self.spec_dim = out_dim
        self.excit_dim = excit_dim
        self.hidden_layers = hidden_layers
        self.hidden_units = hidden_units
        self.kernel_size = kernel_size
        self.dilation_size = dilation_size
        self.do_prob = do_prob
        self.causal_conv = causal_conv
        self.use_weight_norm = use_weight_norm
        self.s_conv_flag = s_conv_flag
        self.seg_conv_flag = seg_conv_flag
        if self.s_conv_flag:
            self.s_dim = 320
        self.pad_first = pad_first
        self.right_size = right_size
        self.pdf = pdf
        self.pdf_gauss = pdf_gauss
        self.post_layer = post_layer
        if self.pdf or self.pdf_gauss:
            self.out_dim = self.spec_dim*2
            self.post_layer = False
        else:
            self.out_dim = self.spec_dim
        self.scale_in_flag = scale_in_flag
        self.scale_out_flag = scale_out_flag
        self.red_dim = red_dim
        self.red_dim_upd = red_dim_upd

        if self.excit_dim is not None:
            if self.scale_in_flag:
                self.scale_in = nn.Conv1d(self.spec_dim+self.excit_dim, self.spec_dim+self.excit_dim, 1)
            else:
                self.scale_in = nn.Conv1d(self.excit_dim, self.excit_dim, 1)
            self.in_dim += self.excit_dim
        elif self.scale_in_flag:
            self.scale_in = nn.Conv1d(self.spec_dim, self.spec_dim, 1)

        # Reduction layers
        if self.red_dim is not None:
            if self.red_dim_upd is not None and self.scale_in_flag and self.excit_dim is None:
                in_red = [nn.Conv1d(self.spec_dim, self.red_dim, 1), nn.ReLU()]
            else:
                in_red = [nn.Conv1d(self.in_dim, self.red_dim, 1), nn.ReLU()]
            self.in_red = nn.Sequential(*in_red)
            if self.red_dim_upd is None:
                self.in_dim = self.red_dim
        if self.red_dim_upd is not None:
            if self.red_dim is not None:
                assert(self.red_dim == self.red_dim_upd)
            in_red_upd = [nn.Conv1d(self.in_dim, self.red_dim_upd, 1), nn.ReLU()]
            self.in_red_upd = nn.Sequential(*in_red_upd)
            self.in_dim = self.red_dim_upd

        # Conv. layers
        if self.right_size <= 0:
            if not self.causal_conv:
                self.conv = TwoSidedDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = self.conv.padding
            else:
                self.conv = CausalDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = 0
        else:
            self.conv = SkewedConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                        right_size=self.right_size, pad_first=self.pad_first)
            self.pad_left = self.conv.left_size
            self.pad_right = self.conv.right_size
        if self.s_conv_flag:
            if self.seg_conv_flag:
                conv_s_c = [nn.Conv1d(self.in_dim*self.conv.rec_field, self.s_dim, 1), nn.ReLU()]
            else:
                conv_s_c = [nn.Conv1d(self.conv.out_dim, self.s_dim, 1), nn.ReLU()]
            self.conv_s_c = nn.Sequential(*conv_s_c)
            self.in_dim = self.s_dim
        else:
            self.in_dim = self.in_dim*self.conv.rec_field
        if self.do_prob > 0:
            self.conv_drop = nn.Dropout(p=self.do_prob)

        # GRU layer(s)
        if self.do_prob > 0 and self.hidden_layers > 1:
            self.gru = nn.GRU(self.in_dim, self.hidden_units, self.hidden_layers,
                                dropout=self.do_prob, batch_first=True)
        else:
            self.gru = nn.GRU(self.in_dim, self.hidden_units, self.hidden_layers,
                                batch_first=True)
        if self.do_prob > 0:
            self.gru_drop = nn.Dropout(p=self.do_prob)

        # Output layers
        self.out = nn.Conv1d(self.hidden_units, self.out_dim, 1)

        # Post layers
        if self.post_layer:
            self.post = nn.Conv1d(self.out_dim, self.out_dim*2, 1)

        # De-normalization layers
        if self.scale_out_flag:
            self.scale_out = nn.Conv1d(self.spec_dim, self.spec_dim, 1)

        # apply weight norm
        if self.use_weight_norm:
            self.apply_weight_norm()
        else:
            self.apply(initialize)

    def forward(self, z, y=None, aux=None, h=None, do=False, e=None, outpad_right=0, sampling=True, scale_fact=None, ret_mid_feat=False, org_in=False, do_conv=False, temp=None):
        if aux is not None:
            if y is not None:
                if len(y.shape) == 2:
                    y = F.one_hot(y, num_classes=self.n_spk).float()
                if e is not None:
                    if self.scale_in_flag:
                        z = torch.cat((y, aux, self.scale_in((torch.cat(e, z), 2).transpose(1,2)).transpose(1,2)), 2) # B x T_frm x C
                    else:
                        z = torch.cat((y, aux, self.scale_in(e.transpose(1,2)).transpose(1,2), z), 2) # B x T_frm x C
                else:
                    if self.red_dim_upd is None and self.scale_in_flag:
                        z = torch.cat((y, aux, self.scale_in(z.transpose(1,2)).transpose(1,2)), 2) # B x T_frm x C
                    else:
                        z = torch.cat((y, aux, z), 2) # B x T_frm x C
            else:
                if e is not None:
                    if self.scale_in_flag:
                        z = torch.cat((aux, self.scale_in((torch.cat(e, z), 2).transpose(1,2)).transpose(1,2)), 2) # B x T_frm x C
                    else:
                        z = torch.cat((aux, self.scale_in(e.transpose(1,2)).transpose(1,2), z), 2) # B x T_frm x C
                elif self.scale_in_flag:
                        z = torch.cat((aux, self.scale_in(z.transpose(1,2)).transpose(1,2)), 2) # B x T_frm x C
                else:
                        z = torch.cat((aux, z), 2) # B x T_frm x C
        else:
            if y is not None:
                if len(y.shape) == 2:
                    y = F.one_hot(y, num_classes=self.n_spk).float()
                if e is not None:
                    if self.scale_in_flag:
                        z = torch.cat((y, self.scale_in((torch.cat(e, z), 2).transpose(1,2)).transpose(1,2)), 2) # B x T_frm x C
                    else:
                        z = torch.cat((y, self.scale_in(e.transpose(1,2)).transpose(1,2), z), 2) # B x T_frm x C
                else:
                    if self.scale_in_flag:
                        z = torch.cat((y, self.scale_in(z.transpose(1,2)).transpose(1,2)), 2) # B x T_frm x C
                    else:
                        z = torch.cat((y, z), 2) # B x T_frm x C
            else:
                if e is not None:
                    if self.scale_in_flag:
                        z = self.scale_in((torch.cat(e, z), 2).transpose(1,2)).transpose(1,2) # B x T_frm x C
                    else:
                        z = torch.cat((self.scale_in(e.transpose(1,2)).transpose(1,2), z), 2) # B x T_frm x C
                elif self.scale_in_flag:
                        z = self.scale_in(z.transpose(1,2)).transpose(1,2) # B x T_frm x C
        # Input e layers
        if self.red_dim is not None and (not self.red_dim_upd or (self.red_dim_upd and org_in)):
            if ret_mid_feat:
                melsp_relu = self.in_red(z.transpose(1,2)).transpose(1,2)
                if self.s_conv_flag:
                    e = melsp_conv = self.conv_s_c(self.conv(melsp_relu.transpose(1,2))).transpose(1,2)
                else:
                    e = melsp_conv = self.conv(melsp_relu.transpose(1,2)).transpose(1,2)
                if self.pad_right > 0:
                    melsp_relu = melsp_relu[:,self.pad_left:-self.pad_right]
                else:
                    melsp_relu = melsp_relu[:,self.pad_left:]
            else:
                if self.do_prob > 0 and (do or do_conv):
                    if self.s_conv_flag:
                        e = self.conv_drop(self.conv_s_c(self.conv(self.in_red(z.transpose(1,2)))).transpose(1,2)) # B x C x T --> B x T x C
                    else:
                        e = self.conv_drop(self.conv(self.in_red(z.transpose(1,2))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    if self.s_conv_flag:
                        e = self.conv_s_c(self.conv(self.in_red(z.transpose(1,2)))).transpose(1,2) # B x C x T --> B x T x C
                    else:
                        e = self.conv(self.in_red(z.transpose(1,2))).transpose(1,2) # B x C x T --> B x T x C
        elif self.red_dim_upd is not None:
            if ret_mid_feat:
                melsp_relu = self.in_red_upd(z.transpose(1,2)).transpose(1,2)
                if self.s_conv_flag:
                    e = melsp_conv = self.conv_s_c(self.conv(melsp_relu.transpose(1,2))).transpose(1,2)
                else:
                    e = melsp_conv = self.conv(melsp_relu.transpose(1,2)).transpose(1,2)
                if self.pad_right > 0:
                    melsp_relu = melsp_relu[:,self.pad_left:-self.pad_right]
                else:
                    melsp_relu = melsp_relu[:,self.pad_left:]
            else:
                if self.do_prob > 0 and (do or do_conv):
                    if self.s_conv_flag:
                        e = self.conv_drop(self.conv_s_c(self.conv(self.in_red_upd(z.transpose(1,2)))).transpose(1,2)) # B x C x T --> B x T x C
                    else:
                        e = self.conv_drop(self.conv(self.in_red_upd(z.transpose(1,2))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    if self.s_conv_flag:
                        e = self.conv_s_c(self.conv(self.in_red_upd(z.transpose(1,2)))).transpose(1,2) # B x C x T --> B x T x C
                    else:
                        e = self.conv(self.in_red_upd(z.transpose(1,2))).transpose(1,2) # B x C x T --> B x T x C
        else:
            if self.do_prob > 0 and (do or do_conv):
                if self.s_conv_flag:
                    e = self.conv_drop(self.conv_s_c(self.conv(z.transpose(1,2))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    e = self.conv_drop(self.conv(z.transpose(1,2)).transpose(1,2)) # B x C x T --> B x T x C
            else:
                if self.s_conv_flag:
                    e = self.conv_s_c(self.conv(z.transpose(1,2))).transpose(1,2) # B x C x T --> B x T x C
                else:
                    e = self.conv(z.transpose(1,2)).transpose(1,2) # B x C x T --> B x T x C
        if outpad_right > 0:
            # GRU e layers
            if h is None:
                out, h = self.gru(e[:,:-outpad_right]) # B x T x C
            else:
                out, h = self.gru(e[:,:-outpad_right], h) # B x T x C
            out_, _ = self.gru(e[:,-outpad_right:], h) # B x T x C
            e = torch.cat((out, out_), 1)
        else:
            # GRU e layers
            if h is None:
                e, h = self.gru(e) # B x T x C
            else:
                e, h = self.gru(e, h) # B x T x C
        # Output e layers
        if ret_mid_feat:
            melsp_gru = e
            melsp_out = self.out(e.transpose(1,2)).transpose(1,2)
            e = torch.clamp(melsp_out, min=MIN_CLAMP, max=MAX_CLAMP)
        else:
            if self.do_prob > 0 and do:
                e = torch.clamp(self.out(self.gru_drop(e).transpose(1,2)).transpose(1,2), min=MIN_CLAMP, max=MAX_CLAMP) # B x T x C -> B x C x T -> B x T x C
            else:
                e = torch.clamp(self.out(e.transpose(1,2)).transpose(1,2), min=MIN_CLAMP, max=MAX_CLAMP) # B x T x C -> B x C x T -> B x T x C

        if not self.pdf and not self.pdf_gauss:
            if not self.post_layer:
                if self.scale_out_flag:
                    return self.scale_out(F.tanhshrink(e).transpose(1,2)).transpose(1,2), h.detach()
                else:
                    return F.tanhshrink(e), h.detach()
            else:
                e = F.tanhshrink(e).transpose(1,2)
                x_ = self.scale_out(e).transpose(1,2)
                e = torch.clamp(self.post(e), min=MIN_CLAMP, max=MAX_CLAMP)
                mus_e = self.scale_out(F.tanhshrink(e[:,:self.spec_dim,:])).transpose(1,2)
                mus = x_+mus_e
                log_scales = F.logsigmoid(e[:,self.spec_dim:,:]).transpose(1,2)
                if scale_fact is None:
                    e = sampling_laplace(mus_e, log_scales)
                else:
                    e = sampling_laplace(mus_e, log_scales+scale_fact)
                if do or do_conv:
                    return torch.cat((mus, torch.clamp(log_scales, min=CLIP_1E12)), 2), x_+e, x_, h.detach()
                else:
                    return torch.cat((mus, log_scales), 2), x_+e, x_, h.detach()
        elif self.pdf_gauss:
            if self.scale_out_flag:
                mus = self.scale_out(F.tanhshrink(e[:,:,:self.spec_dim]).transpose(1,2)).transpose(1,2)
            else:
                mus = F.tanhshrink(e[:,:,:self.spec_dim])
            var = torch.sigmoid(e[:,:,self.spec_dim:])**2
            if sampling:
                if do:
                    return torch.cat((mus, torch.clamp(var, min=1e-12)), 2), \
                            sampling_gauss(mus, var, temp), h.detach()
                else:
                    return torch.cat((mus, var), 2), sampling_gauss(mus, var, temp), h.detach()
            else:
                return torch.cat((mus, var), 2), mus, h.detach()
        else:
            if self.scale_out_flag:
                mus = self.scale_out(F.tanhshrink(e[:,:,:self.spec_dim]).transpose(1,2)).transpose(1,2)
            else:
                mus = F.tanhshrink(e[:,:,:self.spec_dim])
            log_scales = F.logsigmoid(e[:,:,self.spec_dim:])
            if sampling:
                if ret_mid_feat:
                    return torch.cat((mus, log_scales), 2), sampling_laplace(mus, log_scales), \
                            melsp_relu, melsp_conv, melsp_gru, melsp_out, h.detach()
                else:
                    if do or do_conv:
                        return torch.cat((mus, torch.clamp(log_scales, min=CLIP_1E12)), 2), \
                                sampling_laplace(mus, log_scales), h.detach()
                    else:
                        return torch.cat((mus, log_scales), 2), sampling_laplace(mus, log_scales), \
                                h.detach()
            else:
                return torch.cat((mus, log_scales), 2), mus, h.detach()


    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:
                return

        self.apply(_remove_weight_norm)


class GRU_LAT_FEAT_CLASSIFIER(nn.Module):
    def __init__(self, lat_dim=None, feat_dim=50, n_spk=14, hidden_layers=1, hidden_units=32,
            use_weight_norm=True, feat_aux_dim=None, spk_aux_dim=None, do_prob=0):
        super(GRU_LAT_FEAT_CLASSIFIER, self).__init__()
        self.lat_dim = lat_dim
        self.spk_aux_dim = spk_aux_dim
        self.feat_aux_dim = feat_aux_dim
        self.feat_dim = feat_dim
        self.n_spk = n_spk
        self.hidden_layers = hidden_layers
        self.hidden_units = hidden_units
        self.use_weight_norm = use_weight_norm
        self.do_prob = do_prob

        # Conv. layers
        if self.lat_dim is not None:
            conv_lat = [nn.Conv1d(self.lat_dim, self.hidden_units, 1), nn.ReLU()]
            self.conv_lat = nn.Sequential(*conv_lat)
        if self.feat_aux_dim is not None:
            conv_feat_aux = [nn.Conv1d(self.feat_aux_dim, self.hidden_units, 1), nn.ReLU()]
            self.conv_feat_aux = nn.Sequential(*conv_feat_aux)
        if self.spk_aux_dim is not None:
            conv_spk_aux = [nn.Conv1d(self.spk_aux_dim, self.hidden_units, 1), nn.ReLU()]
            self.conv_spk_aux = nn.Sequential(*conv_spk_aux)
        conv_feat = [nn.Conv1d(self.feat_dim, self.hidden_units, 1), nn.ReLU()]
        self.conv_feat = nn.Sequential(*conv_feat)

        # GRU layer(s)
        self.gru = nn.GRU(self.hidden_units, self.hidden_units, self.hidden_layers, batch_first=True)
        if self.do_prob > 0:
            self.gru_drop = nn.Dropout(p=self.do_prob)

        # Output layers
        self.out = nn.Conv1d(self.hidden_units, self.n_spk, 1)

        # apply weight norm
        if self.use_weight_norm:
            self.apply_weight_norm()
        else:
            self.apply(initialize)

    def forward(self, lat=None, feat=None, feat_aux=None, spk_aux=None, h=None, do=False):
        # Input layers
        if lat is not None:
            c = self.conv_lat(lat.transpose(1,2)).transpose(1,2)
        elif spk_aux is not None:
            c = self.conv_spk_aux(spk_aux.transpose(1,2)).transpose(1,2)
        elif feat_aux is not None:
            c = self.conv_feat_aux(feat_aux.transpose(1,2)).transpose(1,2)
        else:
            c = self.conv_feat(feat.transpose(1,2)).transpose(1,2)
        # GRU layers
        if h is not None:
            out, h = self.gru(c, h) # B x T x C
        else:
            out, h = self.gru(c) # B x T x C
        # Output layers
        if do and self.do_prob > 0:
            return F.selu(torch.clamp(self.out(self.gru_drop(out).transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2), h.detach() # B x T x C -> B x C x T -> B x T x C
        else:
            return F.selu(torch.clamp(self.out(out.transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2), h.detach() # B x T x C -> B x C x T -> B x T x C

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:
                return

        self.apply(_remove_weight_norm)


class GRU_SPK(nn.Module):
    def __init__(self, n_spk=14, feat_dim=64, hidden_layers=1, hidden_units=32, do_prob=0, n_weight_emb=None,
            kernel_size=3, dilation_size=1,  use_weight_norm=True, scale_in_flag=False, red_dim=None, weight_fact=2,
                s_conv_flag=False, seg_conv_flag=True, dim_out=None, right_size=0, pad_first=True, causal_conv=True):
        super(GRU_SPK, self).__init__()
        self.n_spk = n_spk
        self.feat_dim = feat_dim
        if self.n_spk is not None:
            self.in_dim = self.n_spk+self.feat_dim
        else:
            self.in_dim = self.feat_dim
        self.hidden_layers = hidden_layers
        self.hidden_units = hidden_units
        self.do_prob = do_prob
        self.use_weight_norm = use_weight_norm
        self.scale_in_flag = scale_in_flag
        self.kernel_size = kernel_size
        self.dilation_size = dilation_size
        self.causal_conv = causal_conv
        if dim_out is not None:
            self.dim_out = dim_out
        else:
            self.dim_out = self.n_spk
        self.pad_first = pad_first
        self.right_size = right_size
        self.red_dim = red_dim
        self.s_conv_flag = s_conv_flag
        self.seg_conv_flag = seg_conv_flag
        if self.s_conv_flag:
            self.s_dim = 320
        self.n_weight_emb = n_weight_emb
        self.weight_fact = weight_fact

        if self.scale_in_flag:
            self.scale_in = nn.Conv1d(self.feat_dim, self.feat_dim, 1)

        # Reduction layers
        if self.red_dim is not None:
            in_red = [nn.Conv1d(self.in_dim, self.red_dim, 1), nn.ReLU()]
            self.in_red = nn.Sequential(*in_red)
            self.in_dim = self.red_dim

        # Conv. layers
        if self.right_size <= 0:
            if not self.causal_conv:
                self.conv = TwoSidedDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = self.conv.padding
            else:
                self.conv = CausalDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = 0
        else:
            self.conv = SkewedConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                        right_size=self.right_size, pad_first=self.pad_first)
            self.pad_left = self.conv.left_size
            self.pad_right = self.conv.right_size
        if self.s_conv_flag:
            if self.seg_conv_flag:
                conv_s_c = [nn.Conv1d(self.in_dim*self.conv.rec_field, self.s_dim, 1), nn.ReLU()]
            else:
                conv_s_c = [nn.Conv1d(self.conv.out_dim, self.s_dim, 1), nn.ReLU()]
            self.conv_s_c = nn.Sequential(*conv_s_c)
            self.in_dim = self.s_dim
        else:
            self.in_dim = self.in_dim*self.conv.rec_field
        if self.do_prob > 0:
            self.conv_drop = nn.Dropout(p=self.do_prob)

        # GRU layer(s)
        if self.do_prob > 0 and self.hidden_layers > 1:
            self.gru = nn.GRU(self.in_dim, self.hidden_units, self.hidden_layers,
                                dropout=self.do_prob, batch_first=True)
        else:
            self.gru = nn.GRU(self.in_dim, self.hidden_units, self.hidden_layers,
                                batch_first=True)
        if self.do_prob > 0:
            self.gru_drop = nn.Dropout(p=self.do_prob)

        # Output layers
        if self.n_weight_emb is not None:
            self.out = nn.Conv1d(self.hidden_units, self.n_weight_emb, 1)
            self.dim_weight_emb = self.n_spk//(self.n_weight_emb//self.weight_fact)
            self.embed_spk = nn.Embedding(self.n_weight_emb, self.dim_weight_emb)
        else:
            self.out = nn.Conv1d(self.hidden_units, self.dim_out, 1)

        # apply weight norm
        if self.use_weight_norm:
            self.apply_weight_norm()
        else:
            self.apply(initialize)

    def forward(self, y, z=None, h=None, do=False, outpad_right=0):
        if len(y.shape) == 2:
            y = F.one_hot(y, num_classes=self.n_spk).float()
        if self.scale_in_flag:
            z = torch.cat((y, self.scale_in(z.transpose(1,2)).transpose(1,2)), 2) # B x T_frm x C
        else:
            z = torch.cat((y, z), 2) # B x T_frm x C
        # Conv layers
        if self.s_conv_flag:
            if self.red_dim is not None:
                if self.do_prob > 0 and do:
                    z = self.conv_drop(self.conv_s_c(self.conv(self.in_red(z.transpose(1,2)))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    z = self.conv_s_c(self.conv(self.in_red(z.transpose(1,2)))).transpose(1,2) # B x C x T --> B x T x C
            else:
                if self.do_prob > 0 and do:
                    z = self.conv_drop(self.conv_s_c(self.conv(z.transpose(1,2))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    z = self.conv_s_c(self.conv(z.transpose(1,2))).transpose(1,2) # B x C x T --> B x T x C
        else:
            if self.red_dim is not None:
                if self.do_prob > 0 and do:
                    z = self.conv_drop(self.conv(self.in_red(z.transpose(1,2))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    z = self.conv(self.in_red(z.transpose(1,2))).transpose(1,2) # B x C x T --> B x T x C
            else:
                if self.do_prob > 0 and do:
                    z = self.conv_drop(self.conv(z.transpose(1,2)).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    z = self.conv(z.transpose(1,2)).transpose(1,2) # B x C x T --> B x T x C
        # GRU layers
        if outpad_right > 0:
            if h is None:
                out, h = self.gru(z[:,:-outpad_right]) # B x T x C
            else:
                out, h = self.gru(z[:,:-outpad_right], h) # B x T x C
            out_, _ = self.gru(z[:,-outpad_right:], h) # B x T x C
            e = torch.cat((out, out_), 1)
        else:
            if h is None:
                e, h = self.gru(z) # B x T x C
            else:
                e, h = self.gru(z, h) # B x T x C
        # Output layers
        if self.do_prob > 0 and do:
            e = self.out(self.gru_drop(e).transpose(1,2)).transpose(1,2) # B x T x C -> B x C x T -> B x T x C
        else:
            e = self.out(e.transpose(1,2)).transpose(1,2) # B x T x C -> B x C x T -> B x T x C

        if self.n_weight_emb is not None:
            weight_emb = torch.tanh(torch.clamp(e, min=MIN_CLAMP, max=MAX_CLAMP)) # B x T x n_weight
            out = self.embed_spk.weight[0].unsqueeze(0).unsqueeze(1)*weight_emb[:,:,:1] # 1 x 1 x emb_dim * B x T x 1
            for i in range(1,self.n_weight_emb):
                out = torch.cat((out, self.embed_spk.weight[i].unsqueeze(0).unsqueeze(1)*weight_emb[:,:,i:i+1]), 2) # 1 x 1 x emb_dim * B x T x 1
            # B x T x emb_dim*n_weight
            return out, h.detach()
        else:
            return F.tanhshrink(torch.clamp(e, min=MIN_CLAMP, max=MAX_CLAMP)), h.detach()

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:
                return

        self.apply(_remove_weight_norm)


class SPKID_TRANSFORM_LAYER(nn.Module):
    def __init__(self, n_spk=14, spkidtr_dim=2, emb_dim=None, n_weight_emb=None, conv_emb_flag=False, use_weight_norm=True):
        super(SPKID_TRANSFORM_LAYER, self).__init__()

        self.n_spk = n_spk
        self.spkidtr_dim = spkidtr_dim
        self.emb_dim = emb_dim
        self.n_weight_emb = n_weight_emb
        if self.n_weight_emb is not None:
            if self.emb_dim is None:
                self.emb_dim = self.n_spk
            self.dim_weight_emb = self.emb_dim // self.n_weight_emb
            self.emb_dim = self.dim_weight_emb * self.n_weight_emb
        self.conv_emb_flag = conv_emb_flag
        if self.conv_emb_flag and self.emb_dim is None:
            self.emb_dim = self.n_spk
        self.use_weight_norm = use_weight_norm

        if self.spkidtr_dim is not None:
            if self.conv_emb_flag:
                conv_emb = [nn.Conv1d(self.n_spk, self.emb_dim, 1), nn.ReLU()]
                self.conv_emb = nn.Sequential(*conv_emb)
                self.conv = nn.Conv1d(self.emb_dim, self.spkidtr_dim, 1)
            else:
                self.conv = nn.Conv1d(self.n_spk, self.spkidtr_dim, 1)
            if self.n_weight_emb is not None:
                 self.deconv = nn.Conv1d(self.spkidtr_dim, self.n_weight_emb, 1)
            else:
                if self.emb_dim is not None:
                    deconv = [nn.Conv1d(self.spkidtr_dim, self.emb_dim, 1), nn.ReLU()]
                else:
                    deconv = [nn.Conv1d(self.spkidtr_dim, self.n_spk, 1), nn.ReLU()]
                self.deconv = nn.Sequential(*deconv)
        else:
            if self.n_weight_emb is not None:
                if self.conv_emb_flag:
                    conv_emb = [nn.Conv1d(self.n_spk, self.emb_dim, 1), nn.ReLU()]
                    self.conv_emb = nn.Sequential(*conv_emb)
                    self.conv = nn.Conv1d(self.emb_dim, self.n_weight_emb, 1)
                else:
                    self.conv = nn.Conv1d(self.n_spk, self.n_weight_emb, 1)
            else:
                if self.emb_dim is not None:
                    conv = [nn.Conv1d(self.n_spk, self.emb_dim, 1), nn.ReLU()]
                else:
                    conv = [nn.Conv1d(self.n_spk, self.n_spk, 1), nn.ReLU()]
                self.conv = nn.Sequential(*conv)

        if self.n_weight_emb is not None:
            self.embed_spk = nn.Embedding(self.n_weight_emb, self.dim_weight_emb)

        # apply weight norm
        if self.use_weight_norm:
            self.apply_weight_norm()
        else:
            self.apply(initialize)

    def forward(self, x):
        # in: B x T
        # out: B x T x C
        if self.spkidtr_dim is not None:
            if self.n_weight_emb is not None:
                if self.conv_emb_flag:
                    weight_emb = torch.tanh(torch.clamp(self.deconv(F.tanhshrink(torch.clamp(self.conv(self.conv_emb(F.one_hot(x, num_classes=self.n_spk).float().transpose(1,2))),
                                                min=MIN_CLAMP, max=MAX_CLAMP))), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2) # B x T x n_weight
                else:
                    weight_emb = torch.tanh(torch.clamp(self.deconv(F.tanhshrink(torch.clamp(self.conv(F.one_hot(x, num_classes=self.n_spk).float().transpose(1,2)),
                                                min=MIN_CLAMP, max=MAX_CLAMP))), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2) # B x T x n_weight
                out = self.embed_spk.weight[0].unsqueeze(0).unsqueeze(1)*weight_emb[:,:,:1] # 1 x 1 x emb_dim * B x T x 1
                for i in range(1,self.n_weight_emb):
                    out = torch.cat((out, self.embed_spk.weight[i].unsqueeze(0).unsqueeze(1)*weight_emb[:,:,i:i+1]), 2) # 1 x 1 x emb_dim * B x T x 1
                # B x T x emb_dim*n_weight
                return weight_emb, out
            else:
                if self.conv_emb_flag:
                    return self.deconv(F.tanhshrink(torch.clamp(self.conv(self.conv_emb(F.one_hot(x, num_classes=self.n_spk).float().transpose(1,2))), min=MIN_CLAMP, max=MAX_CLAMP))).transpose(1,2)
                else:
                    return self.deconv(F.tanhshrink(torch.clamp(self.conv(F.one_hot(x, num_classes=self.n_spk).float().transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP))).transpose(1,2)
        else:
            if self.n_weight_emb is not None:
                if self.conv_emb_flag:
                    weight_emb = torch.tanh(torch.clamp(self.conv(self.conv_emb(F.one_hot(x, num_classes=self.n_spk).float().transpose(1,2))), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2) # B x T x n_weight
                else:
                    weight_emb = torch.tanh(torch.clamp(self.conv(F.one_hot(x, num_classes=self.n_spk).float().transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP)).transpose(1,2) # B x T x n_weight
                out = self.embed_spk.weight[0].unsqueeze(0).unsqueeze(1)*weight_emb[:,:,:1] # 1 x 1 x emb_dim * B x T x 1
                for i in range(1,self.n_weight_emb):
                    out = torch.cat((out, self.embed_spk.weight[i].unsqueeze(0).unsqueeze(1)*weight_emb[:,:,i:i+1]), 2) # 1 x 1 x emb_dim * B x T x 1
                # B x T x emb_dim*n_weight
                return weight_emb, out
            else:
                return self.conv(F.one_hot(x, num_classes=self.n_spk).float().transpose(1,2)).transpose(1,2)

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:
                return

        self.apply(_remove_weight_norm)


class GRU_EXCIT_DECODER(nn.Module):
    def __init__(self, feat_dim=60, hidden_layers=1, hidden_units=128, causal_conv=True, s_conv_flag=False,
            kernel_size=7, dilation_size=1, do_prob=0, n_spk=14, use_weight_norm=True, aux_dim=None,
                seg_conv_flag=True, cap_dim=None, right_size=0, pad_first=True, red_dim=None):
        super(GRU_EXCIT_DECODER, self).__init__()
        self.n_spk = n_spk
        self.feat_dim = feat_dim
        self.in_dim = self.n_spk+self.feat_dim
        self.cap_dim = cap_dim
        self.aux_dim = aux_dim
        if self.cap_dim is not None:
            self.out_dim = 2+1+self.cap_dim
        else:
            self.out_dim = 2
        if self.aux_dim is not None:
            self.in_dim += self.aux_dim
        self.hidden_layers = hidden_layers
        self.hidden_units = hidden_units
        self.kernel_size = kernel_size
        self.dilation_size = dilation_size
        self.do_prob = do_prob
        self.causal_conv = causal_conv
        self.s_conv_flag = s_conv_flag
        self.seg_conv_flag = seg_conv_flag
        if self.s_conv_flag:
            self.s_dim = 320
        self.use_weight_norm = use_weight_norm
        self.pad_first = pad_first
        self.right_size = right_size
        self.red_dim = red_dim

        # Reduction layers
        if self.red_dim is not None:
            in_red = [nn.Conv1d(self.in_dim, self.red_dim, 1), nn.ReLU()]
            self.in_red = nn.Sequential(*in_red)
            self.in_dim = self.red_dim

        # Conv. layers
        if self.right_size <= 0:
            if not self.causal_conv:
                self.conv = TwoSidedDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = self.conv.padding
            else:
                self.conv = CausalDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = 0
        else:
            self.conv = SkewedConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                        right_size=self.right_size, pad_first=self.pad_first)
            self.pad_left = self.conv.left_size
            self.pad_right = self.conv.right_size
        if self.s_conv_flag:
            if self.seg_conv_flag:
                conv_s_c = [nn.Conv1d(self.in_dim*self.conv.rec_field, self.s_dim, 1), nn.ReLU()]
            else:
                conv_s_c = [nn.Conv1d(self.conv.out_dim, self.s_dim, 1), nn.ReLU()]
            self.conv_s_c = nn.Sequential(*conv_s_c)
            self.in_dim = self.s_dim
        else:
            self.in_dim = self.in_dim*self.conv.rec_field
        if self.do_prob > 0:
            self.conv_drop = nn.Dropout(p=self.do_prob)

        # GRU layer(s)
        if self.do_prob > 0 and self.hidden_layers > 1:
            self.gru = nn.GRU(self.in_dim, self.hidden_units, self.hidden_layers,
                                dropout=self.do_prob, batch_first=True)
        else:
            self.gru = nn.GRU(self.in_dim, self.hidden_units, self.hidden_layers,
                                batch_first=True)
        if self.do_prob > 0:
            self.gru_drop = nn.Dropout(p=self.do_prob)

        # Output layers
        self.out = nn.Conv1d(self.hidden_units, self.out_dim, 1)

        # De-normalization layers
        self.scale_out = nn.Conv1d(1, 1, 1)
        if self.cap_dim is not None:
            self.scale_out_cap = nn.Conv1d(self.cap_dim, self.cap_dim, 1)

        # apply weight norm
        if self.use_weight_norm:
            self.apply_weight_norm()
        else:
            self.apply(initialize)

    def forward(self, z, y=None, aux=None, h=None, do=False, outpad_right=0):
        if y is not None:
            if aux is not None:
                if len(y.shape) == 2:
                    z = torch.cat((F.one_hot(y, num_classes=self.n_spk).float(), aux, z), 2) # B x T_frm x C
                else:
                    z = torch.cat((y, aux, z), 2) # B x T_frm x C
            else:
                if len(y.shape) == 2:
                    z = torch.cat((F.one_hot(y, num_classes=self.n_spk).float(), z), 2) # B x T_frm x C
                else:
                    z = torch.cat((y, z), 2) # B x T_frm x C
        elif aux is not None:
            z = torch.cat((aux, z), 2) # B x T_frm x C
        # Input e layers
        if self.s_conv_flag:
            if self.red_dim is not None:
                if self.do_prob > 0 and do:
                    e = self.conv_drop(self.conv_s_c(self.conv(self.in_red(z.transpose(1,2)))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    e = self.conv_s_c(self.conv(self.in_red(z.transpose(1,2)))).transpose(1,2) # B x C x T --> B x T x C
            else:
                if self.do_prob > 0 and do:
                    e = self.conv_drop(self.conv_s_c(self.conv(z.transpose(1,2))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    e = self.conv_s_c(self.conv(z.transpose(1,2))).transpose(1,2) # B x C x T --> B x T x C
        else:
            if self.red_dim is not None:
                if self.do_prob > 0 and do:
                    e = self.conv_drop(self.conv(self.in_red(z.transpose(1,2))).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    e = self.conv(self.in_red(z.transpose(1,2))).transpose(1,2) # B x C x T --> B x T x C
            else:
                if self.do_prob > 0 and do:
                    e = self.conv_drop(self.conv(z.transpose(1,2)).transpose(1,2)) # B x C x T --> B x T x C
                else:
                    e = self.conv(z.transpose(1,2)).transpose(1,2) # B x C x T --> B x T x C
        if outpad_right > 0:
            # GRU e layers
            if h is None:
                out, h = self.gru(e[:,:-outpad_right]) # B x T x C
            else:
                out, h = self.gru(e[:,:-outpad_right], h) # B x T x C
            out_, _ = self.gru(e[:,-outpad_right:], h) # B x T x C
            e = torch.cat((out, out_), 1)
        else:
            # GRU e layers
            if h is None:
                e, h = self.gru(e) # B x T x C
            else:
                e, h = self.gru(e, h) # B x T x C
        # Output e layers
        if self.do_prob > 0 and do:
            e = torch.clamp(self.out(self.gru_drop(e).transpose(1,2)).transpose(1,2), min=MIN_CLAMP, max=MAX_CLAMP) # B x T x C -> B x C x T -> B x T x C
        else:
            e = torch.clamp(self.out(e.transpose(1,2)).transpose(1,2), min=MIN_CLAMP, max=MAX_CLAMP) # B x T x C -> B x C x T -> B x T x C

        if self.cap_dim is not None:
            return torch.cat((torch.sigmoid(e[:,:,:1]), self.scale_out(F.tanhshrink(e[:,:,1:2]).transpose(1,2)).transpose(1,2), \
                            torch.sigmoid(e[:,:,2:3]), self.scale_out_cap(F.tanhshrink(e[:,:,3:]).transpose(1,2)).transpose(1,2)), 2), \
                                h.detach()
        else:
            return torch.cat((torch.sigmoid(e[:,:,:1]), self.scale_out(F.tanhshrink(e)[:,:,1:].transpose(1,2)).transpose(1,2)), 2), \
                    h.detach()

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:
                return

        self.apply(_remove_weight_norm)


class GRU_WAVE_DECODER_DUALGRU_COMPACT_MBAND_CF(nn.Module):
    """
    GRU, wave, decoder, DualGPU, Compact, Multinand, CF
    """
    def __init__(self, feat_dim=80, upsampling_factor=120, hidden_units=640, hidden_units_2=32, n_quantize=65536, s_dim=320, seg_conv_flag=True,
            kernel_size=7, dilation_size=1, do_prob=0, causal_conv=False, use_weight_norm=True, lpc=6, remove_scale_in_weight_norm=True,
                right_size=2, n_bands=5, excit_dim=0, pad_first=False, mid_out_flag=True, red_dim=None, spk_dim=None, res_gru=None, frm_upd_flag=False,
                    scale_in_aux_dim=None, n_spk=None, scale_in_flag=True, mid_dim=None, aux_dim=None, res_flag=False, res_smpl_flag=False, conv_in_flag=False,
                        emb_flag=False):
        super(GRU_WAVE_DECODER_DUALGRU_COMPACT_MBAND_CF, self).__init__()
        self.feat_dim = feat_dim
        self.in_dim = self.feat_dim
        self.n_quantize = n_quantize
        self.n_bands = n_bands
        self.cf_dim = int(np.sqrt(self.n_quantize))
        self.out_dim = self.n_quantize
        self.upsampling_factor = upsampling_factor // self.n_bands
        self.hidden_units = hidden_units
        self.hidden_units_2 = hidden_units_2
        self.kernel_size = kernel_size
        self.dilation_size = dilation_size
        self.do_prob = do_prob
        self.causal_conv = causal_conv
        self.seg_conv_flag = seg_conv_flag
        self.s_dim = s_dim
        self.wav_dim = 64
        self.wav_dim_bands = self.wav_dim * self.n_bands
        self.use_weight_norm = use_weight_norm
        self.lpc = lpc
        self.right_size = right_size
        self.excit_dim = excit_dim
        self.pad_first = pad_first
        self.mid_out_flag = mid_out_flag
        self.mid_dim = mid_dim
        if self.mid_dim is None:
            if self.mid_out_flag:
                if self.cf_dim > 32:
                    self.mid_out = 32
                else:
                    self.mid_out = self.cf_dim
            else:
                self.mid_out = None
        else:
            self.mid_out = mid_dim
        self.red_dim = red_dim
        self.scale_in_aux_dim = scale_in_aux_dim
        self.scale_in_flag = scale_in_flag
        self.n_spk = n_spk
        self.aux_dim = aux_dim
        self.spk_dim = spk_dim
        self.res_flag = res_flag
        self.res_smpl_flag = res_smpl_flag
        self.conv_in_flag = conv_in_flag
        self.res_gru = res_gru
        self.frm_upd_flag = frm_upd_flag
        self.remove_scale_in_weight_norm = remove_scale_in_weight_norm
        self.emb_flag = emb_flag

        # Norm. layer
        if self.scale_in_flag:
            if self.scale_in_aux_dim is not None:
                self.scale_in = nn.Conv1d(self.scale_in_aux_dim, self.scale_in_aux_dim, 1)
            else:
                self.scale_in = nn.Conv1d(self.in_dim, self.in_dim, 1)

        # Conv. layers
        if self.right_size <= 0:
            if not self.causal_conv:
                self.conv = TwoSidedDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = self.conv.padding
            else:
                self.conv = CausalDilConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = 0
        else:
            self.conv = SkewedConv1d(in_dim=self.in_dim, seg_conv=self.seg_conv_flag, kernel_size=self.kernel_size,
                                        right_size=self.right_size, pad_first=self.pad_first)
            self.pad_left = self.conv.left_size
            self.pad_right = self.conv.right_size
        if not self.seg_conv_flag:
            conv_s_c = [nn.Conv1d(self.conv.out_dim, self.s_dim, 1), nn.ReLU()]
        else:
            conv_s_c = [nn.Conv1d(self.in_dim*self.conv.rec_field, self.s_dim, 1), nn.ReLU()]
        self.conv_s_c = nn.Sequential(*conv_s_c)

        if self.do_prob > 0:
            self.drop = nn.Dropout(p=self.do_prob)

        self.embed_c_wav = nn.Embedding(self.cf_dim, self.wav_dim)
        self.embed_f_wav = nn.Embedding(self.cf_dim, self.wav_dim)

        # `Sparse GRU` & `Dense GRU`?
        # GRU layer(s) coarse
        self.gru = nn.GRU(self.s_dim+self.wav_dim_bands*2, self.hidden_units, 1, batch_first=True)
        self.gru_2 = nn.GRU(self.s_dim+self.hidden_units, self.hidden_units_2, 1, batch_first=True)

        # `DualFC` for coarse bits
        self.out = DualFC_(self.hidden_units_2, self.cf_dim, self.lpc, n_bands=self.n_bands, mid_out=self.mid_out)

        # `Dence GRU` for fine bits
        self.gru_f = nn.GRU(self.s_dim+self.wav_dim_bands+self.hidden_units_2, self.hidden_units_2, 1, batch_first=True)

        # `DualFC` for fine bits
        self.out_f = DualFC_(self.hidden_units_2, self.cf_dim, self.lpc, n_bands=self.n_bands, mid_out=self.mid_out)

        # Prev logits if using data-driven lpc
        if self.lpc > 0:
            self.logits = nn.Embedding(self.cf_dim, self.cf_dim)
            logits_param = torch.empty(self.cf_dim, self.cf_dim).fill_(0)
            for i in range(self.cf_dim):
                logits_param[i,i] = 1
            self.logits.weight = torch.nn.Parameter(logits_param)
            if self.emb_flag:
                self.logits_c = EmbeddingOne(self.cf_dim, 1)
                self.logits_f = EmbeddingOne(self.cf_dim, 1)

        # apply weight norm
        if self.use_weight_norm:
            self.apply_weight_norm()
            if self.scale_in_flag and self.remove_scale_in_weight_norm:
                torch.nn.utils.remove_weight_norm(self.scale_in)
        else:
            self.apply(initialize)

    def forward(self, c, x_c_prev, x_f_prev, x_c, h=None, h_2=None, h_f=None, do=False, x_c_lpc=None, x_f_lpc=None,
            outpad_left=None, outpad_right=None, ret_mid_feat=False, ret_mid_smpl=False):
        """
        Forward pass for single sample-step.
        
        Args:
            x_c_prev: Sample coarse t-1
            x_f_prev: Sample fine   t-1

            h: Hidden state of Sparse GRU
            h_2: Hidden state of Dense GRU
            h_f: Hidden state of Fine GRU
            h_spk: ※ not used
        Returns:
            Updated hidden states (h, h_2, h_f) are returned.
        """
        # Input
        if self.scale_in_flag:
            if self.do_prob > 0 and do:
                if not ret_mid_feat:
                    conv = self.drop(torch.repeat_interleave(self.conv_s_c(self.conv(self.scale_in(c.transpose(1,2)))).transpose(1,2),self.upsampling_factor,dim=1))
                else:
                    if outpad_left is not None:
                        if outpad_right is not None and outpad_right > 0:
                            seg_conv = self.conv(self.scale_in(c.transpose(1,2)))[:,:,outpad_left:-outpad_right]
                        else:
                            seg_conv = self.conv(self.scale_in(c.transpose(1,2)))[:,:,outpad_left:]
                    elif outpad_right is not None and outpad_right > 0:
                        seg_conv = self.conv(self.scale_in(c.transpose(1,2)))[:,:,:-outpad_right]
                    else:
                        seg_conv = self.conv(self.scale_in(c.transpose(1,2)))
                    conv_sc = self.conv_s_c(seg_conv).transpose(1,2)
                    conv = self.drop(torch.repeat_interleave(conv_sc,self.upsampling_factor,dim=1))
            else:
                if not ret_mid_feat:
                    conv = torch.repeat_interleave(self.conv_s_c(self.conv(self.scale_in(c.transpose(1,2)))).transpose(1,2),self.upsampling_factor,dim=1)
                else:
                    if outpad_left is not None:
                        if outpad_right is not None and outpad_right > 0:
                            seg_conv = self.conv(self.scale_in(c.transpose(1,2)))[:,:,outpad_left:-outpad_right]
                        else:
                            seg_conv = self.conv(self.scale_in(c.transpose(1,2)))[:,:,outpad_left:]
                    elif outpad_right is not None and outpad_right > 0:
                        seg_conv = self.conv(self.scale_in(c.transpose(1,2)))[:,:,:-outpad_right]
                    else:
                        seg_conv = self.conv(self.scale_in(c.transpose(1,2)))
                    conv_sc = self.conv_s_c(seg_conv).transpose(1,2)
                    conv = torch.repeat_interleave(conv_sc,self.upsampling_factor,dim=1)
        else:
            if self.do_prob > 0 and do:
                conv = self.drop(torch.repeat_interleave(self.conv_s_c(self.conv(c.transpose(1,2))).transpose(1,2),self.upsampling_factor,dim=1))
            else:
                conv = torch.repeat_interleave(self.conv_s_c(self.conv(c.transpose(1,2))).transpose(1,2),self.upsampling_factor,dim=1)

        # Sparse GRU
        # (features, embedding_coarse, embedding_fine) => (out, h)
        if x_c_prev.shape[1] < conv.shape[1]:
            conv = conv[:,:x_c_prev.shape[1]]
        if h is not None:
            out, h = self.gru(torch.cat((conv, self.embed_c_wav(x_c_prev).reshape(x_c_prev.shape[0], x_c_prev.shape[1], -1),
                        self.embed_f_wav(x_f_prev).reshape(x_f_prev.shape[0], x_f_prev.shape[1], -1)), 2), h) # B x T x C -> B x C x T -> B x T x C
        else:
            out, h = self.gru(torch.cat((conv, self.embed_c_wav(x_c_prev).reshape(x_c_prev.shape[0], x_c_prev.shape[1], -1),
                        self.embed_f_wav(x_f_prev).reshape(x_f_prev.shape[0], x_f_prev.shape[1], -1)), 2))

        # Dense GRU
        # (features, out_GRU_sparse) => (out_GRU_dense, h_2)
        if h_2 is not None:
            out_2, h_2 = self.gru_2(torch.cat((conv, out), 2), h_2) # B x T x C -> B x C x T -> B x T x C
        else:
            out_2, h_2 = self.gru_2(torch.cat((conv, out), 2))

        # GRU_fine
        # (features, embedding_coarse, out_GRU_dense) => (out_GRU_fine, h_f)
        if h_f is not None:
            out_f, h_f = self.gru_f(torch.cat((conv, self.embed_c_wav(x_c).reshape(x_c.shape[0], x_c.shape[1], -1), out_2), 2), h_f)
        else:
            out_f, h_f = self.gru_f(torch.cat((conv, self.embed_c_wav(x_c).reshape(x_c.shape[0], x_c.shape[1], -1), out_2), 2))

        # output
        if self.lpc > 0:
            # (out_GRU_dense) => (signs_c, scales_c, lin_c, logits_c)
            # (out_GRU_fine)  => (signs_f, scales_f, lin_f, logits_f)
            signs_c, scales_c, logits_c = self.out(out_2.transpose(1,2))
            signs_f, scales_f, logits_f = self.out_f(out_f.transpose(1,2))
            # B x T x x n_bands x K, B x T x n_bands x K and B x T x n_bands x 32
            # x_lpc B x T_lpc x n_bands --> B x T x n_bands x K --> B x T x n_bands x K x 32
            # unfold put new dimension on the last
            if not ret_mid_feat:
                if self.emb_flag:
                    x_c_lpc = x_c_lpc.unfold(1, self.lpc, 1) # B x T x n_bands --> B x T x n_bands x K
                    x_f_lpc = x_f_lpc.unfold(1, self.lpc, 1)
                    #lpc_c = (signs_c*scales_c).flip(-1).unsqueeze(-1)
                    #lpc_f = (signs_f*scales_f).flip(-1).unsqueeze(-1)
                    #logging.info(lpc_c.mean(2).mean(1).mean(0)[:,0])
                    #logging.info(lpc_f.mean(2).mean(1).mean(0)[:,0])
                    #logging.info(signs_c.flip(-1).mean(2).mean(1).mean(0))
                    #logging.info(scales_c.flip(-1).mean(2).mean(1).mean(0))
                    return torch.clamp(logits_c + torch.sum(self.logits(x_c_lpc)*(signs_c*scales_c).flip(-1).unsqueeze(-1)*self.logits_c(x_c_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), \
                            torch.clamp(logits_f + torch.sum(self.logits(x_f_lpc)*(signs_f*scales_f).flip(-1).unsqueeze(-1)*self.logits_f(x_f_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), \
                                    h.detach(), h_2.detach(), h_f.detach()
                else:
                    return torch.clamp(logits_c + torch.sum((signs_c*scales_c).flip(-1).unsqueeze(-1)*self.logits(x_c_lpc.unfold(1, self.lpc, 1)), 3), min=MIN_CLAMP, max=MAX_CLAMP), \
                        torch.clamp(logits_f + torch.sum((signs_f*scales_f).flip(-1).unsqueeze(-1)*self.logits(x_f_lpc.unfold(1, self.lpc, 1)), 3), min=MIN_CLAMP, max=MAX_CLAMP), h.detach(), h_2.detach(), h_f.detach()
            else:
                if not ret_mid_smpl:
                    return torch.clamp(logits_c + torch.sum((signs_c*scales_c).flip(-1).unsqueeze(-1)*self.logits(x_c_lpc.unfold(1, self.lpc, 1)), 3), min=MIN_CLAMP, max=MAX_CLAMP), \
                        torch.clamp(logits_f + torch.sum((signs_f*scales_f).flip(-1).unsqueeze(-1)*self.logits(x_f_lpc.unfold(1, self.lpc, 1)), 3), min=MIN_CLAMP, max=MAX_CLAMP), \
                            seg_conv.transpose(1,2), conv_sc, h.detach(), h_2.detach(), h_f.detach()
                else:
                    return logits_c + torch.sum((signs_c*scales_c).flip(-1).unsqueeze(-1)*self.logits(x_c_lpc.unfold(1, self.lpc, 1)), 3), \
                        logits_f + torch.sum((signs_f*scales_f).flip(-1).unsqueeze(-1)*self.logits(x_f_lpc.unfold(1, self.lpc, 1)), 3), \
                            seg_conv.transpose(1,2), conv_sc, out, out_2, out_f, signs_c, scales_c, logits_c, signs_f, scales_f, logits_f, h.detach(), h_2.detach(), h_f.detach()
            # B x T x n_bands x 32
        else:
            logits_c = self.out(out_2.transpose(1,2))
            logits_f = self.out_f(out_f.transpose(1,2))
            return torch.clamp(logits_c, min=MIN_CLAMP, max=MAX_CLAMP), torch.clamp(logits_f, min=MIN_CLAMP, max=MAX_CLAMP), h.detach(), h_2.detach(), h_f.detach()

    def gen_mid_feat(self, c):
        # Input
        if self.scale_in_flag:
            seg_conv = self.conv(self.scale_in(c.transpose(1,2)))
        else:
            seg_conv = self.conv(c.transpose(1,2))
        conv_sc = self.conv_s_c(seg_conv).transpose(1,2)

        return seg_conv.transpose(1,2), conv_sc

    def gen_mid_feat_smpl(self, c, x_c_prev, x_f_prev, x_c, h=None, h_2=None, h_f=None, x_c_lpc=None, x_f_lpc=None):
        # Input
        if self.scale_in_flag:
            seg_conv = self.conv(self.scale_in(c.transpose(1,2)))
        else:
            seg_conv = self.conv(c.transpose(1,2))
        conv_sc = self.conv_s_c(seg_conv).transpose(1,2)
        conv = torch.repeat_interleave(conv_sc,self.upsampling_factor,dim=1)

        # GRU1
        if x_c_prev.shape[1] < conv.shape[1]:
            conv = conv[:,:x_c_prev.shape[1]]
        if h is not None:
            out, h = self.gru(torch.cat((conv, self.embed_c_wav(x_c_prev).reshape(x_c_prev.shape[0], x_c_prev.shape[1], -1),
                        self.embed_f_wav(x_f_prev).reshape(x_f_prev.shape[0], x_f_prev.shape[1], -1)), 2), h) # B x T x C -> B x C x T -> B x T x C
        else:
            out, h = self.gru(torch.cat((conv, self.embed_c_wav(x_c_prev).reshape(x_c_prev.shape[0], x_c_prev.shape[1], -1),
                        self.embed_f_wav(x_f_prev).reshape(x_f_prev.shape[0], x_f_prev.shape[1], -1)), 2))

        # GRU2
        if h_2 is not None:
            out_2, h_2 = self.gru_2(torch.cat((conv, out), 2), h_2) # B x T x C -> B x C x T -> B x T x C
        else:
            out_2, h_2 = self.gru_2(torch.cat((conv, out), 2))

        # GRU_fine
        if h_f is not None:
            out_f, h_f = self.gru_f(torch.cat((conv, self.embed_c_wav(x_c).reshape(x_c.shape[0], x_c.shape[1], -1), out_2), 2), h_f)
        else:
            out_f, h_f = self.gru_f(torch.cat((conv, self.embed_c_wav(x_c).reshape(x_c.shape[0], x_c.shape[1], -1), out_2), 2))

        # output
        if self.lpc > 0:
            signs_c, scales_c, logits_c = self.out(out_2.transpose(1,2))
            signs_f, scales_f, logits_f = self.out_f(out_f.transpose(1,2))
            # B x T x x n_bands x K, B x T x n_bands x K and B x T x n_bands x 32
            # x_lpc B x T_lpc x n_bands --> B x T x n_bands x K --> B x T x n_bands x K x 32
            # unfold put new dimension on the last

            return seg_conv.transpose(1,2), conv_sc, out, out_2, out_f, signs_c, scales_c, logits_c, signs_f, scales_f, logits_f, \
                    logits_c + torch.sum((signs_c*scales_c).flip(-1).unsqueeze(-1)*self.logits(x_c_lpc.unfold(1, self.lpc, 1)), 3), \
                        logits_f + torch.sum((signs_f*scales_f).flip(-1).unsqueeze(-1)*self.logits(x_f_lpc.unfold(1, self.lpc, 1)), 3), h, h_2, h_f
        else:
            logits_c = self.out(out_2.transpose(1,2))
            logits_f = self.out_f(out_f.transpose(1,2))

            return seg_conv.transpose(1,2), conv_sc, out, out_2, out_f, logits_c, logits_f, h, h_2, h_f

    def generate(self, c, intervals=25, spk_code=None, spk_aux=None, aux=None, outpad_left=None, outpad_right=None, pad_first=True):
        """
        Args:
            c ((B, T, F)?): Conditioning feature sequence.
            intervals (number): Performance test config, currently no meaning.
            spk_code
            spk_aux
            aux
            outpad_left
            outpad_right
            pad_first (bool): Padding setting
        Returns:
            (B, Subband, T) - A batch of subband waveforms
        """
        # Performance test utilities
        start = time.time()
        time_sample = []
        # intervals: log every frame intervals (25 frames = 0.25 sec for 10 ms shift)
        intervals *= self.upsampling_factor
        # /Performance test utilities

        c_pad = (self.n_quantize // 2) // self.cf_dim
        f_pad = (self.n_quantize // 2) % self.cf_dim
        B = c.shape[0] # n_Batch?

        # Input
        if pad_first and outpad_left is None and outpad_right is None:
            c = F.pad(c.transpose(1,2), (self.pad_left,self.pad_right), "replicate").transpose(1,2)
        if self.scale_in_flag:
            c = self.conv_s_c(self.conv(self.scale_in(c.transpose(1,2)))).transpose(1,2)
        else:
            c = self.conv_s_c(self.conv(c.transpose(1,2))).transpose(1,2)

        if self.lpc > 0:
            x_c_lpc = torch.empty(B,1,self.n_bands,self.lpc).cuda().fill_(c_pad).long() # B x 1 x n_bands x K
            x_f_lpc = torch.empty(B,1,self.n_bands,self.lpc).cuda().fill_(f_pad).long() # B x 1 x n_bands x K

        # Sample sequence length.
        T = c.shape[1]*self.upsampling_factor

        c_f = c[:,:1]
        out, h = self.gru(torch.cat((c_f,self.embed_c_wav(torch.empty(B,1,self.n_bands).cuda().fill_(c_pad).long()).reshape(B,1,-1),
                                        self.embed_f_wav(torch.empty(B,1,self.n_bands).cuda().fill_(f_pad).long()).reshape(B,1,-1)),2))
        out, h_2 = self.gru_2(torch.cat((c_f,out), 2))
        if self.lpc > 0:
            # coarse part
            signs_c, scales_c, logits_c = self.out(out.transpose(1,2)) # B x 1 x n_bands x K or 32
            if self.emb_flag:
                dist = OneHotCategorical(F.softmax(torch.clamp(logits_c + torch.sum(self.logits(x_c_lpc)*(signs_c*scales_c).unsqueeze(-1)\
                            *self.logits_c(x_c_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                            #*torch.tanh(self.logits_sgns_c(x_c_lpc))*torch.exp(self.logits_mags_c(x_c_lpc)), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
            else:
                dist = OneHotCategorical(F.softmax(torch.clamp(logits_c + torch.sum((signs_c*scales_c).unsqueeze(-1)*self.logits(x_c_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
            # B x 1 x n_bands x 32, B x 1 x n_bands x K x 32 --> B x 1 x n_bands x 2 x 32
            x_c_out = x_c_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands
            x_c_lpc[:,:,:,1:] = x_c_lpc[:,:,:,:-1]
            x_c_lpc[:,:,:,0] = x_c_wav
            # fine part
            embed_x_c_wav = self.embed_c_wav(x_c_wav).reshape(B,1,-1)
            out, h_f = self.gru_f(torch.cat((c_f, embed_x_c_wav, out), 2))
            signs_f, scales_f, logits_f = self.out_f(out.transpose(1,2)) # B x 1 x n_bands x K or 32
            if self.emb_flag:
                dist = OneHotCategorical(F.softmax(torch.clamp(logits_f + torch.sum(self.logits(x_f_lpc)*(signs_f*scales_f).unsqueeze(-1)\
                            *self.logits_f(x_f_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                            #*torch.tanh(self.logits_sgns_f(x_f_lpc))*torch.exp(self.logits_mags_f(x_f_lpc)), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
            else:
                dist = OneHotCategorical(F.softmax(torch.clamp(logits_f + torch.sum((signs_f*scales_f).unsqueeze(-1)*self.logits(x_f_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
            x_f_out = x_f_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands
            x_f_lpc[:,:,:,1:] = x_f_lpc[:,:,:,:-1]
            x_f_lpc[:,:,:,0] = x_f_wav
        else:
            # coarse part
            dist = OneHotCategorical(F.softmax(torch.clamp(self.out(out.transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
            x_c_out = x_c_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands
            # fine part
            embed_x_c_wav = self.embed_c_wav(x_c_wav).reshape(B,1,-1)
            out, h_f = self.gru_f(torch.cat((c_f, embed_x_c_wav, out), 2))
            dist = OneHotCategorical(F.softmax(torch.clamp(self.out_f(out.transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
            x_f_out = x_f_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands

        time_sample.append(time.time()-start)
        if self.lpc > 0:
            # Single sample generation step
            for t in range(1,T):
                start_sample = time.time()

                if t % self.upsampling_factor  == 0:
                    idx_t_f = t//self.upsampling_factor
                    c_f = c[:,idx_t_f:idx_t_f+1]

                out, h = self.gru(torch.cat((c_f, embed_x_c_wav, self.embed_f_wav(x_f_wav).reshape(B,1,-1)),2), h)
                out, h_2 = self.gru_2(torch.cat((c_f,out), 2), h_2)

                # coarse part
                signs_c, scales_c, logits_c = self.out(out.transpose(1,2)) # B x 1 x n_bands x K or 32
                if self.emb_flag:
                    dist = OneHotCategorical(F.softmax(torch.clamp(logits_c + torch.sum(self.logits(x_c_lpc)*(signs_c*scales_c).unsqueeze(-1)\
                                *self.logits_c(x_c_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                                #*torch.tanh(self.logits_sgns_c(x_c_lpc))*torch.exp(self.logits_mags_c(x_c_lpc)), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                else:
                    dist = OneHotCategorical(F.softmax(torch.clamp(logits_c + torch.sum((signs_c*scales_c).unsqueeze(-1)*self.logits(x_c_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                x_c_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands x 2
                x_c_out = torch.cat((x_c_out, x_c_wav), 1) # B x t+1 x n_bands
                x_c_lpc[:,:,:,1:] = x_c_lpc[:,:,:,:-1]
                x_c_lpc[:,:,:,0] = x_c_wav

                # fine part
                embed_x_c_wav = self.embed_c_wav(x_c_wav).reshape(B,1,-1)
                out, h_f = self.gru_f(torch.cat((c_f, embed_x_c_wav, out), 2), h_f)
                signs_f, scales_f, logits_f = self.out_f(out.transpose(1,2)) # B x 1 x n_bands x K or 32
                if self.emb_flag:
                    dist = OneHotCategorical(F.softmax(torch.clamp(logits_f + torch.sum(self.logits(x_f_lpc)*(signs_f*scales_f).unsqueeze(-1)\
                                *self.logits_f(x_f_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                                #*torch.tanh(self.logits_sgns_f(x_f_lpc))*torch.exp(self.logits_mags_f(x_f_lpc)), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                else:
                    dist = OneHotCategorical(F.softmax(torch.clamp(logits_f + torch.sum((signs_f*scales_f).unsqueeze(-1)*self.logits(x_f_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                x_f_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands
                x_f_out = torch.cat((x_f_out, x_f_wav), 1) # B x t+1 x n_bands
                x_f_lpc[:,:,:,1:] = x_f_lpc[:,:,:,:-1]
                x_f_lpc[:,:,:,0] = x_f_wav

                time_sample.append(time.time()-start_sample)
                if (t + 1) % intervals == 0:
                    logging.info("%d/%d estimated time = %.6f sec (%.6f sec / sample)" % (
                        (t + 1), T,
                        ((T - t - 1) / intervals) * (time.time() - start),
                        (time.time() - start) / intervals))
                    start = time.time()
        else:
            for t in range(1,T):
                start_sample = time.time()

                if t % self.upsampling_factor  == 0:
                    idx_t_f = t//self.upsampling_factor
                    c_f = c[:,idx_t_f:idx_t_f+1]

                out, h = self.gru(torch.cat((c_f, embed_x_c_wav, self.embed_f_wav(x_f_wav).reshape(B,1,-1)),2), h)
                out, h_2 = self.gru_2(torch.cat((c_f,out),2), h_2)

                # coarse part
                dist = OneHotCategorical(F.softmax(torch.clamp(self.out(out.transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                x_c_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands
                x_c_out = torch.cat((x_c_out, x_c_wav), 1) # B x t+1 x n_bands

                # fine part
                embed_x_c_wav = self.embed_c_wav(x_c_wav).reshape(B,1,-1)
                out, h_f = self.gru_f(torch.cat((c_f, embed_x_c_wav, out), 2), h_f)
                dist = OneHotCategorical(F.softmax(torch.clamp(self.out_f(out.transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                x_f_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands
                x_f_out = torch.cat((x_f_out, x_f_wav), 1) # B x t+1 x n_bands

                time_sample.append(time.time()-start_sample)
                if (t + 1) % intervals == 0:
                    logging.info("%d/%d estimated time = %.6f sec (%.6f sec / sample)" % (
                        (t + 1), T,
                        ((T - t - 1) / intervals) * (time.time() - start),
                        (time.time() - start) / intervals))
                    start = time.time()

        # Performance report
        time_sample = np.array(time_sample)
        logging.info("average time / sample = %.6f sec (%ld samples) [%.3f kHz/s]" % \
                        (np.mean(time_sample), len(time_sample), 1.0/(1000*np.mean(time_sample))))
        logging.info("average throughput / sample = %.6f sec (%ld samples * %ld) [%.3f kHz/s]" % \
                        (np.sum(time_sample)/(len(time_sample)*c.shape[0]), len(time_sample), c.shape[0], \
                            len(time_sample)*c.shape[0]/(1000*np.sum(time_sample))))
        # /Performance report

        if self.n_quantize == 65536:
            return ((x_c_out*self.cf_dim+x_f_out).transpose(1,2).float() - 32768.0) / 32768.0 # B x T x n_bands --> B x n_bands x T
        else:
            return decode_mu_law_torch((x_c_out*self.cf_dim+x_f_out).transpose(1,2).float(), mu=self.n_quantize) # B x T x n_bands --> B x n_bands x T

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d) \
                or isinstance(m, torch.nn.ConvTranspose2d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d) \
                    or isinstance(m, torch.nn.ConvTranspose2d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:  # this module didn't have weight norm
                return

        self.apply(_remove_weight_norm)


class GRU_WAVE_DECODER_DUALGRU_COMPACT_MBAND(nn.Module):
    def __init__(self, feat_dim=80, upsampling_factor=120, hidden_units=640, hidden_units_2=32, n_quantize=512,
            lpc=6, kernel_size=7, dilation_size=1, do_prob=0, causal_conv=False, use_weight_norm=True,
                right_size=0, n_bands=5, pad_first=False):
        super(GRU_WAVE_DECODER_DUALGRU_COMPACT_MBAND, self).__init__()
        self.feat_dim = feat_dim
        self.in_dim = self.feat_dim
        self.n_quantize = n_quantize
        self.out_dim = self.n_quantize
        self.n_bands = n_bands
        self.upsampling_factor = upsampling_factor // self.n_bands
        self.hidden_units = hidden_units
        self.hidden_units_2 = hidden_units_2
        self.kernel_size = kernel_size
        self.dilation_size = dilation_size
        self.do_prob = do_prob
        self.causal_conv = causal_conv
        self.s_dim = 320
        #self.wav_dim = self.s_dim // self.n_bands
        self.wav_dim = 64
        self.mid_out = 32
        self.wav_dim_bands = self.wav_dim * self.n_bands
        self.use_weight_norm = use_weight_norm
        self.lpc = lpc
        self.right_size = right_size
        self.pad_first = pad_first

        self.scale_in = nn.Conv1d(self.in_dim, self.in_dim, 1)

        # Conv. layers
        if self.right_size <= 0:
            if not self.causal_conv:
                self.conv = TwoSidedDilConv1d(in_dim=self.in_dim, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = self.conv.padding
            else:
                self.conv = CausalDilConv1d(in_dim=self.in_dim, kernel_size=self.kernel_size,
                                            layers=self.dilation_size, pad_first=self.pad_first)
                self.pad_left = self.conv.padding
                self.pad_right = 0
        else:
            self.conv = SkewedConv1d(in_dim=self.in_dim, kernel_size=self.kernel_size,
                                        right_size=self.right_size, pad_first=self.pad_first)
            self.pad_left = self.conv.left_size
            self.pad_right = self.conv.right_size
        conv_s_c = [nn.Conv1d(self.in_dim*self.conv.rec_field, self.s_dim, 1), nn.ReLU()]
        self.conv_s_c = nn.Sequential(*conv_s_c)
        if self.do_prob > 0:
            self.drop = nn.Dropout(p=self.do_prob)
        self.embed_wav = nn.Embedding(self.n_quantize, self.wav_dim)

        # GRU layer(s)
        self.gru = nn.GRU(self.s_dim+self.wav_dim_bands, self.hidden_units, 1, batch_first=True)
        self.gru_2 = nn.GRU(self.s_dim+self.hidden_units, self.hidden_units_2, 1, batch_first=True)

        # Output layers
        self.out = DualFC(self.hidden_units_2, self.n_quantize, self.lpc, n_bands=self.n_bands, mid_out=self.mid_out)

        # Prev logits if using data-driven lpc
        if self.lpc > 0:
            self.logits = nn.Embedding(self.n_quantize, self.n_quantize)
            logits_param = torch.empty(self.n_quantize, self.n_quantize).fill_(0)
            for i in range(self.n_quantize):
                logits_param[i,i] = 1
            self.logits.weight = torch.nn.Parameter(logits_param)

        # apply weight norm
        if self.use_weight_norm:
            self.apply_weight_norm()
            torch.nn.utils.remove_weight_norm(self.scale_in)
        else:
            self.apply(initialize)

    def forward(self, c, x_prev, h=None, h_2=None, h_spk=None, do=False, x_lpc=None):
        # Input
        if self.do_prob > 0 and do:
            conv = self.drop(torch.repeat_interleave(self.conv_s_c(self.conv(self.scale_in(c.transpose(1,2)))).transpose(1,2),self.upsampling_factor,dim=1))
        else:
            conv = torch.repeat_interleave(self.conv_s_c(self.conv(self.scale_in(c.transpose(1,2)))).transpose(1,2),self.upsampling_factor,dim=1)

        # GRU1
        if h is not None:
            out, h = self.gru(torch.cat((conv, self.embed_wav(x_prev).reshape(x_prev.shape[0], x_prev.shape[1], -1)),2), h) # B x T x C -> B x C x T -> B x T x C
        else:
            out, h = self.gru(torch.cat((conv, self.embed_wav(x_prev).reshape(x_prev.shape[0], x_prev.shape[1], -1)),2))

        # GRU2
        if h_2 is not None:
            out, h_2 = self.gru_2(torch.cat((conv, out),2), h_2) # B x T x C -> B x C x T -> B x T x C
        else:
            out, h_2 = self.gru_2(torch.cat((conv, out),2))

        # output
        if self.lpc > 0:
            signs, scales, logits = self.out(out.transpose(1,2)) # B x T x x n_bands x K, B x T x n_bands x K and B x T x n_bands x 32
            # x_lpc B x T_lpc x n_bands --> B x T x n_bands x K --> B x T x n_bands x K x 32
            # unfold put new dimension on the last
            return torch.clamp(logits + torch.sum((signs*scales).flip(-1).unsqueeze(-1)*self.logits(x_lpc.unfold(1, self.lpc, 1)), 3), min=MIN_CLAMP, max=MAX_CLAMP), h.detach(), h_2.detach()
            # B x T x n_bands x 32
        else:
            return torch.clamp(self.out(out.transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP), h.detach(), h_2.detach()

    def generate(self, c, intervals=4000):
        start = time.time()
        time_sample = []
        intervals /= self.n_bands

        upsampling_factor = self.upsampling_factor

        B = c.shape[0]
        c = F.pad(c.transpose(1,2), (self.pad_left,self.pad_right), "replicate").transpose(1,2)
        c = self.conv_s_c(self.conv(self.scale_in(c.transpose(1,2)))).transpose(1,2)
        if self.lpc > 0:
            x_lpc = torch.empty(B,1,self.n_bands,self.lpc).cuda().fill_(self.n_quantize // 2).long() # B x 1 x n_bands x K
        T = c.shape[1]*upsampling_factor

        c_f = c[:,:1]
        out, h = self.gru(torch.cat((c_f,self.embed_wav(torch.empty(B,1,self.n_bands).cuda().fill_(self.n_quantize//2).long()).reshape(B,1,-1)),2))
        out, h_2 = self.gru_2(torch.cat((c_f,out),2))
        if self.lpc > 0:
            signs, scales, logits = self.out(out.transpose(1,2)) # B x T x C -> B x C x T -> B x T x C
            dist = OneHotCategorical(F.softmax(torch.clamp(logits + torch.sum((signs*scales).unsqueeze(-1)*self.logits(x_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1)) # B x 1 x n_bands x 32, B x 1 x n_bands x K x 32 --> B x 1 x n_bands x 32
            x_out = x_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands
            x_lpc[:,:,:,1:] = x_lpc[:,:,:,:-1]
            x_lpc[:,:,:,0] = x_wav
        else:
            dist = OneHotCategorical(F.softmax(torch.clamp(self.out(out.transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
            x_out = x_wav = dist.sample().argmax(dim=-1)

        time_sample.append(time.time()-start)
        if self.lpc > 0:
            for t in range(1,T):
                start_sample = time.time()

                if t % upsampling_factor  == 0:
                    idx_t_f = t//upsampling_factor
                    c_f = c[:,idx_t_f:idx_t_f+1]

                out, h = self.gru(torch.cat((c_f, self.embed_wav(x_wav).reshape(B,1,-1)),2), h)
                out, h_2 = self.gru_2(torch.cat((c_f,out),2), h_2)

                signs, scales, logits = self.out(out.transpose(1,2)) # B x T x C -> B x C x T -> B x T x C
                dist = OneHotCategorical(F.softmax(torch.clamp(logits + torch.sum((signs*scales).unsqueeze(-1)*self.logits(x_lpc), 3), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1)) # B x 1 x n_bands x 32, B x 1 x n_bands x K x 32 --> B x 1 x n_bands x 32
                x_wav = dist.sample().argmax(dim=-1) # B x 1 x n_bands
                x_out = torch.cat((x_out, x_wav), 1) # B x t+1 x n_bands
                x_lpc[:,:,:,1:] = x_lpc[:,:,:,:-1]
                x_lpc[:,:,:,0] = x_wav

                time_sample.append(time.time()-start_sample)
                if (t + 1) % intervals == 0:
                    logging.info("%d/%d estimated time = %.6f sec (%.6f sec / sample)" % (
                        (t + 1), T,
                        ((T - t - 1) / intervals) * (time.time() - start),
                        (time.time() - start) / intervals))
                    start = time.time()
        else:
            for t in range(1,T):
                start_sample = time.time()

                if t % upsampling_factor  == 0:
                    idx_t_f = t//upsampling_factor
                    c_f = c[:,idx_t_f:idx_t_f+1]

                out, h = self.gru(torch.cat((c_f, self.embed_wav(x_wav).reshape(B,1,-1)),2), h)
                out, h_2 = self.gru_2(torch.cat((c_f,out),2), h_2)

                dist = OneHotCategorical(F.softmax(torch.clamp(self.out(out.transpose(1,2)), min=MIN_CLAMP, max=MAX_CLAMP), dim=-1))
                x_wav = dist.sample().argmax(dim=-1)
                x_out = torch.cat((x_out, x_wav), 1)

                time_sample.append(time.time()-start_sample)
                if (t + 1) % intervals == 0:
                    logging.info("%d/%d estimated time = %.6f sec (%.6f sec / sample)" % (
                        (t + 1), T,
                        ((T - t - 1) / intervals) * (time.time() - start),
                        (time.time() - start) / intervals))
                    start = time.time()

        time_sample = np.array(time_sample)
        logging.info("average time / sample = %.6f sec (%ld samples) [%.3f kHz/s]" % \
                        (np.mean(time_sample), len(time_sample), 1.0/(1000*np.mean(time_sample))))
        logging.info("average throughput / sample = %.6f sec (%ld samples * %ld) [%.3f kHz/s]" % \
                        (np.sum(time_sample)/(len(time_sample)*c.shape[0]), len(time_sample), c.shape[0], \
                            len(time_sample)*c.shape[0]/(1000*np.sum(time_sample))))

        return decode_mu_law_torch(x_out.transpose(1,2).float(), mu=self.n_quantize) # B x T x n_bands --> B x n_bands x T

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d) \
                or isinstance(m, torch.nn.ConvTranspose2d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d) \
                    or isinstance(m, torch.nn.ConvTranspose2d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:  # this module didn't have weight norm
                return

        self.apply(_remove_weight_norm)


class ModulationSpectrumLoss(nn.Module):
    def __init__(self, fftsize=256):
        super(ModulationSpectrumLoss, self).__init__()
        self.fftsize = fftsize

    def forward(self, x, y):
        """ x : B x T x C / T x C
            y : B x T x C / T x C
            return : B, B or 1, 1 [norm, error in log10] """
        if len(x.shape) > 2: # B x T x C
            padded_x = F.pad(x, (0, 0, 0, self.fftsize-x.shape[1]), "constant", 0)
            padded_y = F.pad(y, (0, 0, 0, self.fftsize-y.shape[1]), "constant", 0)

            csp_x = torch.fft.fftn(padded_x)
            csp_y = torch.fft.fftn(padded_y)

            magsp_x = torch.abs(csp_x)
            magsp_y = torch.abs(csp_y)

            diff = magsp_y - magsp_x
            norm = LA.norm(diff, 'fro', dim=(1,2)) / LA.norm(magsp_y, 'fro', dim=(1,2)) \
                    + diff.abs().sum(-1).sum(-1) / magsp_y.sum(-1).sum(-1)
            if x.shape[1] > 1:
                log_diff = torch.log10(torch.clamp(magsp_y, min=1e-13)) - torch.log10(torch.clamp(magsp_x, min=1e-13))
                err = log_diff.abs().mean(-1).mean(-1) + (log_diff**2).mean(-1).mean(-1).sqrt()
            else:
                err = (torch.log10(torch.clamp(magsp_y, min=1e-13)) - torch.log10(torch.clamp(magsp_x, min=1e-13))).abs().mean(-1).mean(-1)
        else: # T x C
            padded_x = F.pad(x, (0, self.fftsize-x.shape[1]), "constant", 0)
            padded_y = F.pad(y, (0, self.fftsize-y.shape[1]), "constant", 0)

            csp_x = torch.fft.fftn(padded_x)
            csp_y = torch.fft.fftn(padded_y)

            magsp_x = torch.abs(csp_x)
            magsp_y = torch.abs(csp_y)

            diff = magsp_y - magsp_x
            norm = LA.norm(diff, 'fro') / LA.norm(magsp_y, 'fro') + diff.abs().sum() / magsp_y.sum()
            if x.shape[0] > 1:
                log_diff = torch.log10(torch.clamp(magsp_y, min=1e-13)) - torch.log10(torch.clamp(magsp_x, min=1e-13))
                err = log_diff.abs().mean() + (log_diff**2).mean().sqrt()
            else:
                err = (torch.log10(torch.clamp(magsp_y, min=1e-13)) - torch.log10(torch.clamp(magsp_x, min=1e-13))).abs().mean()

        return norm, err


class LaplaceWavLoss(nn.Module):
    def __init__(self):
        super(LaplaceWavLoss, self).__init__()
        self.c = 0.69314718055994530941723212145818 # ln(2)

    def forward(self, mu, log_b, target):
        if len(mu.shape) == 2: # B x T
            return torch.mean(self.c + log_b + torch.abs(target-mu)/log_b.exp(), -1) # B x 1
        else: # T
            return torch.mean(self.c + log_b + torch.abs(target-mu)/log_b.exp()) # 1


class GaussLoss(nn.Module):
    def __init__(self, dim=None):
    #def __init__(self, dim):
        super(GaussLoss, self).__init__()
        self.dim = dim
        if self.dim is not None:
            self.c = 0.91893853320467274178032973640562 #-(1/2)log(2*pi)
        else:
            self.c = self.dim*0.91893853320467274178032973640562 #-(k/2)log(2*pi)
    #    self.sum =sum

    def forward(self, mu, s, target):
        #logdet = -0.5*torch.sum(torch.log(s), -1)
        #mhndist = -0.5*torch.sum((mu-target)**2/s, -1)
        if len(mu.shape) > 2: # B x T x C --> B
            if self.dim is not None:
                return torch.mean(self.c + 0.5*(torch.sum(torch.log(s), -1) + torch.sum((mu-target)**2/s, -1)), -1)
            else:
                return torch.mean(torch.mean(self.c + 0.5*(torch.log(s) + (mu-target)**2/s), -1), -1)
        else: # T x C --> 1
            if self.dim is not None:
                return torch.mean(self.c + 0.5*(torch.sum(torch.log(s), -1) + torch.sum((mu-target)**2/s, -1)))
            else:
                return torch.mean(self.c + 0.5*(torch.log(s) + (mu-target)**2/s))


class LaplaceLoss(nn.Module):
    def __init__(self, sum=True):
        super(LaplaceLoss, self).__init__()
        self.c = 0.69314718055994530941723212145818 # ln(2)
        self.sum =sum

    def forward(self, mu, log_b, target):
        if len(mu.shape) > 2: # B x T x C
            if self.sum:
                return torch.mean(torch.sum(self.c + log_b + torch.abs(target-mu)/log_b.exp(), -1), -1) # B x 1
            else:
                return torch.mean(torch.mean(self.c + log_b + torch.abs(target-mu)/log_b.exp(), -1), -1) # B x 1
        else: # T x C
            if self.sum:
                return torch.mean(torch.sum(self.c + log_b + torch.abs(target-mu)/log_b.exp(), -1)) # 1
            else:
                return torch.mean(self.c + log_b + torch.abs(target-mu)/log_b.exp()) # 1


def laplace_logits(mu, b, disc, log_b):
    return -0.69314718055994530941723212145818 - log_b - torch.abs(disc-mu)/b # log_like (Laplace)


class LaplaceLogits(nn.Module):
    def __init__(self):
        super(LaplaceLogits, self).__init__()
        self.c = 0.69314718055994530941723212145818 # ln(2)

    def forward(self, mu, b, disc, log_b):
        return -self.c - log_b - torch.abs(disc-mu)/b # log_like (Laplace)


class CausalConv1d(nn.Module):
    """1D DILATED CAUSAL CONVOLUTION"""

    def __init__(self, in_channels, out_channels, kernel_size, dil_fact=0, bias=True):
        super(CausalConv1d, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.dil_fact = dil_fact
        self.dilation = self.kernel_size**self.dil_fact
        self.padding = self.kernel_size**(self.dil_fact+1) - self.dilation
        self.bias = bias
        self.conv = nn.Conv1d(self.in_channels, self.out_channels, self.kernel_size, padding=self.padding, \
                                dilation=self.dilation, bias=self.bias)

    def forward(self, x):
        """Forward calculation

        Arg:
            x (Variable): float tensor variable with the shape  (B x C x T)

        Return:
            (Variable): float tensor variable with the shape (B x C x T)
        """
        return self.conv(x)[:,:,:x.shape[2]]


class DSWNV(nn.Module):
    """SHALLOW WAVENET VOCODER WITH SOFTMAX OUTPUT"""

    def __init__(self, n_quantize=256, n_aux=54, hid_chn=192, skip_chn=256, aux_kernel_size=3, nonlinear_conv=False,
                aux_dilation_size=2, dilation_depth=3, dilation_repeat=3, kernel_size=6, right_size=0, pad_first=True,
                upsampling_factor=110, audio_in_flag=False, wav_conv_flag=False, do_prob=0, use_weight_norm=True):
        super(DSWNV, self).__init__()
        self.n_aux = n_aux
        self.n_quantize = n_quantize
        self.upsampling_factor = upsampling_factor
        self.in_audio_dim = self.n_quantize
        self.n_hidch = hid_chn
        self.n_skipch = skip_chn
        self.kernel_size = kernel_size
        self.dilation_depth = dilation_depth
        self.dilation_repeat = dilation_repeat
        self.aux_kernel_size = aux_kernel_size
        self.aux_dilation_size = aux_dilation_size
        self.do_prob = do_prob
        self.audio_in_flag = audio_in_flag
        self.wav_conv_flag = wav_conv_flag
        self.use_weight_norm = use_weight_norm
        self.right_size = right_size
        self.nonlinear_conv = nonlinear_conv
        self.s_dim = 320
        self.pad_first = pad_first

        # Input Layers
        self.scale_in = nn.Conv1d(self.n_aux, self.n_aux, 1)
        if self.right_size <= 0:
            self.conv = CausalDilConv1d(in_dim=self.n_aux, kernel_size=aux_kernel_size,
                                        layers=aux_dilation_size, nonlinear=self.nonlinear_conv, pad_first=self.pad_first)
            self.pad_left = self.conv.padding
            self.pad_right = 0
        else:
            self.conv = SkewedConv1d(in_dim=self.n_aux, kernel_size=aux_kernel_size,
                                        right_size=self.right_size, nonlinear=self.nonlinear_conv, pad_first=self.pad_first)
            self.pad_left = self.conv.left_size
            self.pad_right = self.conv.right_size
        conv_s_c = [nn.Conv1d(self.n_aux*self.conv.rec_field, self.s_dim, 1), nn.ReLU()]
        self.conv_s_c = nn.Sequential(*conv_s_c)

        self.in_aux_dim = self.s_dim
        self.upsampling = UpSampling(self.upsampling_factor)
        if self.do_prob > 0:
            self.aux_drop = nn.Dropout(p=self.do_prob)
        if not self.audio_in_flag:
            self.in_tot_dim = self.in_aux_dim
        else:
            self.in_tot_dim = self.in_aux_dim+self.in_audio_dim
        if self.wav_conv_flag:
            self.wav_conv = nn.Conv1d(self.in_audio_dim, self.n_hidch, 1)
            self.causal = CausalConv1d(self.n_hidch, self.n_hidch, self.kernel_size, dil_fact=0)
        else:
            self.causal = CausalConv1d(self.in_audio_dim, self.n_hidch, self.kernel_size, dil_fact=0)

        # Dilated Convolutional Recurrent Neural Network (DCRNN)
        self.padding = []
        self.dil_facts = [i for i in range(self.dilation_depth)] * self.dilation_repeat
        logging.info(self.dil_facts)
        self.in_x = nn.ModuleList()
        self.dil_h = nn.ModuleList()
        self.out_skip = nn.ModuleList()
        for i, d in enumerate(self.dil_facts):
            self.in_x += [nn.Conv1d(self.in_tot_dim, self.n_hidch*2, 1)]
            self.dil_h += [CausalConv1d(self.n_hidch, self.n_hidch*2, self.kernel_size, dil_fact=d)]
            self.padding.append(self.dil_h[i].padding)
            self.out_skip += [nn.Conv1d(self.n_hidch, self.n_skipch, 1)]
        logging.info(self.padding)
        self.receptive_field = sum(self.padding) + self.kernel_size-1
        logging.info(self.receptive_field)
        if self.do_prob > 0:
            self.dcrnn_drop = nn.Dropout(p=self.do_prob)

        # Output Layers
        self.out_1 = nn.Conv1d(self.n_skipch, self.n_quantize, 1)
        self.out_2 = nn.Conv1d(self.n_quantize, self.n_quantize, 1)

        ## apply weight norm
        if self.use_weight_norm:
            self.apply_weight_norm()
            torch.nn.utils.remove_weight_norm(self.scale_in)
        else:
            self.apply(initialize)

    def forward(self, aux, audio, first=False, do=False):
        audio = F.one_hot(audio, num_classes=self.n_quantize).float().transpose(1,2)
        # Input	Features
        x = self.upsampling(self.conv_s_c(self.conv(self.scale_in(aux.transpose(1,2)))))
        if first:
            x = F.pad(x, (self.receptive_field, 0), "replicate")
        if self.do_prob > 0 and do:
            x = self.aux_drop(x)
        if self.audio_in_flag:
            x = torch.cat((x,audio),1) # B x C x T
        # Initial Hidden Units
        if not self.wav_conv_flag:
            h = F.softsign(self.causal(audio)) # B x C x T
        else:
            h = F.softsign(self.causal(self.wav_conv(audio))) # B x C x T
        # DCRNN blocks
        sum_out, h = self._dcrnn_forward(x, h, self.in_x[0], self.dil_h[0], self.out_skip[0])
        if self.do_prob > 0 and do:
            for l in range(1,len(self.dil_facts)):
                if (l+1)%self.dilation_depth == 0:
                    out, h = self._dcrnn_forward_drop(x, h, self.in_x[l], self.dil_h[l], self.out_skip[l])
                else:
                    out, h = self._dcrnn_forward(x, h, self.in_x[l], self.dil_h[l], self.out_skip[l])
                sum_out += out
        else:
            for l in range(1,len(self.dil_facts)):
                out, h = self._dcrnn_forward(x, h, self.in_x[l], self.dil_h[l], self.out_skip[l])
                sum_out += out
        # Output
        return self.out_2(F.relu(self.out_1(F.relu(sum_out)))).transpose(1,2)

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d) \
                or isinstance(m, torch.nn.ConvTranspose2d):
                torch.nn.utils.weight_norm(m)
                logging.info(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                if isinstance(m, torch.nn.Conv1d) \
                    or isinstance(m, torch.nn.ConvTranspose2d):
                    torch.nn.utils.remove_weight_norm(m)
                    logging.info(f"Weight norm is removed from {m}.")
            except ValueError:  # this module didn't have weight norm
                return

        self.apply(_remove_weight_norm)

    def _dcrnn_forward_drop(self, x, h, in_x, dil_h, out_skip):
        x_h_ = in_x(x)*dil_h(h)
        z = torch.sigmoid(x_h_[:,:self.n_hidch,:])
        h = (1-z)*torch.tanh(x_h_[:,self.n_hidch:,:]) + z*h
        return out_skip(h), self.dcrnn_drop(h)

    def _dcrnn_forward(self, x, h, in_x, dil_h, out_skip):
        x_h_ = in_x(x)*dil_h(h)
        z = torch.sigmoid(x_h_[:,:self.n_hidch,:])
        h = (1-z)*torch.tanh(x_h_[:,self.n_hidch:,:]) + z*h
        return out_skip(h), h

    def _generate_dcrnn_forward(self, x, h, in_x, dil_h, out_skip):
        x_h_ = in_x(x)*dil_h(h)[:,:,-1:]
        z = torch.sigmoid(x_h_[:,:self.n_hidch,:])
        h = (1-z)*torch.tanh(x_h_[:,self.n_hidch:,:]) + z*h[:,:,-1:]
        return out_skip(h), h

    def batch_fast_generate(self, audio, aux, n_samples_list, intervals=4410):
        with torch.no_grad():
            # set max length
            max_samples = max(n_samples_list)
    
            # upsampling
            aux = F.pad(aux.transpose(1,2), (self.pad_left,self.pad_right), "replicate").transpose(1,2)
            x = self.upsampling(self.conv_s_c(self.conv(self.scale_in(aux.transpose(1,2))))) # B x C x T
    
            logging.info(x.shape)
            # padding if the length less than
            n_pad = self.receptive_field
            if n_pad > 0:
                audio = F.pad(audio, (n_pad, 0), "constant", self.n_quantize // 2)
                x = F.pad(x, (n_pad, 0), "replicate")

            logging.info(x.shape)
            #audio = OneHot(audio).transpose(1,2)
            audio = F.one_hot(audio, num_classes=self.n_quantize).float().transpose(1,2)
            #audio = OneHot(audio)
            if not self.audio_in_flag:
                x_ = x[:, :, :audio.size(2)]
            else:
                x_ = torch.cat((x[:, :, :audio.size(2)],audio),1)
            if self.wav_conv_flag:
                audio = self.wav_conv(audio) # B x C x T
            output = F.softsign(self.causal(audio)) # B x C x T
            output_buffer = []
            buffer_size = []
            for l in range(len(self.dil_facts)):
                _, output = self._dcrnn_forward(
                    x_, output, self.in_x[l], self.dil_h[l],
                    self.out_skip[l])
                if l < len(self.dil_facts)-1:
                    buffer_size.append(self.padding[l+1])
                else:
                    buffer_size.append(self.kernel_size - 1)
                output_buffer.append(output[:, :, -buffer_size[l] - 1: -1])
    
            # generate
            samples = audio.data  # B x T
            time_sample = []
            start = time.time()
            out_idx = self.kernel_size*2-1
            for i in range(max_samples):
                start_sample = time.time()
                samples_size = samples.size(-1)
                if not self.audio_in_flag:
                    x_ = x[:, :, (samples_size-1):samples_size]
                else:
                    x_ = torch.cat((x[:, :, (samples_size-1):samples_size],samples[:,:,-1:]),1)
                output = F.softsign(self.causal(samples[:,:,-out_idx:])[:,:,-self.kernel_size:]) # B x C x T
                output_buffer_next = []
                skip_connections = []
                for l in range(len(self.dil_facts)):
                    #start_ = time.time()
                    skip, output = self._generate_dcrnn_forward(
                        x_, output, self.in_x[l], self.dil_h[l],
                        self.out_skip[l])
                    output = torch.cat((output_buffer[l], output), 2)
                    output_buffer_next.append(output[:, :, -buffer_size[l]:])
                    skip_connections.append(skip)
    
                # update buffer
                output_buffer = output_buffer_next
    
                # get predicted sample
                output = self.out_2(F.relu(self.out_1(F.relu(sum(skip_connections))))).transpose(1,2)[:,-1]

                posterior = F.softmax(output, dim=-1)
                dist = torch.distributions.OneHotCategorical(posterior)
                sample = dist.sample().data  # B
                if i > 0:
                    out_samples = torch.cat((out_samples, torch.argmax(sample, dim=--1).unsqueeze(1)), 1)
                else:
                    out_samples = torch.argmax(sample, dim=--1).unsqueeze(1)

                if self.wav_conv_flag:
                    samples = torch.cat((samples, self.wav_conv(sample.unsqueeze(2))), 2)
                else:
                    samples = torch.cat((samples, sample.unsqueeze(2)), 2)
    
                # show progress
                time_sample.append(time.time()-start_sample)
                #if intervals is not None and (i + 1) % intervals == 0:
                if (i + 1) % intervals == 0:
                    logging.info("%d/%d estimated time = %.6f sec (%.6f sec / sample)" % (
                        (i + 1), max_samples,
                        (max_samples - i - 1) * ((time.time() - start) / intervals),
                        (time.time() - start) / intervals))
                    start = time.time()
                    #break
            logging.info("average time / sample = %.6f sec (%ld samples) [%.3f kHz/s]" % (
                        np.mean(np.array(time_sample)), len(time_sample),
                        1.0/(1000*np.mean(np.array(time_sample)))))
            logging.info("average throughput / sample = %.6f sec (%ld samples * %ld) [%.3f kHz/s]" % (
                        sum(time_sample)/(len(time_sample)*len(n_samples_list)), len(time_sample),
                        len(n_samples_list), len(time_sample)*len(n_samples_list)/(1000*sum(time_sample))))
            samples = out_samples
    
            # devide into each waveform
            samples = samples[:, -max_samples:].cpu().numpy()
            samples_list = np.split(samples, samples.shape[0], axis=0)
            samples_list = [s[0, :n_s] for s, n_s in zip(samples_list, n_samples_list)]
    
            return samples_list


class STFTLoss(torch.nn.Module):
    """STFT loss module."""

    def __init__(self, fft_size=2048, shift_size=120, win_length=600, window="hann_window"):
        """Initialize STFT loss module."""
        super(STFTLoss, self).__init__()
        self.fft_size = fft_size
        self.shift_size = shift_size
        self.win_length = win_length
        self.window = getattr(torch, window)(win_length).cuda()

    def forward(self, x, y):
        """Calculate forward propagation.

        Args:
            x (Tensor): Predicted signal (B, T) or (T).
            y (Tensor): Groundtruth signal (B, T) or (T).

        Returns:
            Tensor: Frobenius-norm STFT magnitude loss (B) or (1)
            Tensor: L1-norm STFT magnitude loss (B) or (1)

        """
        # torch.stft --> * x N x T x 2 [N: freq_bins, T: frames, 2: real-imag]
        #logging.info(x.shape)
        #logging.info(y.shape)
        x_stft = torch.stft(x, self.fft_size, self.shift_size, self.win_length, self.window, return_complex=False)
        #logging.info(x_stft.shape)
        y_stft = torch.stft(y, self.fft_size, self.shift_size, self.win_length, self.window, return_complex=False)
        #logging.info(y_stft.shape)
        if len(x.shape) > 1:
            x_mag = torch.clamp(torch.sqrt(x_stft[..., 0]**2 + x_stft[..., 1]**2).transpose(2, 1), min=1e-16)
            y_mag = torch.clamp(torch.sqrt(y_stft[..., 0]**2 + y_stft[..., 1]**2).transpose(2, 1), min=1e-16)
            #x_mag = torch.clamp(torch.sqrt(x_stft[..., 0]**2 + x_stft[..., 1]**2).transpose(2, 1), min=1.2e-7)
            #y_mag = torch.clamp(torch.sqrt(y_stft[..., 0]**2 + y_stft[..., 1]**2).transpose(2, 1), min=1.2e-7)
        #    logging.info("BxT")
        #    logging.info(x_mag.shape)
        #    logging.info(y_mag.shape)
            err = y_mag - x_mag
            fro = torch.norm(err, 'fro', dim=(1,2)) / torch.norm(y_mag, 'fro', dim=(1,2)) # (B)
            l1 = err.abs().sum(-1).sum(-1) / y_mag.sum(-1).sum(-1)
            dB = torch.mean(torch.sqrt(torch.mean((20*(torch.log10(x_mag)-torch.log10(y_mag)))**2, -1)), -1)
        else:
            x_mag = torch.clamp(torch.sqrt(x_stft[..., 0]**2 + x_stft[..., 1]**2).transpose(1, 0), min=1e-16)
            y_mag = torch.clamp(torch.sqrt(y_stft[..., 0]**2 + y_stft[..., 1]**2).transpose(1, 0), min=1e-16)
            #x_mag = torch.clamp(torch.sqrt(x_stft[..., 0]**2 + x_stft[..., 1]**2).transpose(1, 0), min=1.2e-7)
            #y_mag = torch.clamp(torch.sqrt(y_stft[..., 0]**2 + y_stft[..., 1]**2).transpose(1, 0), min=1.2e-7)
        #    logging.info("T")
        #    logging.info(x_mag.shape)
        #    logging.info(y_mag.shape)
            err = y_mag - x_mag
            fro = torch.norm(err, 'fro') / torch.norm(y_mag, 'fro') # (1)
            l1 = err.abs().sum() / y_mag.sum()
            dB = torch.mean(torch.sqrt(torch.mean((20*(torch.log10(x_mag)-torch.log10(y_mag)))**2, -1)))

        #return fro, l1
        return fro+l1, dB


class MultiResolutionSTFTLoss(torch.nn.Module):
    """Multi resolution STFT loss module."""

    def __init__(self,
                 fft_sizes = [128, 256, 64],
                 hop_sizes = [8, 15, 4],
                 win_lengths = [38, 75, 19],
                 window="hann_window"):
                 #fft_sizes = [512, 1024, 256],
                 #hop_sizes = [30, 60, 15],
                 #win_lengths = [150, 300, 75],
                 #fft_sizes = [1024, 2048, 512],
                 #hop_sizes = [60, 120, 30],
                 #win_lengths = [300, 600, 150],
        """Initialize Multi resolution STFT loss module.

        Args:
            fft_sizes (list): List of FFT sizes.
            hop_sizes (list): List of hop sizes.
            win_lengths (list): List of window lengths.
            window (str): Window function type.

        """
        super(MultiResolutionSTFTLoss, self).__init__()
        if hop_sizes is not None:
            assert len(fft_sizes) == len(hop_sizes)
            self.hop_sizes = hop_sizes
        else:
            self.hop_sizes = [fft_size // 4 for fft_size in fft_sizes]
        if win_lengths is not None:
            assert len(fft_sizes) == len(win_lengths)
            self.win_lengths = win_lengths
        else:
            self.win_lengths = fft_sizes
        self.fft_sizes = fft_sizes
        #self.pad_sizes = [(fft_size - win_length) // 2 for fft_size, win_length in zip(self.fft_sizes, self.win_lengths)]
        self.n_fft_confs = len(self.fft_sizes)
        self.stft_losses = torch.nn.ModuleList()
        for fs, ss, wl in zip(self.fft_sizes, self.hop_sizes, self.win_lengths):
            self.stft_losses += [STFTLoss(fs, ss, wl, window)]

    def forward(self, x, y):
        """Calculate forward propagation.

        Args:
            x (Tensor): Predicted signal (B, T) or (T).
            y (Tensor): Groundtruth signal (B, T) or (T).

        Returns:
            Tensor: Multi resolution frobenius-norm STFT magnitude loss (B) or (1)
            Tensor: Multi resolution L1-norm STFT magnitude loss (B) or (1)

        """
        fro_count = 0
        l1_count = 0
        if len(x.shape) > 1:
            B = x.shape[0]
            if len(x.shape) > 2:
                N = x.shape[1]
                x = x.reshape(B*N,-1)
                y = y.reshape(B*N,-1)
            else:
                N = 0
        else:
            B = 0
            N = 0
        for i in range(self.n_fft_confs):
            #logging.info(x.shape[-1])
            #logging.info(self.fft_sizes[i])
            #logging.info(self.win_lengths[i])
            #logging.info(self.pad_sizes[i])
            if x.shape[-1] > (self.fft_sizes[i]//2):
            #if x.shape[-1] > self.pad_sizes[i]:
            #    logging.info("pad-%d"%(i))
                fro, l1 = self.stft_losses[i](x, y)
                if fro_count > 0:
                    if not torch.isinf(fro.sum()) and not torch.isnan(fro.sum()):
                        fro_loss = torch.cat((fro_loss, fro.unsqueeze(-1)), -1)
                        fro_count += 1
                    else:
                        logging.info("nan_1")
                else:
                    if not torch.isinf(fro.sum()) and not torch.isnan(fro.sum()):
                        fro_loss = fro.unsqueeze(-1)
                        fro_count += 1
                    else:
                        logging.info("nan_2")
                if l1_count > 0:
                    if not torch.isinf(l1.sum()) and not torch.isnan(l1.sum()):
                        l1_loss = torch.cat((l1_loss, l1.unsqueeze(-1)), -1)
                        l1_count += 1
                    else:
                        logging.info("nan_3")
                else:
                    if not torch.isinf(l1.sum()) and not torch.isnan(l1.sum()):
                        l1_loss = l1.unsqueeze(-1)
                        l1_count += 1
                    else:
                        logging.info("nan_4")
        if fro_count == 0:
            if len(x.shape) > 1:
                fro_loss = torch.zeros_like(x[..., 0])
            else:
                fro_loss = torch.zeros(1, device=x.device)[0]
        else:
            fro_loss = torch.mean(fro_loss, -1)
        if l1_count == 0:
            if len(x.shape) > 1:
                l1_loss = torch.zeros_like(x[..., 0])
            else:
                l1_loss = torch.zeros(1, device=x.device)[0]
        else:
            l1_loss = torch.mean(l1_loss, -1)
        if N > 0:
            fro_loss = fro_loss.reshape(B,N)
            l1_loss = l1_loss.reshape(B,N)

        return fro_loss, l1_loss
