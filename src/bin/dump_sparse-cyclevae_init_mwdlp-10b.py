#!/usr/bin/env python
'''Copyright (c) 2017-2018 Mozilla

   Redistribution and use in source and binary forms, with or without
   modification, are permitted provided that the following conditions
   are met:

   - Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

   - Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.

   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
   ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
   LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
   A PARTICULAR PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE FOUNDATION OR
   CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
   EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
   PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
   PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
   LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
   NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
   SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
'''
'''
    Based on dump_lpcnet.py
    Modified for 16-bit output multiband wavernn with data-driven LPC
    by: Patrick Lumban Tobing (Nagoya University) on October 2020
    Further modified for sparse cyclevae and 10-bit mu-law output multiband wavernn with data-driven LPC.
    by: Patrick Lumban Tobing (Nagoya University) on December 2020 - September 2021
'''

import argparse
import os
import sys

import torch
from vcneuvoco import GRU_VAE_ENCODER, GRU_SPEC_DECODER, GRU_EXCIT_DECODER
from vcneuvoco import GRU_SPK, SPKID_TRANSFORM_LAYER
from vcneuvoco import GRU_WAVE_DECODER_DUALGRU_COMPACT_MBAND_CF, decode_mu_law
from pqmf import PQMF

from scipy.signal import firwin
from scipy.signal import windows

#print("a")
from librosa import filters
#print("b")

import numpy as np

#FS = 8000
#FS = 16000
#FS = 22050
FS = 24000
#FFTL = 1024
FFTL = 2048
#SHIFTMS = 5
#SHIFTMS = 4.9886621315192743764172335600907
SHIFTMS = 10
#SHIFTMS = 9.9773242630385487528344671201814
WINMS = 27.5
HIGHPASS_CUTOFF = 65
HPASS_FILTER_TAPS = 1023


def printVector(f, vector, name, dtype='float'):
    v = np.reshape(vector, (-1))
    #print('static const float ', name, '[', len(v), '] = \n', file=f)
    f.write('static const {} {}[{}] = {{\n'.format(dtype, name, len(v)))
    if dtype == 'float':
        for i in range(0, len(v)):
            f.write('{}f'.format(v[i]))
            if (i!=len(v)-1):
                f.write(',')
            else:
                break;
            if (i%8==7):
                f.write("\n")
            else:
                f.write(" ")
    else:
        for i in range(0, len(v)):
            f.write('{}'.format(v[i]))
            if (i!=len(v)-1):
                f.write(',')
            else:
                break;
            if (i%8==7):
                f.write("\n")
            else:
                f.write(" ")
    #print(v, file=f)
    f.write('\n};\n\n')


def printSparseVector(f, A, name):
    N = A.shape[0]
    W = np.zeros((0,))
    diag = np.concatenate([np.diag(A[:,:N]), np.diag(A[:,N:2*N]), np.diag(A[:,2*N:])])
    A[:,:N] = A[:,:N] - np.diag(np.diag(A[:,:N]))
    A[:,N:2*N] = A[:,N:2*N] - np.diag(np.diag(A[:,N:2*N]))
    A[:,2*N:] = A[:,2*N:] - np.diag(np.diag(A[:,2*N:]))
    printVector(f, diag, name + '_diag')
    idx = np.zeros((0,), dtype='int')
    for i in range(3*N//16):
        pos = idx.shape[0]
        idx = np.append(idx, -1)
        nb_nonzero = 0
        for j in range(N):
            if np.sum(np.abs(A[j, i*16:(i+1)*16])) > 1e-10:
                nb_nonzero = nb_nonzero + 1
                idx = np.append(idx, j)
                W = np.concatenate([W, A[j, i*16:(i+1)*16]])
        idx[pos] = nb_nonzero
    printVector(f, W, name)
    #idx = np.tile(np.concatenate([np.array([N]), np.arange(N)]), 3*N//16)
    printVector(f, idx, name + '_idx', dtype='int')


def main():
    parser = argparse.ArgumentParser()
    # mandatory arguments
    parser.add_argument("config_cycvae", metavar="config_cycvae.conf",
                        type=str, help="path of model cycvae config")
    parser.add_argument("model_cycvae", metavar="model_cycvae.pkl",
                        type=str, help="path of model cycvae file")
    parser.add_argument("config", metavar="config_mwdlp10bit.conf",
                        type=str, help="path of model mwdlp10bit config")
    parser.add_argument("model", metavar="model_mwdlp10bit.pkl",
                        type=str, help="path of model mwdlp10bit file")
    # optional arguments
    parser.add_argument("--fs", metavar="sampling rate", default=FS,
                        type=int, help="waveform sampling rate [Hz]")
    parser.add_argument("--shiftms", metavar="shift ms", default=SHIFTMS,
                        type=float, help="frame shift in feature extraction [ms]")
    parser.add_argument("--winms", metavar="window length ms", default=WINMS,
                        type=float, help="window length in feature extraction [ms]")
    parser.add_argument("--fftl", metavar="FFT length", default=FFTL,
                        type=int, help="FFT length in feature extraction")
    parser.add_argument("--highpass_cutoff", metavar="highpass cutoff [Hz]", default=HIGHPASS_CUTOFF,
                        type=int, help="frequency cutoff for waveform high-pass filter")
    parser.add_argument("--c_cycvae_file", "-cvf", default="nnet_cv_data.c", metavar="c_cycvae.c",
                        type=str, help="cycvae c file; default is nnet_cv_data.c")
    parser.add_argument("--h_cycvae_file", "-hvf", default="nnet_cv_data.h", metavar="c_cycvae.h",
                        type=str, help="cycvae header file; default is nnet_cv_data.h")
    parser.add_argument("--c_mwdlp10bit_file", "-cf", default="nnet_data.c", metavar="c_mwdlp10bit.c",
                        type=str, help="mwdlp10bit c file; default is nnet_data.c")
    parser.add_argument("--h_mwdlp10bit_file", "-hf", default="nnet_data.h", metavar="c_mwdlp10bit.h",
                        type=str, help="mwdlp10bit header file; default is nnet_data.h")
    args = parser.parse_args()

    os.environ["CUDA_VISIBLE_DEVICES"]  = ""

    #set config and model
    config_cycvae = torch.load(args.config_cycvae)
    print(config_cycvae)
    config = torch.load(args.config)
    print(config)
    spk_list = config_cycvae.spk_list.split('@')
    n_spk = len(spk_list)
    print(spk_list)

    model_encoder_melsp = GRU_VAE_ENCODER(
        in_dim=config_cycvae.mel_dim,
        n_spk=n_spk,
        lat_dim=config_cycvae.lat_dim,
        hidden_layers=config_cycvae.hidden_layers_enc,
        hidden_units=config_cycvae.hidden_units_enc,
        kernel_size=config_cycvae.kernel_size_enc,
        dilation_size=config_cycvae.dilation_size_enc,
        causal_conv=config_cycvae.causal_conv_enc,
        pad_first=True,
        s_conv_flag=config_cycvae.s_conv_flag,
        seg_conv_flag=config_cycvae.seg_conv_flag,
        right_size=config_cycvae.right_size_enc)
    print(model_encoder_melsp)
    model_decoder_melsp = GRU_SPEC_DECODER(
        feat_dim=config_cycvae.lat_dim+config_cycvae.lat_dim_e,
        excit_dim=config_cycvae.excit_dim,
        out_dim=config_cycvae.mel_dim,
        n_spk=config_cycvae.emb_spk_dim_ti,
        aux_dim=config_cycvae.emb_spk_dim_tv,
        hidden_layers=config_cycvae.hidden_layers_dec,
        hidden_units=config_cycvae.hidden_units_dec,
        kernel_size=config_cycvae.kernel_size_dec,
        dilation_size=config_cycvae.dilation_size_dec,
        causal_conv=config_cycvae.causal_conv_dec,
        pad_first=True,
        right_size=config_cycvae.right_size_dec,
        s_conv_flag=config_cycvae.s_conv_flag,
        seg_conv_flag=config_cycvae.seg_conv_flag,
        pdf_gauss=True,
        red_dim=config_cycvae.mel_dim)
    print(model_decoder_melsp)
    model_encoder_excit = GRU_VAE_ENCODER(
        in_dim=config_cycvae.mel_dim,
        n_spk=n_spk,
        lat_dim=config_cycvae.lat_dim_e,
        hidden_layers=config_cycvae.hidden_layers_enc,
        hidden_units=config_cycvae.hidden_units_enc,
        kernel_size=config_cycvae.kernel_size_enc,
        dilation_size=config_cycvae.dilation_size_enc,
        causal_conv=config_cycvae.causal_conv_enc,
        pad_first=True,
        s_conv_flag=config_cycvae.s_conv_flag,
        seg_conv_flag=config_cycvae.seg_conv_flag,
        right_size=config_cycvae.right_size_enc)
    print(model_encoder_excit)
    model_decoder_excit = GRU_EXCIT_DECODER(
        feat_dim=config_cycvae.lat_dim_e,
        cap_dim=config_cycvae.cap_dim,
        n_spk=config_cycvae.emb_spk_dim_ti,
        aux_dim=config_cycvae.emb_spk_dim_tv,
        hidden_layers=config_cycvae.hidden_layers_lf0,
        hidden_units=config_cycvae.hidden_units_lf0,
        kernel_size=config_cycvae.kernel_size_lf0,
        dilation_size=config_cycvae.dilation_size_lf0,
        causal_conv=config_cycvae.causal_conv_lf0,
        pad_first=True,
        s_conv_flag=config_cycvae.s_conv_flag,
        seg_conv_flag=config_cycvae.seg_conv_flag,
        right_size=config_cycvae.right_size_lf0,
        red_dim=config_cycvae.mel_dim)
    print(model_decoder_excit)
    model_spkidtr = SPKID_TRANSFORM_LAYER(
        n_spk=n_spk,
        emb_dim=config_cycvae.emb_spk_dim_ti,
        n_weight_emb=config_cycvae.n_weight_emb,
        conv_emb_flag=True,
        spkidtr_dim=config_cycvae.spkidtr_dim)
    print(model_spkidtr)
    model_spk = GRU_SPK(
        n_spk=config_cycvae.emb_spk_dim_ti,
        dim_out=config_cycvae.emb_spk_dim_tv,
        feat_dim=config_cycvae.lat_dim+config_cycvae.lat_dim_e,
        hidden_units=32,
        kernel_size=config_cycvae.kernel_size_spk,
        dilation_size=config_cycvae.dilation_size_spk,
        causal_conv=config_cycvae.causal_conv_spk,
        pad_first=True,
        right_size=config_cycvae.right_size_spk,
        red_dim=config_cycvae.mel_dim,
        s_conv_flag=config_cycvae.s_conv_flag,
        seg_conv_flag=config_cycvae.seg_conv_flag)
    print(model_spk)
    model = GRU_WAVE_DECODER_DUALGRU_COMPACT_MBAND_CF(
        feat_dim=config_cycvae.mel_dim,
        upsampling_factor=config.upsampling_factor,
        hidden_units=config.hidden_units_wave,
        hidden_units_2=config.hidden_units_wave_2,
        kernel_size=config.kernel_size_wave,
        dilation_size=config.dilation_size_wave,
        n_quantize=config.n_quantize,
        causal_conv=config.causal_conv_wave,
        right_size=config.right_size,
        n_bands=config.n_bands,
        pad_first=True,
        s_dim=config.s_dim,
        mid_dim=config.mid_dim,
        emb_flag=True,
        seg_conv_flag=config.seg_conv_flag_wave,
        lpc=config.lpc)
    print(model)
    device = torch.device("cpu")
    model_encoder_melsp.load_state_dict(torch.load(args.model_cycvae, map_location=device)["model_encoder_melsp"])
    model_decoder_melsp.load_state_dict(torch.load(args.model_cycvae, map_location=device)["model_decoder_melsp"])
    model_encoder_excit.load_state_dict(torch.load(args.model_cycvae, map_location=device)["model_encoder_excit"])
    model_decoder_excit.load_state_dict(torch.load(args.model_cycvae, map_location=device)["model_decoder_excit"])
    model_spkidtr.load_state_dict(torch.load(args.model_cycvae, map_location=device)["model_spkidtr"])
    model_spk.load_state_dict(torch.load(args.model_cycvae, map_location=device)["model_spk"])
    model.load_state_dict(torch.load(args.model, map_location=device)["model_waveform"])
    model_encoder_melsp.remove_weight_norm()
    model_decoder_melsp.remove_weight_norm()
    model_encoder_excit.remove_weight_norm()
    model_decoder_excit.remove_weight_norm()
    model_spkidtr.remove_weight_norm()
    model_spk.remove_weight_norm()
    model.remove_weight_norm()
    model_encoder_melsp.eval()
    model_decoder_melsp.eval()
    model_encoder_excit.eval()
    model_decoder_excit.eval()
    model_spkidtr.eval()
    model_spk.eval()
    model.eval()
    for param in model_encoder_melsp.parameters():
        param.requires_grad = False
    for param in model_decoder_melsp.parameters():
        param.requires_grad = False
    for param in model_encoder_excit.parameters():
        param.requires_grad = False
    for param in model_decoder_excit.parameters():
        param.requires_grad = False
    for param in model_spkidtr.parameters():
        param.requires_grad = False
    for param in model_spk.parameters():
        param.requires_grad = False
    for name, param in model.named_parameters():
        param.requires_grad = False

    ## Multiband WaveRNN with data-driven LPC (MWDLP)
    cfile = args.c_mwdlp10bit_file
    hfile = args.h_mwdlp10bit_file
    
    f = open(cfile, 'w')
    hf = open(hfile, 'w')
    
    f.write('/*This file is automatically generated from a PyTorch model*/\n\n')
    f.write('#ifdef HAVE_CONFIG_H\n#include "config.h"\n#endif\n\n#include "nnet.h"\n#include "{}"\n\n'.format(hfile))
    
    hf.write('/*This file is automatically generated from a PyTorch model*/\n\n')
    hf.write('#ifndef RNN_MWDLP_DATA_H\n#define RNN_MWDLP_DATA_H\n\n#include "nnet.h"\n\n')
    
    cond_size = model.s_dim
    #PyTorch & Keras = (emb_dict_size,emb_size)
    embed_size = model.wav_dim
    embed_size_bands = model.wav_dim_bands
    
    max_rnn_neurons = 1
    #PyTorch = (hidden_dim*3,in_dim*3)
    #Keras = (in_dim*3,hidden_dim*3)

    #embedding coarse and fine
    E_coarse = model.embed_c_wav.weight.data.numpy()
    E_fine = model.embed_f_wav.weight.data.numpy()

    #gru_main weight_input
    W = model.gru.weight_ih_l0.permute(1,0).data.numpy()
    #dump coarse_embed pre-computed input_weight contribution for all classes
    name = 'gru_a_embed_coarse'
    print("printing layer " + name)
    W_bands = W[cond_size:-embed_size_bands]
    # n_bands x embed_dict_size x hidden_size
    weights = np.expand_dims(np.dot(E_coarse, W_bands[:embed_size]), axis=0)
    for i in range(1,model.n_bands):
        weights = np.r_[weights, np.expand_dims(np.dot(E_coarse, W_bands[embed_size*i:embed_size*(i+1)]), axis=0)]
    printVector(f, weights, name + '_weights')
    f.write('const EmbeddingLayer {} = {{\n   {}_weights,\n   {}, {}\n}};\n\n'
            .format(name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const EmbeddingLayer {};\n\n'.format(name))
    #dump coarse_fine pre-computed input_weight contribution for all classes
    name = 'gru_a_embed_fine'
    print("printing layer " + name)
    W_bands = W[-embed_size_bands:]
    # n_bands x embed_dict_size x hidden_size
    weights = np.expand_dims(np.dot(E_fine, W_bands[:embed_size]), axis=0)
    for i in range(1,model.n_bands):
        weights = np.r_[weights, np.expand_dims(np.dot(E_fine, W_bands[embed_size*i:embed_size*(i+1)]), axis=0)]
    printVector(f, weights, name + '_weights')
    f.write('const EmbeddingLayer {} = {{\n   {}_weights,\n   {}, {}\n}};\n\n'
            .format(name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const EmbeddingLayer {};\n\n'.format(name))
    #dump input cond-part weight and input bias
    name = 'gru_a_dense_feature'
    print("printing layer " + name)
    weights = W[:cond_size]
    bias = model.gru.bias_ih_l0.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #dump gru_coarse input weight cond-part and input bias
    name = 'gru_b_dense_feature'
    print("printing layer " + name)
    W = model.gru_2.weight_ih_l0.permute(1,0).data.numpy()
    weights = W[:cond_size]
    bias = model.gru_2.bias_ih_l0.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))
    #dump gru_coarse input weight state-part
    name = 'gru_b_dense_feature_state'
    print("printing layer " + name)
    weights = W[cond_size:]
    bias = np.zeros(W.shape[1])
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #gru_fine weight_input
    W = model.gru_f.weight_ih_l0.permute(1,0).data.numpy()
    #dump coarse_embed pre-computed input_weight contribution for all classes
    name = 'gru_c_embed_coarse'
    print("printing layer " + name)
    W_bands = W[cond_size:-model.hidden_units_2]
    # n_bands x embed_dict_size x hidden_size
    weights = np.expand_dims(np.dot(E_coarse, W_bands[:embed_size]), axis=0)
    for i in range(1,model.n_bands):
        weights = np.r_[weights, np.expand_dims(np.dot(E_coarse, W_bands[embed_size*i:embed_size*(i+1)]), axis=0)]
    printVector(f, weights, name + '_weights')
    f.write('const EmbeddingLayer {} = {{\n   {}_weights,\n   {}, {}\n}};\n\n'
            .format(name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const EmbeddingLayer {};\n\n'.format(name))
    #dump input cond-part weight and input bias
    name = 'gru_c_dense_feature'
    print("printing layer " + name)
    weights = W[:cond_size]
    bias = model.gru_f.bias_ih_l0.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))
    #dump input state-part weight
    name = 'gru_c_dense_feature_state'
    print("printing layer " + name)
    weights = W[-model.hidden_units_2:]
    bias = np.zeros(W.shape[1])
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))
  
    #PyTorch = (out,in,ks) / (out,in)
    #to
    #Keras = (ks,in,out) / (in,out)

    #dump scale_in
    name = 'feature_norm'
    print("printing layer " + name + " of type " + model.scale_in.__class__.__name__)
    weights = model.scale_in.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model.scale_in.bias.data.numpy()
    std = 1.0/np.diag(weights) #in training script, diagonal square weights matrix defined as 1/std
    mean = (-bias)*std #in training script, bias defined as -mean/std
    printVector(f, mean, name + '_mean')
    printVector(f, std, name + '_std')
    f.write('const NormStats {} = {{\n   {}_mean,\n   {}_std,\n   {}\n}};\n\n'
            .format(name, name, name, bias.shape[0]))
    hf.write('extern const NormStats {};\n\n'.format(name))

    #dump segmental_conv
    name = "feature_conv"
    #FIXME: make model format without sequential for two-sided/causal conv
    if model.right_size <= 0:
        print("printing layer " + name + " of type " + model.conv.conv[0].__class__.__name__)
        weights = model.conv.conv[0].weight.permute(2,1,0).data.numpy()
        bias = model.conv.conv[0].bias.data.numpy()
    else:
        print("printing layer " + name + " of type " + model.conv.conv.__class__.__name__)
        weights = model.conv.conv.weight.permute(2,1,0).data.numpy()
        bias = model.conv.conv.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const Conv1DLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[1], weights.shape[0], weights.shape[2]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[2]))
    hf.write('#define {}_INPUT_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model.pad_left+1+model.pad_right))
    hf.write('#define {}_STATE_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model.pad_left+1+model.pad_right-1))
    hf.write('#define {}_DELAY {}\n'.format(name.upper(), model.pad_right))
    hf.write('extern const Conv1DLayer {};\n\n'.format(name))

    #dump dense_relu
    name = 'feature_dense'
    print("printing layer " + name + " of type " + model.conv_s_c[0].__class__.__name__)
    weights = model.conv_s_c[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model.conv_s_c[0].bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #dump sparse_main_gru
    name = 'sparse_gru_a'
    print("printing layer " + name + " of type sparse " + model.gru.__class__.__name__)
    weights = model.gru.weight_hh_l0.transpose(0,1).data.numpy()
    bias = model.gru.bias_hh_l0.data.numpy()
    printSparseVector(f, weights, name + '_recurrent_weights')
    printVector(f, bias, name + '_bias')
    activation = 'TANH'
    #activation = 'TANH_EXP'
    reset_after = 1
    neurons = weights.shape[1]//3
    max_rnn_neurons = max(max_rnn_neurons, neurons)
    f.write('const SparseGRULayer {} = {{\n   {}_bias,\n   {}_recurrent_weights_diag,\n   {}_recurrent_weights,\n   '\
        '{}_recurrent_weights_idx,\n   {}, ACTIVATION_{}, {}\n}};\n\n'.format(name, name, name, name, name,
            weights.shape[1]//3, activation, reset_after))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]//3))
    hf.write('#define {}_STATE_SIZE {}\n'.format(name.upper(), weights.shape[1]//3))
    hf.write('extern const SparseGRULayer {};\n\n'.format(name))

    #dump dense_gru_coarse
    name = "gru_b"
    print("printing layer " + name + " of type " + model.gru_2.__class__.__name__)
    weights_ih = model.gru_2.weight_ih_l0.transpose(0,1)[cond_size:].data.numpy()
    weights_hh = model.gru_2.weight_hh_l0.transpose(0,1).data.numpy()
    bias = model.gru_2.bias_hh_l0.data.numpy()
    printVector(f, weights_ih, name + '_weights')
    printVector(f, weights_hh, name + '_recurrent_weights')
    printVector(f, bias, name + '_bias')
    activation = 'TANH'
    #activation = 'TANH_EXP'
    reset_after = 1
    neurons = weights_hh.shape[1]//3
    max_rnn_neurons = max(max_rnn_neurons, neurons)
    f.write('const GRULayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}_recurrent_weights,\n   {}, {}, ACTIVATION_{}, '\
        '{}\n}};\n\n'.format(name, name, name, name, weights_ih.shape[0], weights_hh.shape[1]//3,
            activation, reset_after))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('#define {}_STATE_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('extern const GRULayer {};\n\n'.format(name))

    #dump dense_gru_fine
    name = "gru_c"
    print("printing layer " + name + " of type " + model.gru_f.__class__.__name__)
    weights_ih = model.gru_f.weight_ih_l0.transpose(0,1)[-model.hidden_units_2:].data.numpy()
    weights_hh = model.gru_f.weight_hh_l0.transpose(0,1).data.numpy()
    bias = model.gru_f.bias_hh_l0.data.numpy()
    printVector(f, weights_ih, name + '_weights')
    printVector(f, weights_hh, name + '_recurrent_weights')
    printVector(f, bias, name + '_bias')
    activation = 'TANH'
    #activation = 'TANH_EXP'
    reset_after = 1
    neurons = weights_hh.shape[1]//3
    max_rnn_neurons = max(max_rnn_neurons, neurons)
    f.write('const GRULayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}_recurrent_weights,\n   {}, {}, ACTIVATION_{}, '\
        '{}\n}};\n\n'.format(name, name, name, name, weights_ih.shape[0], weights_hh.shape[1]//3,
            activation, reset_after))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('#define {}_STATE_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('extern const GRULayer {};\n\n'.format(name))

    #dump dual_fc_coarse
    name = "dual_fc_coarse"
    print("printing layer " + name)
    weights = model.out.conv.weight.permute(2,1,0)[0].data.numpy() # in x out: 32 x 384 ((6*2*8)*2+6*2*16) [6 bands, 8 lpc]
    bias = model.out.conv.bias.data.numpy()
    factors = (0.5*torch.exp(model.out.fact.weight[0])).data.numpy()
    ## [NBx2x(K+K+16] --> [2x(K+K+16)xNB]
    ## [[K,K,16]_1a,[K,K,16]_1b,...,[K,K,16]_NBa,[K,K,16]_NBb]
    if model.lpc > 0:
        ## permute weights and bias out structure from [NBx2x(K+K+16)] to [2x(K+K+16)xNB]
        lpc2 = model.lpc*2
        lpc2mid = lpc2+model.mid_out
        lpc3mid = lpc2mid+model.lpc
        lpc4mid = lpc3mid+model.lpc
        lpc4mid2 = lpc4mid+model.mid_out
        #bias_signs_1 = bias[:lpc]
        #bias_mags_1 = bias[lpc:lpc2]
        #bias_mids_1 = bias[lpc2:lpc2mid]
        bias_1 = bias[:lpc2mid]
        #bias_signs_2 = bias[lpc2mid:lpc3mid]
        #bias_mags_2 = bias[lpc3mid:lpc4mid]
        #bias_mids_2 = bias[lpc4mid:lpc4mid2]
        bias_2 = bias[lpc2mid:lpc4mid2]
        for i in range(1,model.n_bands):
            idx = lpc4mid2*i
            #bias_signs_1 = np.r_[bias_signs_1, bias[idx:idx+lpc]]
            #bias_mags_1 = np.r_[bias_mags_1, bias[idx+lpc:idx+lpc2]]
            #bias_mids_1 = np.r_[bias_mids_1, bias[idx+lpc2:idx+lpc2mid]]
            bias_1 = np.r_[bias_1, bias[idx:idx+lpc2mid]]
            #bias_signs_2 = np.r_[bias_signs_2, bias[idx+lpc2mid:idx+lpc3mid]]
            #bias_mags_2 = np.r_[bias_mags_2, bias[idx+lpc3mid:idx+lpc4mid]]
            #bias_mids_2 = np.r_[bias_mids_2, bias[idx+lpc4mid:idx+lpc4mid2]]
            bias_2 = np.r_[bias_2, bias[idx+lpc2mid:idx+lpc4mid2]]
        #bias = np.r_[bias_signs_1, bias_mags_1, bias_mids_1, bias_signs_2, bias_mags_2, bias_mids_2]
        bias = np.r_[bias_1, bias_2]
        #weights_signs_1 = weights[:,:lpc]
        #weights_mags_1 = weights[:,lpc:lpc2]
        #weights_mids_1 = weights[:,lpc2:lpc2mid]
        weights_1 = weights[:,:lpc2mid]
        #weights_signs_2 = weights[:,lpc2mid:lpc3mid]
        #weights_mags_2 = weights[:,lpc3mid:lpc4mid]
        #weights_mids_2 = weights[:,lpc4mid:lpc4mid2]
        weights_2 = weights[:,lpc2mid:lpc4mid2]
        for i in range(1,model.n_bands):
            idx = lpc4mid2*i
            #weights_signs_1 = np.c_[weights_signs_1, weights[:,idx:idx+lpc]]
            #weights_mags_1 = np.c_[weights_mags_1, weights[:,idx+lpc:idx+lpc2]]
            #weights_mids_1 = np.c_[weights_mids_1, weights[:,idx+lpc2:idx+lpc2mid]]
            weights_1 = np.c_[weights_1, weights[:,idx:idx+lpc2mid]]
            #weights_signs_2 = np.c_[weights_signs_2, weights[:,idx+lpc2mid:idx+lpc3mid]]
            #weights_mags_2 = np.c_[weights_mags_2, weights[:,idx+lpc3mid:idx+lpc4mid]]
            #weights_mids_2 = np.c_[weights_mids_2, weights[:,idx+lpc4mid:idx+lpc4mid2]]
            weights_2 = np.c_[weights_2, weights[:,idx+lpc2mid:idx+lpc4mid2]]
        #weights = np.c_[weights_signs_1, weights_mags_1, weights_mids_1, weights_signs_2, weights_mags_2, weights_mids_2]
        weights = np.c_[weights_1, weights_2]
        #factors_signs_1 = factors[:lpc]
        #factors_mags_1 = factors[lpc:lpc2]
        #factors_mids_1 = factors[lpc2:lpc2mid]
        factors_1 = factors[:lpc2mid]
        #factors_signs_2 = factors[lpc2mid:lpc3mid]
        #factors_mags_2 = factors[lpc3mid:lpc4mid]
        #factors_mids_2 = factors[lpc4mid:lpc4mid2]
        factors_2 = factors[lpc2mid:lpc4mid2]
        for i in range(1,model.n_bands):
            idx = lpc4mid2*i
            #factors_signs_1 = np.r_[factors_signs_1, factors[idx:idx+lpc]]
            #factors_mags_1 = np.r_[factors_mags_1, factors[idx+lpc:idx+lpc2]]
            #factors_mids_1 = np.r_[factors_mids_1, factors[idx+lpc2:idx+lpc2mid]]
            factors_1 = np.r_[factors_1, factors[idx:idx+lpc2mid]]
            #factors_signs_2 = np.r_[factors_signs_2, factors[idx+lpc2mid:idx+lpc3mid]]
            #factors_mags_2 = np.r_[factors_mags_2, factors[idx+lpc3mid:idx+lpc4mid]]
            #factors_mids_2 = np.r_[factors_mids_2, factors[idx+lpc4mid:idx+lpc4mid2]]
            factors_2 = np.r_[factors_2, factors[idx+lpc2mid:idx+lpc4mid2]]
        #factors = np.r_[factors_signs_1, factors_mags_1, factors_mids_1, factors_signs_2, factors_mags_2, factors_mids_2]
        factors = np.r_[factors_1, factors_2]
    else:
        mid_out2 = model.mid_out*2
        ## permute weights and bias out structure from [NBx2x16] to [NBx16x2]
        bias_mids = bias
        bias_mids_1 = bias_mids[:model.mid_out]
        bias_mids_2 = bias_mids[model.mid_out:mid_out2]
        for i in range(1,model.n_bands):
            idx = mid_out2*i
            idx_ = idx+model.mid_out
            bias_mids_1 = np.r_[bias_mids_1, bias_mids[idx:idx_]]
            bias_mids_2 = np.r_[bias_mids_2, bias_mids[idx_:mid_out2*(i+1)]]
        bias = np.r_[bias_mids_1, bias_mids_2]
        weights_mids = weights
        weights_mids_1 = weights_mids[:,:model.mid_out]
        weights_mids_2 = weights_mids[:,model.mid_out:mid_out2]
        for i in range(1,model.n_bands):
            idx = mid_out2*i
            idx_ = idx+model.mid_out
            weights_mids_1 = np.c_[weights_mids_1, weights_mids[:,idx:idx_]]
            weights_mids_2 = np.c_[weights_mids_2, weights_mids[:,idx_:mid_out2*(i+1)]]
        weights = np.c_[weights_mids_1, weights_mids_2]
        # change factors structure from NBx2xmid_out to NBxmid_outx2
        factors_mids = factors.reshape(model.n_bands,2,model.mid_out)
        factors_mids_1 = factors_mids[:,0].reshape(-1)
        factors_mids_2 = factors_mids[:,1].reshape(-1)
        factors = np.r_[factors_mids_1, factors_mids_2]
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    #printVector(f, factors[:model.out.lpc2bands], name + '_factor_signs')
    #printVector(f, factors[model.out.lpc2bands:model.out.lpc4bands], name + '_factor_mags')
    #printVector(f, factors[model.out.lpc4bands:], name + '_factor_mids')
    printVector(f, factors, name + '_factors')
    f.write('const MDenseLayerMWDLP10 {} = {{\n   {}_bias,\n   {}_weights,\n   {}_factors,\n   '\
        'ACTIVATION_RELU, ACTIVATION_TANH_EXP, ACTIVATION_EXP, ACTIVATION_TANHSHRINK\n}};\n\n'.format(name, name, name, name))
    hf.write('extern const MDenseLayerMWDLP10 {};\n\n'.format(name))

    #dump dense_fc_out_coarse
    name = 'fc_out_coarse'
    print("printing layer " + name)
    weights = model.out.out.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model.out.out.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #dump dual_fc_fine
    name = "dual_fc_fine"
    print("printing layer " + name)
    weights = model.out_f.conv.weight.permute(2,1,0)[0].data.numpy()
    bias = model.out_f.conv.bias.data.numpy()
    factors = (0.5*torch.exp(model.out_f.fact.weight[0])).data.numpy()
    ## [NBx2x(K+K+16] --> [2x(K+K+16)xNB]
    ## [[K,K,16]_1a,[K,K,16]_1b,...,[K,K,16]_NBa,[K,K,16]_NBb]
    if model.lpc > 0:
        ## permute weights and bias out structure from [NBx2x(K+K+16)] to [2x(K+K+16)xNB]
        lpc2 = model.lpc*2
        lpc2mid = lpc2+model.mid_out
        lpc3mid = lpc2mid+model.lpc
        lpc4mid = lpc3mid+model.lpc
        lpc4mid2 = lpc4mid+model.mid_out
        #bias_signs_1 = bias[:lpc]
        #bias_mags_1 = bias[lpc:lpc2]
        #bias_mids_1 = bias[lpc2:lpc2mid]
        bias_1 = bias[:lpc2mid]
        #bias_signs_2 = bias[lpc2mid:lpc3mid]
        #bias_mags_2 = bias[lpc3mid:lpc4mid]
        #bias_mids_2 = bias[lpc4mid:lpc4mid2]
        bias_2 = bias[lpc2mid:lpc4mid2]
        for i in range(1,model.n_bands):
            idx = lpc4mid2*i
            #bias_signs_1 = np.r_[bias_signs_1, bias[idx:idx+lpc]]
            #bias_mags_1 = np.r_[bias_mags_1, bias[idx+lpc:idx+lpc2]]
            #bias_mids_1 = np.r_[bias_mids_1, bias[idx+lpc2:idx+lpc2mid]]
            bias_1 = np.r_[bias_1, bias[idx:idx+lpc2mid]]
            #bias_signs_2 = np.r_[bias_signs_2, bias[idx+lpc2mid:idx+lpc3mid]]
            #bias_mags_2 = np.r_[bias_mags_2, bias[idx+lpc3mid:idx+lpc4mid]]
            #bias_mids_2 = np.r_[bias_mids_2, bias[idx+lpc4mid:idx+lpc4mid2]]
            bias_2 = np.r_[bias_2, bias[idx+lpc2mid:idx+lpc4mid2]]
        #bias = np.r_[bias_signs_1, bias_mags_1, bias_mids_1, bias_signs_2, bias_mags_2, bias_mids_2]
        bias = np.r_[bias_1, bias_2]
        #weights_signs_1 = weights[:,:lpc]
        #weights_mags_1 = weights[:,lpc:lpc2]
        #weights_mids_1 = weights[:,lpc2:lpc2mid]
        weights_1 = weights[:,:lpc2mid]
        #weights_signs_2 = weights[:,lpc2mid:lpc3mid]
        #weights_mags_2 = weights[:,lpc3mid:lpc4mid]
        #weights_mids_2 = weights[:,lpc4mid:lpc4mid2]
        weights_2 = weights[:,lpc2mid:lpc4mid2]
        for i in range(1,model.n_bands):
            idx = lpc4mid2*i
            #weights_signs_1 = np.c_[weights_signs_1, weights[:,idx:idx+lpc]]
            #weights_mags_1 = np.c_[weights_mags_1, weights[:,idx+lpc:idx+lpc2]]
            #weights_mids_1 = np.c_[weights_mids_1, weights[:,idx+lpc2:idx+lpc2mid]]
            weights_1 = np.c_[weights_1, weights[:,idx:idx+lpc2mid]]
            #weights_signs_2 = np.c_[weights_signs_2, weights[:,idx+lpc2mid:idx+lpc3mid]]
            #weights_mags_2 = np.c_[weights_mags_2, weights[:,idx+lpc3mid:idx+lpc4mid]]
            #weights_mids_2 = np.c_[weights_mids_2, weights[:,idx+lpc4mid:idx+lpc4mid2]]
            weights_2 = np.c_[weights_2, weights[:,idx+lpc2mid:idx+lpc4mid2]]
        #weights = np.c_[weights_signs_1, weights_mags_1, weights_mids_1, weights_signs_2, weights_mags_2, weights_mids_2]
        weights = np.c_[weights_1, weights_2]
        #factors_signs_1 = factors[:lpc]
        #factors_mags_1 = factors[lpc:lpc2]
        #factors_mids_1 = factors[lpc2:lpc2mid]
        factors_1 = factors[:lpc2mid]
        #factors_signs_2 = factors[lpc2mid:lpc3mid]
        #factors_mags_2 = factors[lpc3mid:lpc4mid]
        #factors_mids_2 = factors[lpc4mid:lpc4mid2]
        factors_2 = factors[lpc2mid:lpc4mid2]
        for i in range(1,model.n_bands):
            idx = lpc4mid2*i
            #factors_signs_1 = np.r_[factors_signs_1, factors[idx:idx+lpc]]
            #factors_mags_1 = np.r_[factors_mags_1, factors[idx+lpc:idx+lpc2]]
            #factors_mids_1 = np.r_[factors_mids_1, factors[idx+lpc2:idx+lpc2mid]]
            factors_1 = np.r_[factors_1, factors[idx:idx+lpc2mid]]
            #factors_signs_2 = np.r_[factors_signs_2, factors[idx+lpc2mid:idx+lpc3mid]]
            #factors_mags_2 = np.r_[factors_mags_2, factors[idx+lpc3mid:idx+lpc4mid]]
            #factors_mids_2 = np.r_[factors_mids_2, factors[idx+lpc4mid:idx+lpc4mid2]]
            factors_2 = np.r_[factors_2, factors[idx+lpc2mid:idx+lpc4mid2]]
        #factors = np.r_[factors_signs_1, factors_mags_1, factors_mids_1, factors_signs_2, factors_mags_2, factors_mids_2]
        factors = np.r_[factors_1, factors_2]
    else:
        mid_out2 = model.mid_out*2
        ## permute weights and bias out structure from [NBx2x16] to [NBx16x2]
        bias_mids = bias
        bias_mids_1 = bias_mids[:model.mid_out]
        bias_mids_2 = bias_mids[model.mid_out:mid_out2]
        for i in range(1,model.n_bands):
            idx = mid_out2*i
            idx_ = idx+model.mid_out
            bias_mids_1 = np.r_[bias_mids_1, bias_mids[idx:idx_]]
            bias_mids_2 = np.r_[bias_mids_2, bias_mids[idx_:mid_out2*(i+1)]]
        bias = np.r_[bias_mids_1, bias_mids_2]
        weights_mids = weights
        weights_mids_1 = weights_mids[:,:model.mid_out]
        weights_mids_2 = weights_mids[:,model.mid_out:mid_out2]
        for i in range(1,model.n_bands):
            idx = mid_out2*i
            idx_ = idx+model.mid_out
            weights_mids_1 = np.c_[weights_mids_1, weights_mids[:,idx:idx_]]
            weights_mids_2 = np.c_[weights_mids_2, weights_mids[:,idx_:mid_out2*(i+1)]]
        weights = np.c_[weights_mids_1, weights_mids_2]
        # change factors structure from NBx2xmid_out to NBxmid_outx2
        factors_mids = factors.reshape(model.n_bands,2,model.mid_out)
        factors_mids_1 = factors_mids[:,0].reshape(-1)
        factors_mids_2 = factors_mids[:,1].reshape(-1)
        factors = np.r_[factors_mids_1, factors_mids_2]
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    #printVector(f, factors[:model.out_f.lpc2bands], name + '_factor_signs')
    #printVector(f, factors[model.out_f.lpc2bands:model.out_f.lpc4bands], name + '_factor_mags')
    #printVector(f, factors[model.out_f.lpc4bands:], name + '_factor_mids')
    printVector(f, factors, name + '_factors')
    f.write('const MDenseLayerMWDLP10 {} = {{\n   {}_bias,\n   {}_weights,\n   {}_factors,\n   '\
        'ACTIVATION_RELU, ACTIVATION_TANH_EXP, ACTIVATION_EXP, ACTIVATION_TANHSHRINK\n}};\n\n'.format(name, name, name, name))
    hf.write('extern const MDenseLayerMWDLP10 {};\n\n'.format(name))

    #dump dense_fc_out_fine
    name = 'fc_out_fine'
    print("printing layer " + name)
    weights = model.out_f.out.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model.out_f.out.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    if config.lpc > 0:
        #previous logits embedding coarse and fine
        #logits_c = (torch.tanh(model.logits_sgns_c.weight)*torch.exp(model.logits_mags_c.weight)).data.numpy()
        #logits_f = (torch.tanh(model.logits_sgns_f.weight)*torch.exp(model.logits_mags_f.weight)).data.numpy()
        logits_c = model.logits_c.weight.data.numpy()
        logits_f = model.logits_f.weight.data.numpy()
    else:
        #previous logits embedding coarse and fine
        logits_c = np.zeros((model.cf_dim, 1))
        logits_f = np.zeros((model.cf_dim, 1))

    #dump previous logits coarse
    name = 'prev_logits_coarse'
    print("printing layer " + name)
    printVector(f, logits_c, name + '_weights')
    f.write('const EmbeddingLayer {} = {{\n   {}_weights,\n   {}, {}\n}};\n\n'
            .format(name, name, logits_c.shape[0], logits_c.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), logits_c.shape[1]))
    hf.write('extern const EmbeddingLayer {};\n\n'.format(name))
    #dump previous logits fine
    name = 'prev_logits_fine'
    print("printing layer " + name)
    printVector(f, logits_f, name + '_weights')
    f.write('const EmbeddingLayer {} = {{\n   {}_weights,\n   {}, {}\n}};\n\n'
            .format(name, name, logits_f.shape[0], logits_f.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), logits_f.shape[1]))
    hf.write('extern const EmbeddingLayer {};\n\n'.format(name))

    #dump pqmf_synthesis filt
    name = "pqmf_synthesis"
    print("printing layer " + name)
    pqmf = PQMF(model.n_bands)
    pqmf_order = pqmf.taps
    pqmf_delay = pqmf_order // 2
    weights = pqmf.synthesis_filter.permute(2,1,0).data.numpy()
    bias = np.zeros(1)
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const Conv1DLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[1], weights.shape[0], weights.shape[2]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[2]))
    hf.write('#define {}_STATE_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1], pqmf_delay+1))
    hf.write('#define {}_DELAY {}\n'.format(name.upper(), pqmf_delay))
    hf.write('extern const Conv1DLayer {};\n\n'.format(name))
    print(f'{pqmf.subbands} {pqmf.err} {pqmf.A} {pqmf.taps} {pqmf.cutoff_ratio} {pqmf.beta}')

    #hf.write('#define MAX_RNN_NEURONS {}\n\n'.format(max_rnn_neurons))
    hf.write('#define RNN_MAIN_NEURONS {}\n\n'.format(model.hidden_units))
    hf.write('#define RNN_SUB_NEURONS {}\n\n'.format(model.hidden_units_2))
    hf.write('#define N_MBANDS {}\n\n'.format(model.n_bands))
    hf.write('#define DLPC_ORDER {}\n\n'.format(model.lpc))
    hf.write('#define PQMF_ORDER {}\n\n'.format(pqmf_order))
    hf.write('#define MID_OUT {}\n\n'.format(model.mid_out))
    hf.write('#define N_QUANTIZE {}\n\n'.format(model.n_quantize))
    hf.write('#define SQRT_QUANTIZE {}\n\n'.format(model.cf_dim))
    hf.write('#define N_SAMPLE_BANDS {}\n\n'.format(model.upsampling_factor))
    hf.write('#define CONV_KERNEL_1 {}\n\n'.format(model.kernel_size-1))
    hf.write('#define FEATURES_DIM {}\n\n'.format(model.in_dim))

    hf.write('typedef struct {\n')
    hf.write('  float feature_conv_state[FEATURE_CONV_STATE_SIZE];\n')
    hf.write('  float gru_a_state[SPARSE_GRU_A_STATE_SIZE];\n')
    hf.write('  float gru_b_state[GRU_B_STATE_SIZE];\n')
    hf.write('  float gru_c_state[GRU_C_STATE_SIZE];\n')
    hf.write('} MWDLP10NNetState;\n')
    
    hf.write('\n\n#endif\n')
    
    f.close()
    hf.close()

    ## CycleVAE+SpkNet for Mel-Spectrogram conversion with intermediate excitation estimation
    cfile = args.c_cycvae_file
    hfile = args.h_cycvae_file
    
    f = open(cfile, 'w')
    hf = open(hfile, 'w')
    
    f.write('/*This file is automatically generated from a PyTorch model*/\n\n')
    f.write('#ifdef HAVE_CONFIG_H\n#include "config.h"\n#endif\n\n#include "nnet.h"\n#include "{}"\n\n'.format(hfile))
    
    hf.write('/*This file is automatically generated from a PyTorch model*/\n\n')
    hf.write('#ifndef RNN_CYCVAE_DATA_H\n#define RNN_CYCVAE_DATA_H\n\n#include "nnet.h"\n\n')

    ## Dump melsp_norm
    name = 'melsp_norm'
    print("printing layer " + name + " of type " + model_encoder_melsp.scale_in.__class__.__name__)
    weights = model_encoder_melsp.scale_in.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_encoder_melsp.scale_in.bias.data.numpy()
    std = 1.0/np.diag(weights) #in training script, diagonal square weights matrix defined as 1/std
    mean = (-bias)*std #in training script, bias defined as -mean/std
    printVector(f, mean, name + '_mean')
    printVector(f, std, name + '_std')
    f.write('const NormStats {} = {{\n   {}_mean,\n   {}_std,\n   {}\n}};\n\n'
            .format(name, name, name, bias.shape[0]))
    hf.write('extern const NormStats {};\n\n'.format(name))

    ## Dump uvf0_norm
    name = 'uvf0_norm'
    print("printing layer " + name + " of type " + model_decoder_melsp.scale_in.__class__.__name__)
    weights = model_decoder_melsp.scale_in.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_decoder_melsp.scale_in.bias.data.numpy()
    std = 1.0/np.diag(weights)[:1] #in training script, diagonal square weights matrix defined as 1/std
    mean = (-bias[:1])*std #in training script, bias defined as -mean/std
    printVector(f, mean, name + '_mean')
    printVector(f, std, name + '_std')
    f.write('const NormStats {} = {{\n   {}_mean,\n   {}_std,\n   {}\n}};\n\n'
            .format(name, name, name, mean.shape[0]))
    hf.write('extern const NormStats {};\n\n'.format(name))

    ## Dump in_red spk
    name = 'fc_red_spk'
    print("printing layer " + name)
    #defined as sequential with relu activation
    weights = model_spk.in_red[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_spk.in_red[0].bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    ## Dump in_red dec_excit
    name = 'fc_red_dec_excit'
    print("printing layer " + name)
    #defined as sequential with relu activation
    weights = model_decoder_excit.in_red[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_decoder_excit.in_red[0].bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    ## Dump in_red dec_melsp
    name = 'fc_red_dec_melsp'
    print("printing layer " + name)
    #defined as sequential with relu activation
    weights = model_decoder_melsp.in_red[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_decoder_melsp.in_red[0].bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    ## Dump conv_in enc_melsp
    name = "feature_conv_enc_melsp"
    #FIXME: make model format without sequential for two-sided/causal conv
    if model_encoder_melsp.right_size <= 0:
        print("printing layer " + name + " of type " + model_encoder_melsp.conv.conv[0].__class__.__name__)
        weights = model_encoder_melsp.conv.conv[0].weight.permute(2,1,0).data.numpy()
        bias = model_encoder_melsp.conv.conv[0].bias.data.numpy()
    else:
        print("printing layer " + name + " of type " + model_encoder_melsp.conv.conv.__class__.__name__)
        weights = model_encoder_melsp.conv.conv.weight.permute(2,1,0).data.numpy()
        bias = model_encoder_melsp.conv.conv.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const Conv1DLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[1], weights.shape[0], weights.shape[2]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[2]))
    if not config_cycvae.s_conv_flag:
        feat_enc_melsp_dim = weights.shape[2]
    hf.write('#define {}_INPUT_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_encoder_melsp.pad_left+1+model_encoder_melsp.pad_right))
    hf.write('#define {}_STATE_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_encoder_melsp.pad_left+1+model_encoder_melsp.pad_right-1))
    enc_melsp_state_size = weights.shape[1]*(model_encoder_melsp.pad_left+1+model_encoder_melsp.pad_right-1)
    hf.write('#define {}_DELAY {}\n'.format(name.upper(), model_encoder_melsp.pad_right))
    hf.write('extern const Conv1DLayer {};\n\n'.format(name))

    if config_cycvae.s_conv_flag:
        #dump dense_relu enc_melsp
        name = 'feature_dense_enc_melsp'
        print("printing layer " + name + " of type " + model_encoder_melsp.conv_s_c[0].__class__.__name__)
        weights = model_encoder_melsp.conv_s_c[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
        bias = model_encoder_melsp.conv_s_c[0].bias.data.numpy()
        printVector(f, weights, name + '_weights')
        printVector(f, bias, name + '_bias')
        f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
                .format(name, name, name, weights.shape[0], weights.shape[1]))
        hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
        feat_enc_melsp_dim = weights.shape[1]
        hf.write('extern const DenseLayer {};\n\n'.format(name))

    ## Dump conv_in enc_excit
    name = "feature_conv_enc_excit"
    #FIXME: make model format without sequential for two-sided/causal conv
    if model_encoder_excit.right_size <= 0:
        print("printing layer " + name + " of type " + model_encoder_excit.conv.conv[0].__class__.__name__)
        weights = model_encoder_excit.conv.conv[0].weight.permute(2,1,0).data.numpy()
        bias = model_encoder_excit.conv.conv[0].bias.data.numpy()
    else:
        print("printing layer " + name + " of type " + model_encoder_excit.conv.conv.__class__.__name__)
        weights = model_encoder_excit.conv.conv.weight.permute(2,1,0).data.numpy()
        bias = model_encoder_excit.conv.conv.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const Conv1DLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[1], weights.shape[0], weights.shape[2]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[2]))
    if not config_cycvae.s_conv_flag:
        feat_enc_excit_dim = weights.shape[2]
    hf.write('#define {}_INPUT_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_encoder_excit.pad_left+1+model_encoder_excit.pad_right))
    hf.write('#define {}_STATE_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_encoder_excit.pad_left+1+model_encoder_excit.pad_right-1))
    enc_excit_state_size = weights.shape[1]*(model_encoder_excit.pad_left+1+model_encoder_excit.pad_right-1)
    hf.write('#define {}_DELAY {}\n'.format(name.upper(), model_encoder_excit.pad_right))
    hf.write('extern const Conv1DLayer {};\n\n'.format(name))

    if config_cycvae.s_conv_flag:
        #dump dense_relu enc_excit
        name = 'feature_dense_enc_excit'
        print("printing layer " + name + " of type " + model_encoder_excit.conv_s_c[0].__class__.__name__)
        weights = model_encoder_excit.conv_s_c[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
        bias = model_encoder_excit.conv_s_c[0].bias.data.numpy()
        printVector(f, weights, name + '_weights')
        printVector(f, bias, name + '_bias')
        f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
                .format(name, name, name, weights.shape[0], weights.shape[1]))
        hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
        feat_enc_excit_dim = weights.shape[1]
        hf.write('extern const DenseLayer {};\n\n'.format(name))

    ## Same delay for melsp and excit encoders
    assert(model_encoder_melsp.pad_right == model_encoder_excit.pad_right)

    ## Same conv out size for melsp and excit encoders
    assert(enc_melsp_state_size == enc_excit_state_size)

    ## Dump conv_in spk
    name = "feature_conv_spk"
    #FIXME: make model format without sequential for two-sided/causal conv
    if model_spk.right_size <= 0:
        print("printing layer " + name + " of type " + model_spk.conv.conv[0].__class__.__name__)
        weights = model_spk.conv.conv[0].weight.permute(2,1,0).data.numpy()
        bias = model_spk.conv.conv[0].bias.data.numpy()
    else:
        print("printing layer " + name + " of type " + model_spk.conv.conv.__class__.__name__)
        weights = model_spk.conv.conv.weight.permute(2,1,0).data.numpy()
        bias = model_spk.conv.conv.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const Conv1DLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[1], weights.shape[0], weights.shape[2]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[2]))
    if not config_cycvae.s_conv_flag:
        feat_spk_dim = weights.shape[2]
    hf.write('#define {}_INPUT_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_spk.pad_left+1+model_spk.pad_right))
    hf.write('#define {}_STATE_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_spk.pad_left+1+model_spk.pad_right-1))
    hf.write('#define {}_DELAY {}\n'.format(name.upper(), model_spk.pad_right))
    hf.write('extern const Conv1DLayer {};\n\n'.format(name))

    if config_cycvae.s_conv_flag:
        #dump dense_relu spk
        name = 'feature_dense_spk'
        print("printing layer " + name + " of type " + model_spk.conv_s_c[0].__class__.__name__)
        weights = model_spk.conv_s_c[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
        bias = model_spk.conv_s_c[0].bias.data.numpy()
        printVector(f, weights, name + '_weights')
        printVector(f, bias, name + '_bias')
        f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
                .format(name, name, name, weights.shape[0], weights.shape[1]))
        hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
        feat_spk_dim = weights.shape[1]
        hf.write('extern const DenseLayer {};\n\n'.format(name))

    ## Dump conv_in dec_excit
    name = "feature_conv_dec_excit"
    #FIXME: make model format without sequential for two-sided/causal conv
    if model_decoder_excit.right_size <= 0:
        print("printing layer " + name + " of type " + model_decoder_excit.conv.conv[0].__class__.__name__)
        weights = model_decoder_excit.conv.conv[0].weight.permute(2,1,0).data.numpy()
        bias = model_decoder_excit.conv.conv[0].bias.data.numpy()
    else:
        print("printing layer " + name + " of type " + model_decoder_excit.conv.conv.__class__.__name__)
        weights = model_decoder_excit.conv.conv.weight.permute(2,1,0).data.numpy()
        bias = model_decoder_excit.conv.conv.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const Conv1DLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[1], weights.shape[0], weights.shape[2]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[2]))
    if not config_cycvae.s_conv_flag:
        feat_dec_excit_dim = weights.shape[2]
    hf.write('#define {}_INPUT_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_decoder_excit.pad_left+1+model_decoder_excit.pad_right))
    hf.write('#define {}_STATE_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_decoder_excit.pad_left+1+model_decoder_excit.pad_right-1))
    hf.write('#define {}_DELAY {}\n'.format(name.upper(), model_decoder_excit.pad_right))
    hf.write('extern const Conv1DLayer {};\n\n'.format(name))

    if config_cycvae.s_conv_flag:
        #dump dense_relu dec_excit
        name = 'feature_dense_dec_excit'
        print("printing layer " + name + " of type " + model_decoder_excit.conv_s_c[0].__class__.__name__)
        weights = model_decoder_excit.conv_s_c[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
        bias = model_decoder_excit.conv_s_c[0].bias.data.numpy()
        printVector(f, weights, name + '_weights')
        printVector(f, bias, name + '_bias')
        f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
                .format(name, name, name, weights.shape[0], weights.shape[1]))
        hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
        feat_dec_excit_dim = weights.shape[1]
        hf.write('extern const DenseLayer {};\n\n'.format(name))

    ## Dump conv_in dec_melsp
    name = "feature_conv_dec_melsp"
    #FIXME: make model format without sequential for two-sided/causal conv
    if model_decoder_melsp.right_size <= 0:
        print("printing layer " + name + " of type " + model_decoder_melsp.conv.conv[0].__class__.__name__)
        weights = model_decoder_melsp.conv.conv[0].weight.permute(2,1,0).data.numpy()
        bias = model_decoder_melsp.conv.conv[0].bias.data.numpy()
    else:
        print("printing layer " + name + " of type " + model_decoder_melsp.conv.conv.__class__.__name__)
        weights = model_decoder_melsp.conv.conv.weight.permute(2,1,0).data.numpy()
        bias = model_decoder_melsp.conv.conv.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const Conv1DLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[1], weights.shape[0], weights.shape[2]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[2]))
    if not config_cycvae.s_conv_flag:
        feat_dec_melsp_dim = weights.shape[2]
    hf.write('#define {}_INPUT_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_decoder_melsp.pad_left+1+model_decoder_melsp.pad_right))
    hf.write('#define {}_STATE_SIZE ({}*{})\n'.format(name.upper(), weights.shape[1],
        model_decoder_melsp.pad_left+1+model_decoder_melsp.pad_right-1))
    hf.write('#define {}_DELAY {}\n'.format(name.upper(), model_decoder_melsp.pad_right))
    hf.write('extern const Conv1DLayer {};\n\n'.format(name))

    if config_cycvae.s_conv_flag:
        #dump dense_relu dec_melsp
        name = 'feature_dense_dec_melsp'
        print("printing layer " + name + " of type " + model_decoder_melsp.conv_s_c[0].__class__.__name__)
        weights = model_decoder_melsp.conv_s_c[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
        bias = model_decoder_melsp.conv_s_c[0].bias.data.numpy()
        printVector(f, weights, name + '_weights')
        printVector(f, bias, name + '_bias')
        f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
                .format(name, name, name, weights.shape[0], weights.shape[1]))
        hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
        feat_dec_melsp_dim = weights.shape[1]
        hf.write('extern const DenseLayer {};\n\n'.format(name))

    #dump sparse_gru_enc_melsp
    name = 'sparse_gru_enc_melsp'
    print("printing layer " + name + " of type sparse " + model_encoder_melsp.gru.__class__.__name__)
    weights_ih = model_encoder_melsp.gru.weight_ih_l0.transpose(0,1).data.numpy()
    weights_hh = model_encoder_melsp.gru.weight_hh_l0.transpose(0,1).data.numpy()
    bias_ih = model_encoder_melsp.gru.bias_ih_l0.data.numpy()
    bias_hh = model_encoder_melsp.gru.bias_hh_l0.data.numpy()
    printVector(f, weights_ih, name + '_input_weights')
    printSparseVector(f, weights_hh, name + '_recurrent_weights')
    printVector(f, bias_ih, name + '_input_bias')
    printVector(f, bias_hh, name + '_recurrent_bias')
    #activation = 'TANH'
    activation = 'TANH_EXP'
    reset_after = 1
    neurons = weights_hh.shape[1]//3
    max_rnn_neurons = max(max_rnn_neurons, neurons)
    f.write('const SparseFrameGRULayer {} = {{\n   {}_input_bias,\n   {}_recurrent_bias,\n   {}_input_weights,\n'\
        '   {}_recurrent_weights_diag,\n    {}_recurrent_weights,\n    {}_recurrent_weights_idx,\n   {}, {}, '\
            'ACTIVATION_{}, {}\n}};\n\n'.format(name, name, name, name, name, name, name,
                weights_ih.shape[0], weights_hh.shape[1]//3, activation, reset_after))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('#define {}_STATE_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('extern const SparseFrameGRULayer {};\n\n'.format(name))

    #dump sparse_gru_enc_excit
    name = 'sparse_gru_enc_excit'
    print("printing layer " + name + " of type sparse " + model_encoder_excit.gru.__class__.__name__)
    weights_ih = model_encoder_excit.gru.weight_ih_l0.transpose(0,1).data.numpy()
    weights_hh = model_encoder_excit.gru.weight_hh_l0.transpose(0,1).data.numpy()
    bias_ih = model_encoder_excit.gru.bias_ih_l0.data.numpy()
    bias_hh = model_encoder_excit.gru.bias_hh_l0.data.numpy()
    printVector(f, weights_ih, name + '_input_weights')
    printSparseVector(f, weights_hh, name + '_recurrent_weights')
    printVector(f, bias_ih, name + '_input_bias')
    printVector(f, bias_hh, name + '_recurrent_bias')
    #activation = 'TANH'
    activation = 'TANH_EXP'
    reset_after = 1
    neurons = weights_hh.shape[1]//3
    max_rnn_neurons = max(max_rnn_neurons, neurons)
    f.write('const SparseFrameGRULayer {} = {{\n   {}_input_bias,\n   {}_recurrent_bias,\n   {}_input_weights,\n'\
        '   {}_recurrent_weights_diag,\n    {}_recurrent_weights,\n    {}_recurrent_weights_idx,\n   {}, {}, '\
            'ACTIVATION_{}, {}\n}};\n\n'.format(name, name, name, name, name, name, name,
                weights_ih.shape[0], weights_hh.shape[1]//3, activation, reset_after))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('#define {}_STATE_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('extern const SparseFrameGRULayer {};\n\n'.format(name))

    #dump dense_gru_spk
    name = "gru_spk"
    print("printing layer " + name + " of type " + model_spk.gru.__class__.__name__)
    weights_ih = model_spk.gru.weight_ih_l0.transpose(0,1).data.numpy()
    weights_hh = model_spk.gru.weight_hh_l0.transpose(0,1).data.numpy()
    bias_ih = model_spk.gru.bias_ih_l0
    bias_hh = model_spk.gru.bias_hh_l0
    printVector(f, weights_ih, name + '_input_weights')
    printVector(f, weights_hh, name + '_recurrent_weights')
    printVector(f, bias_ih, name + '_input_bias')
    printVector(f, bias_hh, name + '_recurrent_bias')
    #activation = 'TANH'
    activation = 'TANH_EXP'
    reset_after = 1
    neurons = weights_hh.shape[1]//3
    max_rnn_neurons = max(max_rnn_neurons, neurons)
    f.write('const FrameGRULayer {} = {{\n   {}_input_bias,\n   {}_recurrent_bias,\n   {}_input_weights,\n   {}_recurrent_weights,\n   {}, {}, ACTIVATION_{}, '\
        '{}\n}};\n\n'.format(name, name, name, name, name, weights_ih.shape[0], weights_hh.shape[1]//3,
            activation, reset_after))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('#define {}_STATE_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('extern const FrameGRULayer {};\n\n'.format(name))

    #dump dense_gru_dec_excit
    name = "gru_dec_excit"
    print("printing layer " + name + " of type " + model_decoder_excit.gru.__class__.__name__)
    weights_ih = model_decoder_excit.gru.weight_ih_l0.transpose(0,1).data.numpy()
    weights_hh = model_decoder_excit.gru.weight_hh_l0.transpose(0,1).data.numpy()
    bias_ih = model_decoder_excit.gru.bias_ih_l0
    bias_hh = model_decoder_excit.gru.bias_hh_l0
    printVector(f, weights_ih, name + '_input_weights')
    printVector(f, weights_hh, name + '_recurrent_weights')
    printVector(f, bias_ih, name + '_input_bias')
    printVector(f, bias_hh, name + '_recurrent_bias')
    #activation = 'TANH'
    activation = 'TANH_EXP'
    reset_after = 1
    neurons = weights_hh.shape[1]//3
    max_rnn_neurons = max(max_rnn_neurons, neurons)
    f.write('const FrameGRULayer {} = {{\n   {}_input_bias,\n   {}_recurrent_bias,\n   {}_input_weights,\n   {}_recurrent_weights,\n   {}, {}, ACTIVATION_{}, '\
        '{}\n}};\n\n'.format(name, name, name, name, name, weights_ih.shape[0], weights_hh.shape[1]//3,
            activation, reset_after))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('#define {}_STATE_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('extern const FrameGRULayer {};\n\n'.format(name))

    #dump sparse_gru_dec_melsp
    name = 'sparse_gru_dec_melsp'
    print("printing layer " + name + " of type sparse " + model_decoder_melsp.gru.__class__.__name__)
    weights_ih = model_decoder_melsp.gru.weight_ih_l0.transpose(0,1).data.numpy()
    weights_hh = model_decoder_melsp.gru.weight_hh_l0.transpose(0,1).data.numpy()
    bias_ih = model_decoder_melsp.gru.bias_ih_l0.data.numpy()
    bias_hh = model_decoder_melsp.gru.bias_hh_l0.data.numpy()
    printVector(f, weights_ih, name + '_input_weights')
    printSparseVector(f, weights_hh, name + '_recurrent_weights')
    printVector(f, bias_ih, name + '_input_bias')
    printVector(f, bias_hh, name + '_recurrent_bias')
    #activation = 'TANH'
    activation = 'TANH_EXP'
    reset_after = 1
    neurons = weights_hh.shape[1]//3
    max_rnn_neurons = max(max_rnn_neurons, neurons)
    f.write('const SparseFrameGRULayer {} = {{\n   {}_input_bias,\n   {}_recurrent_bias,\n   {}_input_weights,\n'\
        '   {}_recurrent_weights_diag,\n    {}_recurrent_weights,\n    {}_recurrent_weights_idx,\n   {}, {}, '\
            'ACTIVATION_{}, {}\n}};\n\n'.format(name, name, name, name, name, name, name,
                weights_ih.shape[0], weights_hh.shape[1]//3, activation, reset_after))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('#define {}_STATE_SIZE {}\n'.format(name.upper(), weights_hh.shape[1]//3))
    hf.write('extern const SparseFrameGRULayer {};\n\n'.format(name))

    #PyTorch = (out,in,ks) / (out,in)
    #to
    #Keras = (ks,in,out) / (in,out)

    #dump fc_out_enc_melsp
    name = 'fc_out_enc_melsp'
    print("printing layer " + name)
    #take only mean-part output [latent without sampling, i.e., MAP estimate]
    weights = model_encoder_melsp.out.weight[n_spk:-model_encoder_melsp.lat_dim].permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_encoder_melsp.out.bias[n_spk:-model_encoder_melsp.lat_dim].data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_TANHSHRINK\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #dump fc_out_enc_excit
    name = 'fc_out_enc_excit'
    print("printing layer " + name)
    #take only mean-part output [latent without sampling, i.e., MAP estimate]
    weights = model_encoder_excit.out.weight[n_spk:-model_encoder_excit.lat_dim].permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_encoder_excit.out.bias[n_spk:-model_encoder_excit.lat_dim].data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_TANHSHRINK\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #dump fc_out_spk
    name = 'fc_out_spk'
    print("printing layer " + name)
    weights = model_spk.out.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_spk.out.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    #f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_TANH_EXP\n}};\n\n'
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_TANHSHRINK\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #embed_spk_ti
    embed_spk_ti = model_spkidtr.embed_spk.weight.data.numpy()

    #dump embed_spk_ti
    name = 'embed_spk_ti'
    print("printing layer " + name)
    printVector(f, embed_spk_ti, name + '_weights')
    f.write('const EmbeddingLayer {} = {{\n   {}_weights,\n   {}, {}\n}};\n\n'
            .format(name, name, embed_spk_ti.shape[0], embed_spk_ti.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), embed_spk_ti.shape[1]))
    hf.write('extern const EmbeddingLayer {};\n\n'.format(name))

    #dump fc_out_dec_excit
    name = 'fc_out_dec_excit'
    print("printing layer " + name)
    weights = model_decoder_excit.out.weight.permute(2,1,0)[0,:,:2].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_decoder_excit.out.bias[:2].data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #dump fc_out_dec_melsp
    name = 'fc_out_dec_melsp'
    print("printing layer " + name)
    weights = model_decoder_melsp.out.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_decoder_melsp.out.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    #f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_TANHSHRINK\n}};\n\n'
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_LINEAR\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    #dump spk-code_transform
    name = 'fc_in_spk_code'
    print("printing layer " + name)
    #defined as sequential with relu activation
    weights = model_spkidtr.conv_emb[0].weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_spkidtr.conv_emb[0].bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_RELU\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    name = 'fc_in_spk_code_transform'
    print("printing layer " + name)
    weights = model_spkidtr.conv.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_spkidtr.conv.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_TANHSHRINK\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    name = 'fc_out_spk_code_transform'
    print("printing layer " + name)
    weights = model_spkidtr.deconv.weight.permute(2,1,0)[0].data.numpy() #it's defined as conv1d with ks=1 on the model
    bias = model_spkidtr.deconv.bias.data.numpy()
    printVector(f, weights, name + '_weights')
    printVector(f, bias, name + '_bias')
    f.write('const DenseLayer {} = {{\n   {}_bias,\n   {}_weights,\n   {}, {}, ACTIVATION_TANH_EXP\n}};\n\n'
            .format(name, name, name, weights.shape[0], weights.shape[1]))
    hf.write('#define {}_OUT_SIZE {}\n'.format(name.upper(), weights.shape[1]))
    hf.write('extern const DenseLayer {};\n\n'.format(name))

    hf.write('#define RNN_ENC_MELSP_NEURONS {}\n\n'.format(model_encoder_melsp.hidden_units))
    hf.write('#define RNN_ENC_EXCIT_NEURONS {}\n\n'.format(model_encoder_excit.hidden_units))
    hf.write('#define RNN_SPK_NEURONS {}\n\n'.format(model_spk.hidden_units))
    hf.write('#define RNN_DEC_MELSP_NEURONS {}\n\n'.format(model_decoder_melsp.hidden_units))
    hf.write('#define RNN_DEC_EXCIT_NEURONS {}\n\n'.format(model_decoder_excit.hidden_units))
    hf.write('#define FEATURE_DIM_MELSP {}\n\n'.format(model_decoder_melsp.spec_dim))
    hf.write('#define FEATURE_LAT_DIM_MELSP {}\n\n'.format(model_encoder_melsp.lat_dim))
    hf.write('#define FEATURE_LAT_DIM_EXCIT {}\n\n'.format(model_encoder_excit.lat_dim))
    hf.write('#define FEATURE_N_SPK {}\n\n'.format(n_spk))
    hf.write('#define FEATURE_SPK_DIM {}\n\n'.format(model_spkidtr.emb_dim))
    hf.write('#define FEATURE_N_WEIGHT_EMBED_SPK {}\n\n'.format(model_spkidtr.n_weight_emb))
    hf.write('#define FEATURE_DIM_EMBED_SPK {}\n\n'.format(model_spkidtr.dim_weight_emb))
    hf.write('#define FEATURE_SPK_DIM_TV {}\n\n'.format(model_spk.dim_out))
    hf.write('#define FEATURE_RED_DIM {}\n\n'.format(model_decoder_melsp.red_dim))
    hf.write('#define FEAT_ENC_MELSP_DIM {}\n\n'.format(feat_enc_melsp_dim))
    hf.write('#define FEAT_ENC_EXCIT_DIM {}\n\n'.format(feat_enc_excit_dim))
    hf.write('#define FEAT_SPK_DIM {}\n\n'.format(feat_spk_dim))
    hf.write('#define FEAT_DEC_EXCIT_DIM {}\n\n'.format(feat_dec_excit_dim))
    hf.write('#define FEAT_DEC_MELSP_DIM {}\n\n'.format(feat_dec_melsp_dim))
    hf.write('#define FEATURE_CONV_ENC_STATE_SIZE {}\n\n'.format(enc_melsp_state_size))
    hf.write('#define FEATURE_CONV_VC_DELAY {}\n\n'.format(model_encoder_melsp.pad_right+model_spk.pad_right+model_decoder_excit.pad_right+model_decoder_melsp.pad_right))
    hf.write('#define ENC_CONV_KERNEL_1 {}\n\n'.format(model_encoder_melsp.kernel_size-1))
    hf.write('#define SPK_CONV_KERNEL_1 {}\n\n'.format(model_spk.kernel_size-1))
    hf.write('#define DEC_EXCIT_CONV_KERNEL_1 {}\n\n'.format(model_decoder_excit.kernel_size-1))
    hf.write('#define DEC_MELSP_CONV_KERNEL_1 {}\n\n'.format(model_decoder_melsp.kernel_size-1))
    hf.write('#define N_INIT_STATE {}\n\n'.format((model_encoder_melsp.pad_left+model_spk.pad_left+model_decoder_excit.pad_left+model_decoder_melsp.pad_left)*config_cycvae.n_half_cyc-model_encoder_melsp.pad_left))
    if config_cycvae.s_conv_flag:
        hf.write('#define CONV_FC_RELU\n\n')

    hf.write('typedef struct {\n')
    hf.write('  float feature_conv_enc_melsp_state[FEATURE_CONV_ENC_MELSP_STATE_SIZE];\n')
    hf.write('  float feature_conv_enc_excit_state[FEATURE_CONV_ENC_EXCIT_STATE_SIZE];\n')
    hf.write('  float feature_conv_spk_state[FEATURE_CONV_SPK_STATE_SIZE];\n')
    hf.write('  float feature_conv_dec_excit_state[FEATURE_CONV_DEC_EXCIT_STATE_SIZE];\n')
    hf.write('  float feature_conv_dec_melsp_state[FEATURE_CONV_DEC_MELSP_STATE_SIZE];\n')
    hf.write('  float gru_enc_melsp_state[SPARSE_GRU_ENC_MELSP_STATE_SIZE];\n')
    hf.write('  float gru_enc_excit_state[SPARSE_GRU_ENC_EXCIT_STATE_SIZE];\n')
    hf.write('  float gru_spk_state[GRU_SPK_STATE_SIZE];\n')
    hf.write('  float gru_dec_excit_state[GRU_DEC_EXCIT_STATE_SIZE];\n')
    hf.write('  float gru_dec_melsp_state[SPARSE_GRU_DEC_MELSP_STATE_SIZE];\n')
    hf.write('} CycleVAEMelspExcitSpkNNetState;\n')

    hf.write('\n\n#endif\n')

    f.close()
    hf.close()

    ## Dump high-pass filter coeffs, half hanning-window coeffs, mel-filterbank, and mu-law 10 table here
    ## hpassfilt.h, halfwin.h, melfb.h, mu_law_10_table.h
    fs = args.fs
    fftl = args.fftl
    shiftms = args.shiftms
    winms = args.winms
    print(f'{fs} {fftl} {shiftms} {winms}')

    hop_length = int((fs/1000)*shiftms)
    win_length = int((fs/1000)*winms)
    print(f'{hop_length} {win_length}')

    cutoff = args.highpass_cutoff
    #cutoff = HIGHPASS_CUTOFF
    nyq = fs // 2
    norm_cutoff = cutoff / nyq
    taps = HPASS_FILTER_TAPS
    print(f'{cutoff} {nyq} {norm_cutoff} {taps}')

    mel_dim = config_cycvae.mel_dim
    print(f'{mel_dim}')

    cfile = "freq_conf.h"
    hf = open(cfile, 'w')
    hf.write('/*This file is automatically generated from model configuration*/\n\n')
    hf.write('#ifndef FREQ_CONF_H\n#define FREQ_CONF_H\n\n')
    hf.write('#define SAMPLING_FREQUENCY {}\n\n'.format(fs))
    hf.write('#define FRAME_SHIFT {}\n\n'.format(hop_length))
    hf.write('#define WINDOW_LENGTH {}\n\n'.format(win_length))
    hf.write('#define FFT_LENGTH {}\n\n'.format(fftl))
    hf.write('#define HPASS_FILT_TAPS {}\n\n'.format(taps))
    hf.write('#define MEL_DIM {}\n\n'.format(mel_dim))
    hf.write('\n\n#endif\n')
    hf.close()

    #periodic hanning window, starts with 0, even N-length
    ## [0,1st,2nd,...,(N/2-1)-th,1,(N/2-1)-th,...,2nd,1st]
    #take only coefficients 1st until (N/2-1)th because 0th is 0 and (N/2)-th is 1
    #the (N/2-1) right side is reflected for (N/2-1)th until 1st
    #so the total length is (N/2-1)*2 [left--right=reflect] + 1 [0th=0] + 1 [(N-2)th=1] = N [win_length]
    half_hann_win = windows.hann(win_length, sym=False)[1:(win_length//2)] #(N-1)/2
    cfile = "halfwin.h"
    hf = open(cfile, 'w')
    hf.write('/*This file is automatically generated from scipy function*/\n\n')
    hf.write('#ifndef HALF_WIN_H\n#define HALF_WIN_H\n\n')
    printVector(hf, half_hann_win, "halfwin")
    hf.write('\n\n#endif\n')
    hf.close()
    
    # high-pass filter
    filt = firwin(taps, norm_cutoff, pass_zero=False) #taps
    cfile = "hpassfilt.h"
    hf = open(cfile, 'w')
    hf.write('/*This file is automatically generated from scipy function*/\n\n')
    hf.write('#ifndef HPASS_FILT_H\n#define HPASS_FILT_H\n\n')
    printVector(hf, filt, "hpassfilt")
    hf.write('\n\n#endif\n')
    hf.close()

    # mel-filterbank
    melfb = filters.mel(fs, fftl, n_mels=mel_dim) #mel_dimx(n_fft//2+1)
    cfile = "melfb.h"
    hf = open(cfile, 'w')
    hf.write('/*This file is automatically generated from librosa function*/\n\n')
    hf.write('#ifndef MEL_FB_H\n#define MEL_FB_H\n\n')
    printVector(hf, melfb, "melfb")
    hf.write('\n\n#endif\n')
    hf.close()

    # mu-law 10-bit table
    mu_law_10_table = np.array([decode_mu_law(x, mu=config.n_quantize) for x in range(config.n_quantize)])
    cfile = "mu_law_10_table.h"
    hf = open(cfile, 'w')
    hf.write('/*This file is automatically generated from numpy function*/\n\n')
    hf.write('#ifndef MU_LAW_10_TABLE_H\n#define MU_LAW_10_TABLE_H\n\n')
    printVector(hf, mu_law_10_table, "mu_law_10_table")
    hf.write('\n\n#endif\n')
    hf.close()


if __name__ == "__main__":
    main()
