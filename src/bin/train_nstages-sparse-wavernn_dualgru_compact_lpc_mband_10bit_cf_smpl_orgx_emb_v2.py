#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2021 Patrick Lumban Tobing (Nagoya University)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

# Main training code in stage 4 of MWDLP `run.sh`.

# Filename: train_nstages-sparse-wavernn_dualgru_compact_lpc_mband_10bit_cf_smpl_orgx_emb.py
#   - train: Training controller
#   - nstages-sparse-wavernn: N-stage spasification on WaveRNN
#   - dualgru_compact_lpc_: ?
#   - mband: Multiband signal processing
#   - 10bit_cf: 10bit coarse-fine output
#   - smpl_orgx_emb: ?
 
from __future__ import division
from __future__ import print_function

import argparse
from dateutil.relativedelta import relativedelta
from distutils.util import strtobool
import logging
import os
import sys
import time
import datetime

from collections import defaultdict
from tensorboardX import SummaryWriter

from decimal import Decimal

import numpy as np
import six
import torch
import librosa

from pqmf import PQMF

from torchvision import transforms
from torch.utils.data import DataLoader

import torch.nn.functional as F

from utils import find_files
from utils import read_hdf5
from utils import read_txt
from vcneuvoco import GRU_WAVE_DECODER_DUALGRU_COMPACT_MBAND_CF, encode_mu_law
from vcneuvoco import decode_mu_law_torch, MultiResolutionSTFTLoss
#from radam import RAdam
import torch_optimizer as optim

from dataset import FeatureDatasetNeuVoco, padding

#import warnings
#warnings.filterwarnings('ignore')

#np.set_printoptions(threshold=np.inf)
#torch.set_printoptions(threshold=np.inf)


def data_generator(dataloader, device, batch_size, upsampling_factor, limit_count=None, n_bands=10):
    """TRAINING BATCH GENERATOR
    Wrap PyTorch dataloader.
    
    Args:
        wav_list (str): list of wav files
        feat_list (str): list of feat files
        batch_size (int): Length of a partial waveform
        wav_transform (func): preprocessing function for waveform
        limit_count: The maximum number of item the Generator will yeild (Not used now (None=Infinite))

    Return:
        (object): generator instance
            Returns of instance:
                x::(B, T) - Batch of fullband waveforms (batch of x_org)
                xs:(B, T, S) - Batch of subband waveform stacks (batch of x_org_band)
                xs_c (== batch_x_c): Coarse?
                xs_f (== batch_x_f): Fine?
                feat (== batch_feat)
                c_idx
                idx (== utt_idx)
                featfile
                x_bs
                f_bs
                x_ss
                f_ss
                n_batch_utt
                del_index_utt
                max_slen: Maximum `slen` in a batch
                max_flen: Maximum `flen` in a batch
                idx_select
                idx_select_full
                slens_acc
                flens_acc
    """
    while True:
        # process over all of files
        c_idx = 0
        count = 0
        upsampling_factor_bands = upsampling_factor // n_bands
        for idx, batch in enumerate(dataloader):
            slens = batch['slen'].data.numpy() # (B, )
            flens = batch['flen'].data.numpy() # (B, )
            max_slen = np.max(slens) ## maximum waveform length
            max_flen = np.max(flens) ## minumum featuresSequence length

            ## Mainly only Sliced (could be deletion with del_index_utt)
            x = batch['x_org'][:,:max_slen*n_bands].to(device) # (B, T)
            xs = batch['x_org_band'][:,:max_slen].to(device) # (B, T, Subband)
            xs_c = batch['x_c'][:,:max_slen].to(device) #
            xs_f = batch['x_f'][:,:max_slen].to(device) #
            feat = batch['feat'][:,:max_flen].to(device) #
            ## /
            
            #spcidx_s = batch['spcidx_s_e'][0]
            #spcidx_s = batch['spcidx_s'].data.numpy()
            #spcidx_e = batch['spcidx_s_e'][1]
            featfiles = batch['featfile']
            #cs = batch['c'].to(device)
            n_batch_utt = feat.size(0) # Actual batch size, which could be smaller than config (last elements in dataset)

            len_frm = max_flen
            ## SampleSize, BatchSize...?
            x_ss = 0
            f_ss = 0
            # `x_bs` is fixed here.
            x_bs = batch_size*upsampling_factor_bands
            f_bs = batch_size
            ##
            delta = batch_size*upsampling_factor_bands
            delta_frm = batch_size
            slens_acc = np.array(slens)
            flens_acc = np.array(flens) # (B, )
            
            # Iterative yield within a batch
            while True:
                del_index_utt = []
                idx_select = []
                idx_select_full = []

                # Delete empty items (length==0 sequences) in a batch
                for i in range(n_batch_utt):
                    # Item No.i in a batch, whether the item is empty or not.
                    if flens_acc[i] <= 0:
                        del_index_utt.append(i)
                if len(del_index_utt) > 0:
                    # delete specified items in a batch
                    x = torch.FloatTensor(np.delete(x.cpu().data.numpy(), del_index_utt, axis=0)).to(device)
                    xs = torch.FloatTensor(np.delete(xs.cpu().data.numpy(), del_index_utt, axis=0)).to(device)
                    xs_c = torch.LongTensor(np.delete(xs_c.cpu().data.numpy(), del_index_utt, axis=0)).to(device)
                    xs_f = torch.LongTensor(np.delete(xs_f.cpu().data.numpy(), del_index_utt, axis=0)).to(device)
                    #cs = torch.LongTensor(np.delete(cs.cpu().data.numpy(), del_index_utt, axis=0)).to(device)
                    feat = torch.FloatTensor(np.delete(feat.cpu().data.numpy(), del_index_utt, axis=0)).to(device)
                    featfiles = np.delete(featfiles, del_index_utt, axis=0)
                    slens_acc = np.delete(slens_acc, del_index_utt, axis=0)
                    flens_acc = np.delete(flens_acc, del_index_utt, axis=0)
                    #spcidx_s = np.delete(spcidx_s, del_index_utt, axis=0)
                    # n_batch_utt: Number of effective (not empty) items in a batch
                    n_batch_utt -= len(del_index_utt)
                # /Delete empty items

#                 idx_select
                for i in range(n_batch_utt):
                    if flens_acc[i] < f_bs:
                        idx_select.append(i)
                if len(idx_select) > 0:
                    idx_select_full = torch.LongTensor(np.delete(np.arange(n_batch_utt), idx_select, axis=0)).to(device)
                    idx_select = torch.LongTensor(idx_select).to(device)
                yield x, xs, xs_c, xs_f, feat, c_idx, idx, featfiles, x_bs, f_bs, x_ss, f_ss, n_batch_utt, del_index_utt, max_slen, \
                    max_flen, idx_select, idx_select_full, slens_acc, flens_acc
                for i in range(n_batch_utt):
                    slens_acc[i] -= delta
                    flens_acc[i] -= delta_frm

                # Yielded, count incremented.
                count += 1
                # Batch break candidate 1/2: limit_count argument
                if limit_count is not None and count > limit_count:
                    break
                # Batch loop judgement
                len_frm -= delta_frm
                if len_frm > 0:
                    x_ss += delta
                    f_ss += delta_frm
                # Batch break candidate 2/2:
                else:
                    break

            # Generator break candidate 1/1: limit_count argument (If limit-based batch break occured in batch loop, this also break whole generator)
            if limit_count is not None and count > limit_count:
                break
            c_idx += 1
        
        yield [], [], [], [], [], -1, -1, [], [], [], [], [], [], [], [], [], [], [], [], []

#         c_idx: total number of processed batches, idx: batch index
#         If all batch processed once, yield (c_idx, idx) = (-1, -1) before next generation loop start.

#         yield [], [],   [],   [],   [],    -1,  -1,        [],   [],   [],   [],   [],          [],            [],       [], \
#         yield  x, xs, xs_c, xs_f, feat, c_idx, idx, featfiles, x_bs, f_bs, x_ss, f_ss, n_batch_utt, del_index_utt, max_slen, \
#                           [],         [],              [],        [],        []
#                     max_flen, idx_select, idx_select_full, slens_acc, flens_acc

def save_checkpoint(checkpoint_dir, model_waveform, optimizer,
    min_eval_loss_ce_avg, min_eval_loss_ce_avg_std, min_eval_loss_err_avg, min_eval_loss_err_avg_std,
        min_eval_loss_l1_avg, min_eval_loss_l1_fb, err_flag, converge_checkpoint_flag,
        iter_idx, min_idx, numpy_random_state, torch_random_state, iterations):
    """FUNCTION TO SAVE CHECKPOINT

    Args:
        checkpoint_dir (str): directory to save checkpoint
        model (torch.nn.Module): pytorch model instance
        optimizer (Optimizer): pytorch optimizer instance
        iterations (int): number of current iterations
    """
    model_waveform.cpu()
    checkpoint = {
        "model_waveform": model_waveform.state_dict(),
        "optimizer": optimizer.state_dict(),
        "min_eval_loss_ce_avg": min_eval_loss_ce_avg,
        "min_eval_loss_ce_avg_std": min_eval_loss_ce_avg_std,
        "min_eval_loss_err_avg": min_eval_loss_err_avg,
        "min_eval_loss_err_avg_std": min_eval_loss_err_avg_std,
        "min_eval_loss_l1_avg": min_eval_loss_l1_avg,
        "min_eval_loss_l1_fb": min_eval_loss_l1_fb,
        "err_flag": err_flag,
        "converge_checkpoint_flag": converge_checkpoint_flag,
        "iter_idx": iter_idx,
        "last_epoch": iterations,
        "min_idx": min_idx,
        "numpy_random_state": numpy_random_state,
        "torch_random_state": torch_random_state,
        "iterations": iterations}
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)
    torch.save(checkpoint, checkpoint_dir + "/checkpoint-%d.pkl" % iterations)
    torch.save(checkpoint, checkpoint_dir + "/checkpoint-last.pkl")
    model_waveform.cuda()
    logging.info("%d-iter and last checkpoints created." % iterations)


def write_to_tensorboard(writer, steps, loss):
    """Write to tensorboard."""
    for key, value in loss.items():
        writer.add_scalar(key, value, steps)


def write_audio_to_tensorboard(writer, steps, waveform, sr):
    """Write an audio waveform to tensorboard."""
    writer.add_audio('audio', waveform, steps, sr)


def sparsify(model_waveform, iter_idx, t_start, t_end, interval, densities, densities_p=None):
    """
    N-stage sparsification, based on [LPCNet](https://github.com/mozilla/LPCNet/blob/master/src/lpcnet.py).
    Spasify a hidden state of `model_waveform.gru` in place.
    
    Args:
        model_waveform: Target model, which should contain `.gru` GRU unit.
        iter_idx: Current iteration index?
        t_start: Sparsification-start iteration?
        t_end: Sparsification-end iteration?
        interval: Interval between sparsification stages?
        densities: Target densities...?
        densities_p: Target densities related something...?
    """
    if iter_idx < t_start or ((iter_idx-t_start) % interval != 0 and iter_idx < t_end):
        pass
    else:
        logging.info('sparsify: %ld %ld %ld %ld' % (iter_idx, t_start, t_end, interval))
        p = model_waveform.gru.weight_hh_l0 #recurrent
        nb = p.shape[0] // p.shape[1]
        N = p.shape[0] // nb
        N_16 = N // 16
        ones = torch.diag(torch.ones(N, device=p.device))
        if densities_p is not None:
            for k in range(nb):
                density = densities[k]
                if iter_idx < t_end:
                    r = 1 - (iter_idx-t_start)/(t_end - t_start)
                    density = densities_p[k] - (densities_p[k]-densities[k])*(1 - r)**5
                logging.info('%ld: %lf %lf %lf' % (k+1, densities_p[k], densities[k], density))
                # Below is common step
                #recurrent weight
                A = p[k*N:(k+1)*N, :]
                #horizontal block structure (16) in input part, in real-time simultaneously computed for each 16 output using 2 registers (256x2 bits)
                L = (A - torch.diag(torch.diag(A))).transpose(1, 0).reshape(N, N_16, 16)
                S = torch.sum(L*L, -1)
                SS, _ = torch.sort(S.reshape(-1))
                thresh = SS[round(N*N_16*(1-density))]
                mask = torch.clamp(torch.repeat_interleave((S>=thresh).float(), 16, dim=1) + ones, max=1).transpose(1,0)
                p[k*N:(k+1)*N, :] = p[k*N:(k+1)*N, :]*mask # outputxinput
        else:
            for k in range(nb):
                density = densities[k]
                if iter_idx < t_end:
                    r = 1 - (iter_idx-t_start)/(t_end - t_start)
                    density = 1 - (1-densities[k])*(1 - r)**5
                logging.info('%ld: 1 %lf %lf' % (k+1, densities[k], density))
                # Below is common step
                #recurrent weight
                A = p[k*N:(k+1)*N, :]
                L = (A - torch.diag(torch.diag(A))).transpose(1, 0).reshape(N, N_16, 16)
                S = torch.sum(L*L, -1)
                SS, _ = torch.sort(S.reshape(-1))
                thresh = SS[round(N*N_16*(1-density))]
                mask = torch.clamp(torch.repeat_interleave((S>=thresh).float(), 16, dim=1) + ones, max=1).transpose(1,0)
                p[k*N:(k+1)*N, :] = p[k*N:(k+1)*N, :]*mask # outputxinput


def main():
    parser = argparse.ArgumentParser()
    # path setting
    parser.add_argument("--waveforms",
                        type=str, help="directory or list of wav files")
    parser.add_argument("--waveforms_eval",
                        type=str, help="directory or list of evaluation wav files")
    parser.add_argument("--feats", required=True,
                        type=str, help="directory or list of wav files")
    parser.add_argument("--feats_eval", required=True,
                        type=str, help="directory or list of evaluation feat files")
    parser.add_argument("--stats", required=True,
                        type=str, help="directory or list of evaluation wav files")
    parser.add_argument("--expdir", required=True,
                        type=str, help="directory to save the model")
    # network structure setting
    parser.add_argument("--upsampling_factor", default=120,
                        type=int, help="number of dimension of aux feats")
    parser.add_argument("--hidden_units_wave", default=896,
                        type=int, help="depth of dilation")
    parser.add_argument("--hidden_units_wave_2", default=32,
                        type=int, help="depth of dilation")
    parser.add_argument("--kernel_size_wave", default=7,
                        type=int, help="kernel size of dilated causal convolution")
    parser.add_argument("--dilation_size_wave", default=1,
                        type=int, help="kernel size of dilated causal convolution")
    parser.add_argument("--lpc", default=12,
                        type=int, help="kernel size of dilated causal convolution")
    parser.add_argument("--mcep_dim", default=50,
                        type=int, help="kernel size of dilated causal convolution")
    parser.add_argument("--right_size", default=0,
                        type=int, help="kernel size of dilated causal convolution")
    parser.add_argument("--s_dim", default=320,
                        type=int, help="kernel size of dilated causal convolution")
    parser.add_argument("--mid_dim", default=16,
                        type=int, help="kernel size of dilated causal convolution")
    parser.add_argument("--seg_conv_flag_wave", default=True,
                        type=strtobool, help="flag to use excit (U/V and F0) if using mel-spec")
    # network training setting
    parser.add_argument("--lr", default=1e-4,
                        type=float, help="learning rate")
    parser.add_argument("--batch_size", default=15,
                        type=int, help="batch size (if set 0, utterance batch will be used)")
    parser.add_argument("--step_count", default=4000000,
                        type=int, help="number of training steps")
    parser.add_argument("--do_prob", default=0,
                        type=float, help="dropout probability")
    parser.add_argument("--n_workers", default=2,
                        type=int, help="batch size (if set 0, utterance batch will be used)")
    parser.add_argument("--n_quantize", default=1024,
                        type=int, help="batch size (if set 0, utterance batch will be used)")
    parser.add_argument("--causal_conv_wave", default=False,
                        type=strtobool, help="batch size (if set 0, utterance batch will be used)")
    parser.add_argument("--n_stage", default=4,
                        type=int, help="number of sparsification stages")
    parser.add_argument("--t_start", default=20000,
                        type=int, help="iter idx to start sparsify")
    parser.add_argument("--t_end", default=4500000,
                        type=int, help="iter idx to finish densitiy sparsify")
    parser.add_argument("--interval", default=100,
                        type=int, help="interval in finishing densitiy sparsify")
    parser.add_argument("--densities", default="0.05-0.05-0.2",
                        type=str, help="final densitiy of reset, update, new hidden gate matrices")
    parser.add_argument("--n_bands", default=10,
                        type=int, help="number of bands")
    parser.add_argument("--fs", default=24000,
                        type=int, help="sampling rate")
    # Currently not used in `run.sh` of both MWDLP and CycVAE-MWDLP-VC
    parser.add_argument("--with_excit", default=False,
                        type=strtobool, help="flag to use excit (U/V and F0) if using mel-spec")
    # other setting
    parser.add_argument("--pad_len", default=3000,
                        type=int, help="seed number")
    parser.add_argument("--save_interval_iter", default=5000,
                        type=int, help="interval steps to logr")
    parser.add_argument("--save_interval_epoch", default=10,
                        type=int, help="interval steps to logr")
    parser.add_argument("--log_interval_steps", default=50,
                        type=int, help="interval steps to logr")
    parser.add_argument("--seed", default=1,
                        type=int, help="seed number")
    parser.add_argument("--resume", default=None,
                        type=str, help="model path to restart training")
    parser.add_argument("--pretrained", default=None,
                        type=str, help="model path to restart training")
    parser.add_argument("--string_path", default=None,
                        type=str, help="model path to restart training")
    parser.add_argument("--GPU_device", default=None,
                        type=int, help="selection of GPU device")
    parser.add_argument("--verbose", default=1,
                        type=int, help="log level")
    args = parser.parse_args()

    if args.GPU_device is not None:
        os.environ["CUDA_DEVICE_ORDER"]     = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"]  = str(args.GPU_device)

    # make experimental directory
    if not os.path.exists(args.expdir):
        os.makedirs(args.expdir)

    # set log level
    if args.verbose == 1:
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S',
                            filename=args.expdir + "/train.log")
        logging.getLogger().addHandler(logging.StreamHandler())
    elif args.verbose > 1:
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S',
                            filename=args.expdir + "/train.log")
        logging.getLogger().addHandler(logging.StreamHandler())
    else:
        logging.basicConfig(level=logging.WARN,
                            format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S',
                            filename=args.expdir + "/train.log")
        logging.getLogger().addHandler(logging.StreamHandler())
        logging.warn("logging is disabled.")

    # fix seed
    os.environ['PYTHONHASHSEED'] = str(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if str(device) == "cpu":
        raise ValueError('ERROR: Training by CPU is not acceptable.')

    torch.backends.cudnn.benchmark = True #faster

    # Conditioning input type: Mel-spectrogram or Cepstrum
    ## excit_dim: Dimension of excitation signals (U/V & Fo) as conditioning inputs
    # Mel-spectrogram
    # string_path=="/log_1pmelmagsp" in `run.sh` of both MWDLP and CycVAE-MWDLP-VC.
    if 'mel' in args.string_path:
        # Only mel-spec
        # args.with_excit==False in `run.sh` of both MWDLP and CycVAE-MWDLP-VC.
        if not args.with_excit:
            mean_stats = torch.FloatTensor(read_hdf5(args.stats, "/mean_melsp"))
            scale_stats = torch.FloatTensor(read_hdf5(args.stats, "/scale_melsp"))
            args.excit_dim = 0
            with_excit = False
        # mel-spec + excitations (vuv & log_fo)
        else:
            mean_stats = torch.FloatTensor(np.r_[read_hdf5(args.stats, "/mean_feat_mceplf0cap")[:2], read_hdf5(args.stats, "/mean_melsp")])
            scale_stats = torch.FloatTensor(np.r_[read_hdf5(args.stats, "/scale_feat_mceplf0cap")[:2], read_hdf5(args.stats, "/scale_melsp")])
            args.excit_dim = 2
            with_excit = True
        #mean_stats = torch.FloatTensor(np.r_[read_hdf5(args.stats, "/mean_feat_mceplf0cap")[:6], read_hdf5(args.stats, "/mean_melsp")])
        #scale_stats = torch.FloatTensor(np.r_[read_hdf5(args.stats, "/scale_feat_mceplf0cap")[:6], read_hdf5(args.stats, "/scale_melsp")])
        #args.excit_dim = 6
    # Cepstrum
    else:
        with_excit = False
        mean_stats = torch.FloatTensor(read_hdf5(args.stats, "/mean_"+args.string_path.replace("/","")))
        scale_stats = torch.FloatTensor(read_hdf5(args.stats, "/scale_"+args.string_path.replace("/","")))
        if mean_stats.shape[0] > args.mcep_dim+2:
            if 'feat_org_lf0' in args.string_path:
                args.cap_dim = mean_stats.shape[0]-(args.mcep_dim+2)
                args.excit_dim = 2+args.cap_dim
            else:
                args.cap_dim = mean_stats.shape[0]-(args.mcep_dim+3)
                args.excit_dim = 2+1+args.cap_dim
        else:
            args.cap_dim = None
            args.excit_dim = 2

    # save args as conf
    args.n_quantize = 1024
    args.cf_dim = int(np.sqrt(args.n_quantize))
    args.half_n_quantize = args.n_quantize // 2
    args.c_pad = args.half_n_quantize // args.cf_dim
    args.f_pad = args.half_n_quantize % args.cf_dim
    args.step_fact = 3.3644859813084112149532710280374
    torch.save(args, args.expdir + "/model.conf")

    # define network
    model_waveform = GRU_WAVE_DECODER_DUALGRU_COMPACT_MBAND_CF(
        feat_dim=args.mcep_dim+args.excit_dim,
        upsampling_factor=args.upsampling_factor,
        hidden_units=args.hidden_units_wave,
        hidden_units_2=args.hidden_units_wave_2,
        kernel_size=args.kernel_size_wave,
        dilation_size=args.dilation_size_wave,
        n_quantize=args.n_quantize,
        causal_conv=args.causal_conv_wave,
        lpc=args.lpc,
        right_size=args.right_size,
        n_bands=args.n_bands,
        pad_first=True,
        mid_dim=args.mid_dim,
        s_dim=args.s_dim,
        emb_flag=True,
        seg_conv_flag=args.seg_conv_flag_wave,
        do_prob=args.do_prob)
    logging.info(model_waveform)
    pqmf = PQMF(args.n_bands)
    
    # FFT config based on sampling rate
    fft_sizes = [256, 128, 64, 32, 32]
    if args.fs == 22050 or args.fs == 44100:
        hop_sizes = [88, 44, 22, 11, 8]
    else:
        hop_sizes = [80, 40, 20, 10, 8]
    win_lengths = [round(elmt*2.5) for elmt in hop_sizes]
    if args.fs == 8000:
        fft_sizes_fb = [512, 256, 128, 64, 64]
        hop_sizes_fb = [160, 80, 40, 20, 16]
    elif args.fs <= 24000:
        if args.fs == 16000:
            fft_sizes_fb = [1024, 512, 256, 128, 128]
            hop_sizes_fb = [320, 160, 80, 40, 32]
        elif args.fs == 22050:
            fft_sizes_fb = [2048, 1024, 512, 256, 128]
            hop_sizes_fb = [440, 220, 110, 55, 44]
        else:
            fft_sizes_fb = [2048, 1024, 512, 256, 128]
            hop_sizes_fb = [480, 240, 120, 60, 48]
    else:
        fft_sizes_fb = [4096, 2048, 1024, 512, 256]
        if args.fs == 44100:
            hop_sizes_fb = [880, 440, 220, 110, 88]
        else:
            hop_sizes_fb = [960, 480, 240, 120, 96]
    win_lengths_fb = [round(elmt*2.5) for elmt in hop_sizes_fb]
    # /FFT config based on sampling rate

    # Loss criterions
    criterion_stft = MultiResolutionSTFTLoss(
        fft_sizes = fft_sizes,
        hop_sizes = hop_sizes,
        win_lengths = win_lengths,
    )
    criterion_stft_fb = MultiResolutionSTFTLoss(
        fft_sizes = fft_sizes_fb,
        hop_sizes = hop_sizes_fb,
        win_lengths = win_lengths_fb,
    )
    criterion_ce = torch.nn.CrossEntropyLoss(reduction='none')
    criterion_l1 = torch.nn.L1Loss(reduction='none')
    # /Loss criterions
    
    indices_1hot = torch.FloatTensor(np.arange(args.cf_dim))

    # send to gpu
    if torch.cuda.is_available():
        model_waveform.cuda()
        pqmf.cuda()
        criterion_stft.cuda()
        criterion_stft_fb.cuda()
        criterion_ce.cuda()
        criterion_l1.cuda()
        indices_1hot = indices_1hot.cuda()
        if args.pretrained is None:
            mean_stats = mean_stats.cuda()
            scale_stats = scale_stats.cuda()
    else:
        logging.error("gpu is not available. please check the setting.")
        sys.exit(1)
    # /send to gpu

    # Logging of configs
    logging.info(indices_1hot)
    logging.info(criterion_stft.fft_sizes)
    logging.info(criterion_stft.hop_sizes)
    logging.info(criterion_stft.win_lengths)
    logging.info(criterion_stft_fb.fft_sizes)
    logging.info(criterion_stft_fb.hop_sizes)
    logging.info(criterion_stft_fb.win_lengths)
    logging.info(f'{pqmf.subbands} {pqmf.A} {pqmf.taps} {pqmf.cutoff_ratio} {pqmf.beta}')
    # /Logging of configs

    model_waveform.train()

    if args.pretrained is None:
        model_waveform.scale_in.weight = torch.nn.Parameter(torch.unsqueeze(torch.diag(1.0/scale_stats.data),2))
        model_waveform.scale_in.bias = torch.nn.Parameter(-(mean_stats.data/scale_stats.data))

    parameters = filter(lambda p: p.requires_grad, model_waveform.parameters())
    parameters = sum([np.prod(p.size()) for p in parameters]) / 1000000
    logging.info('Trainable Parameters (waveform): %.3f million' % parameters)

    for param in model_waveform.parameters():
        param.requires_grad = True
    for param in model_waveform.scale_in.parameters():
        param.requires_grad = False
    if args.lpc > 0:
        for param in model_waveform.logits.parameters():
            param.requires_grad = False

    # Setup optimizers
    module_list = list(model_waveform.conv.parameters()) + list(model_waveform.conv_s_c.parameters())
    module_list += list(model_waveform.embed_c_wav.parameters()) + list(model_waveform.embed_f_wav.parameters())
    module_list += list(model_waveform.gru.parameters())
    module_list += list(model_waveform.gru_2.parameters()) + list(model_waveform.out.parameters())
    module_list += list(model_waveform.gru_f.parameters()) + list(model_waveform.out_f.parameters())
    if args.lpc > 0:
        module_list += list(model_waveform.logits_c.parameters()) + list(model_waveform.logits_f.parameters())
    #module_list += list(model_waveform.logits_sgns_c.parameters()) + list(model_waveform.logits_mags_c.parameters())
    #module_list += list(model_waveform.logits_sgns_f.parameters()) + list(model_waveform.logits_mags_f.parameters())
    optimizer = optim.RAdam(
        module_list,
        lr= args.lr,
        betas=(0.9, 0.999),
        eps=1e-8,
        weight_decay=0,
    )
    # /Setup optimizers

    epoch_idx = 0

    # resume
    if args.pretrained is not None and args.resume is None:
        checkpoint = torch.load(args.pretrained)
        model_waveform.load_state_dict(checkpoint["model_waveform"], strict=False)
        epoch_idx = checkpoint["iterations"]
        logging.info("pretrained from %d-iter checkpoint." % epoch_idx)
        epoch_idx = 0
    elif args.resume is not None:
        checkpoint = torch.load(args.resume)
        model_waveform.load_state_dict(checkpoint["model_waveform"])
        optimizer.load_state_dict(checkpoint["optimizer"])
        epoch_idx = checkpoint["iterations"]
        logging.info("restored from %d-iter checkpoint." % epoch_idx)
    # /resume

    def zero_wav_pad(x): return padding(x, args.pad_len*(args.upsampling_factor // args.n_bands), value=args.half_n_quantize)
    def zero_wav_org_pad(x): return padding(x, args.pad_len*args.upsampling_factor, value=args.half_n_quantize)
    def zero_feat_pad(x): return padding(x, args.pad_len, value=None)
    pad_wav_transform = transforms.Compose([zero_wav_pad])
    pad_wav_org_transform = transforms.Compose([zero_wav_org_pad])
    pad_feat_transform = transforms.Compose([zero_feat_pad])

    wav_transform = transforms.Compose([lambda x: encode_mu_law(x, args.n_quantize)])

    # define generator training
    if os.path.isdir(args.waveforms):
        filenames = sorted(find_files(args.waveforms, "*.wav", use_dir_name=False))
        wav_list = [args.waveforms + "/" + filename for filename in filenames]
    elif os.path.isfile(args.waveforms):
        wav_list = read_txt(args.waveforms)
    else:
        logging.error("--waveforms should be directory or list.")
        sys.exit(1)
    if os.path.isdir(args.feats):
        feat_list = [args.feats + "/" + filename for filename in filenames]
    elif os.path.isfile(args.feats):
        feat_list = read_txt(args.feats)
    else:
        logging.error("--feats should be directory or list.")
        sys.exit(1)
    assert len(wav_list) == len(feat_list)
    n_train_data = len(wav_list)
    batch_size_utt = 8
    logging.info("number of training_data -- batch_size = %d -- %d" % (n_train_data, batch_size_utt))
    dataset = FeatureDatasetNeuVoco(wav_list, feat_list, pad_wav_transform, pad_feat_transform, args.upsampling_factor, 
                    args.string_path, wav_transform=wav_transform, n_bands=args.n_bands, with_excit=with_excit, cf_dim=args.cf_dim, spcidx=True,
                        pad_left=model_waveform.pad_left, pad_right=model_waveform.pad_right, worgx_band_flag=True, worgx_flag=True, pad_wav_org_transform=pad_wav_org_transform)
    # Dataset flags: worgx_band_flag=True, worgx_flag=True, wrec_flag=defaultTrue, with_excit=False
    dataloader = DataLoader(dataset, batch_size=batch_size_utt, shuffle=True, num_workers=args.n_workers)
    # Infinite generator
    n_iter_batch = n_train_data // batch_size_utt
    if n_train_data % batch_size_utt > 0:
        n_iter_batch += 1
    logging.info(f'n_iter_batch {n_iter_batch}')
    #generator = data_generator(dataloader, device, args.batch_size, args.upsampling_factor, limit_count=1, n_bands=args.n_bands)
    #generator = data_generator(dataloader, device, args.batch_size, args.upsampling_factor, limit_count=5, n_bands=args.n_bands)
    #generator = data_generator(dataloader, device, args.batch_size, args.upsampling_factor, limit_count=20, n_bands=args.n_bands)
    generator = data_generator(dataloader, device, args.batch_size, args.upsampling_factor, limit_count=None, n_bands=args.n_bands)

    # define generator evaluation
    if os.path.isdir(args.waveforms_eval):
        filenames = sorted(find_files(args.waveforms_eval, "*.wav", use_dir_name=False))
        wav_list_eval = [args.waveforms + "/" + filename for filename in filenames]
    elif os.path.isfile(args.waveforms_eval):
        wav_list_eval = read_txt(args.waveforms_eval)
    else:
        logging.error("--waveforms_eval should be directory or list.")
        sys.exit(1)
    if os.path.isdir(args.feats_eval):
        feat_list_eval = [args.feats_eval + "/" + filename for filename in filenames]
    elif os.path.isfile(args.feats):
        feat_list_eval = read_txt(args.feats_eval)
    else:
        logging.error("--feats_eval should be directory or list.")
        sys.exit(1)
    assert len(wav_list_eval) == len(feat_list_eval)
    n_eval_data = len(feat_list_eval)
    if n_eval_data > 14:
        batch_size_utt_eval = round(n_eval_data/10)
    else:
        batch_size_utt_eval = 1
    if batch_size_utt_eval > 200:
        batch_size_utt_eval = 200
    logging.info("number of evaluation_data -- batch_size_eval = %d -- %d" % (n_eval_data, batch_size_utt_eval))
    dataset_eval = FeatureDatasetNeuVoco(wav_list_eval, feat_list_eval, pad_wav_transform, pad_feat_transform, args.upsampling_factor, 
                    args.string_path, wav_transform=wav_transform, n_bands=args.n_bands, with_excit=with_excit, cf_dim=args.cf_dim, spcidx=True,
                        pad_left=model_waveform.pad_left, pad_right=model_waveform.pad_right, worgx_band_flag=True, worgx_flag=True, pad_wav_org_transform=pad_wav_org_transform)
    # Same as training dataset except for `wav_list_eval` & `feat_list_eval`
    # dataset flags: worgx_band_flag=True, worgx_flag=True, wrec_flag=defaultTrue
    dataloader_eval = DataLoader(dataset_eval, batch_size=batch_size_utt_eval, shuffle=False, num_workers=args.n_workers)
    # Infinite generator
    generator_eval = data_generator(dataloader_eval, device, args.batch_size, args.upsampling_factor, limit_count=None, n_bands=args.n_bands)

    writer = SummaryWriter(args.expdir)
    total_train_loss = defaultdict(list)
    total_eval_loss = defaultdict(list)

    density_deltas_ = args.densities.split('-')
    density_deltas = [None]*len(density_deltas_)
    for i in range(len(density_deltas_)):
        density_deltas[i] = (1-float(density_deltas_[i]))/args.n_stage
    t_deltas = [None]*args.n_stage
    t_starts = [None]*args.n_stage
    t_ends = [None]*args.n_stage
    densities = [None]*args.n_stage
    t_delta = args.t_end - args.t_start + 1
    if args.n_stage > 3:
        t_deltas[0] = round((1/2)*0.2*t_delta)
    else:
        t_deltas[0] = round(0.2*t_delta)
    t_starts[0] = args.t_start
    t_ends[0] = args.t_start + t_deltas[0] - 1
    densities[0] = [None]*len(density_deltas)
    for j in range(len(density_deltas)):
        densities[0][j] = 1-density_deltas[j]
    for i in range(1,args.n_stage):
        if i < args.n_stage-1:
            if args.n_stage > 3:
                if i < 2:
                    t_deltas[i] = round((1/2)*0.2*t_delta)
                else:
                    if args.n_stage > 4:
                        t_deltas[i] = round((1/2)*0.3*t_delta)
                    else:
                        t_deltas[i] = round(0.3*t_delta)
            else:
                t_deltas[i] = round(0.3*t_delta)
        else:
            t_deltas[i] = round(0.5*t_delta)
        t_starts[i] = t_ends[i-1] + 1
        t_ends[i] = t_starts[i] + t_deltas[i] - 1
        densities[i] = [None]*len(density_deltas)
        if i < args.n_stage-1:
            for j in range(len(density_deltas)):
                densities[i][j] = densities[i-1][j]-density_deltas[j]
        else:
            for j in range(len(density_deltas)):
                densities[i][j] = float(density_deltas_[j])
    logging.info(t_delta)
    logging.info(t_deltas)
    logging.info(t_starts)
    logging.info(t_ends)
    logging.info(args.interval)
    logging.info(densities)
    idx_stage = 0

    # train
    total = 0
    iter_count = 0
    pad_left = model_waveform.pad_left
    pad_right = model_waveform.pad_right
    batch_loss_ce = [None]*args.n_bands
    batch_loss_err = [None]*args.n_bands
    batch_loss_ce_f = [None]*args.n_bands
    batch_loss_err_f = [None]*args.n_bands
    batch_loss_fro = [None]*args.n_bands
    batch_loss_l1 = [None]*args.n_bands
    loss_ce = [None]*args.n_bands
    loss_err = [None]*args.n_bands
    loss_ce_f = [None]*args.n_bands
    loss_err_f = [None]*args.n_bands
    loss_fro = [None]*args.n_bands
    loss_l1 = [None]*args.n_bands
    loss_ce_avg = []
    loss_err_avg = []
    loss_ce_c_avg = []
    loss_err_c_avg = []
    loss_ce_f_avg = []
    loss_err_f_avg = []
    loss_fro_avg = []
    loss_l1_avg = []
    loss_fro_fb = []
    loss_l1_fb = []
    for i in range(args.n_bands):
        loss_ce[i] = []
        loss_err[i] = []
        loss_ce_f[i] = []
        loss_err_f[i] = []
        loss_fro[i] = []
        loss_l1[i] = []
    eval_loss_ce = [None]*args.n_bands
    eval_loss_ce_std = [None]*args.n_bands
    eval_loss_err = [None]*args.n_bands
    eval_loss_err_std = [None]*args.n_bands
    eval_loss_ce_f = [None]*args.n_bands
    eval_loss_ce_f_std = [None]*args.n_bands
    eval_loss_err_f = [None]*args.n_bands
    eval_loss_err_f_std = [None]*args.n_bands
    eval_loss_fro = [None]*args.n_bands
    eval_loss_fro_std = [None]*args.n_bands
    eval_loss_l1 = [None]*args.n_bands
    eval_loss_l1_std = [None]*args.n_bands
    min_eval_loss_ce = [None]*args.n_bands
    min_eval_loss_ce_std = [None]*args.n_bands
    min_eval_loss_err = [None]*args.n_bands
    min_eval_loss_err_std = [None]*args.n_bands
    min_eval_loss_ce_f = [None]*args.n_bands
    min_eval_loss_ce_f_std = [None]*args.n_bands
    min_eval_loss_err_f = [None]*args.n_bands
    min_eval_loss_err_f_std = [None]*args.n_bands
    min_eval_loss_fro = [None]*args.n_bands
    min_eval_loss_fro_std = [None]*args.n_bands
    min_eval_loss_l1 = [None]*args.n_bands
    min_eval_loss_l1_std = [None]*args.n_bands
    min_eval_loss_ce_avg = 99999999.99
    min_eval_loss_ce_avg_std = 99999999.99
    min_eval_loss_err_avg = 99999999.99
    min_eval_loss_err_avg_std = 99999999.99
    min_eval_loss_l1_avg = 99999999.99
    min_eval_loss_l1_fb = 99999999.99
    iter_idx = 0
    min_idx = -1
    err_flag = False
    change_min_flag = False
    sparse_min_flag = False
    sparse_check_flag = False
    
    # resume
    converge_checkpoint_flag = False
    if args.resume is not None:
        np.random.set_state(checkpoint["numpy_random_state"])
        torch.set_rng_state(checkpoint["torch_random_state"])
        min_eval_loss_ce_avg = checkpoint["min_eval_loss_ce_avg"]
        min_eval_loss_ce_avg_std = checkpoint["min_eval_loss_ce_avg_std"]
        min_eval_loss_err_avg = checkpoint["min_eval_loss_err_avg"]
        min_eval_loss_err_avg_std = checkpoint["min_eval_loss_err_avg_std"]
        min_eval_loss_l1_avg = checkpoint["min_eval_loss_l1_avg"]
        min_eval_loss_l1_fb = checkpoint["min_eval_loss_l1_fb"]
        err_flag = checkpoint["err_flag"]
        converge_checkpoint_flag = checkpoint["converge_checkpoint_flag"]
        iter_idx = checkpoint["iter_idx"]
        min_idx = checkpoint["min_idx"]
    # /resume

    while idx_stage < args.n_stage-1 and iter_idx + 1 >= t_starts[idx_stage+1]:
        idx_stage += 1
        logging.info(idx_stage)
    if (not sparse_min_flag) and (iter_idx + 1 >= t_ends[idx_stage]):
        sparse_check_flag = True
        sparse_min_flag = True
    factors = args.n_bands / 2
    logging.info(factors)
    eps = torch.finfo(indices_1hot.dtype).eps
    eps_1 = 1-eps
    logging.info(f"eps: {eps}\neps_1: {eps_1}")

    ##### Training Loop #####
    converge_checkpoint_step = args.t_end * args.step_fact
    logging.info(converge_checkpoint_step)
    logging.info("==%d EPOCH==" % (epoch_idx+1))
    logging.info("Training data")
    while True:
        # =============================================================================================================================================
        # =============================================================================================================================================
        # =============================================================================================================================================
        # Training of a batch

        start = time.time()
        batch_x_fb, batch_x, batch_x_c, batch_x_f, batch_feat, c_idx, utt_idx, featfile, x_bs, f_bs, x_ss, f_ss, n_batch_utt, \
            del_index_utt, max_slen, max_flen, idx_select, idx_select_full, slens_acc, flens_acc = next(generator)
        
        # All training batches processed once (c_idx == -1)
        if c_idx < 0:
            # Training surrary of this epoch and evaluation

            # Stash random state during evaluation
            numpy_random_state = np.random.get_state()
            torch_random_state = torch.get_rng_state()

            # Training summary of a epoch
            text_log = "(EPOCH:%d) average optimization loss = %.6f (+- %.6f) %.6f (+- %.6f) %% "\
                    "%.6f (+- %.6f) %.6f (+- %.6f) %% %.6f (+- %.6f) %.6f (+- %.6f) %% , %.6f (+- %.6f) %.6f (+- %.6f) , %.6f (+- %.6f) %.6f (+- %.6f)" % (epoch_idx + 1,
                    np.mean(loss_ce_avg), np.std(loss_ce_avg), np.mean(loss_err_avg), np.std(loss_err_avg),
                        np.mean(loss_ce_c_avg), np.std(loss_ce_c_avg), np.mean(loss_err_c_avg), np.std(loss_err_c_avg),
                            np.mean(loss_ce_f_avg), np.std(loss_ce_f_avg), np.mean(loss_err_f_avg), np.std(loss_err_f_avg),
                                np.mean(loss_fro_avg), np.std(loss_fro_avg), np.mean(loss_l1_avg), np.std(loss_l1_avg),
                                    np.mean(loss_fro_fb), np.std(loss_fro_fb), np.mean(loss_l1_fb), np.std(loss_l1_fb))
            for i in range(args.n_bands):
                text_log += " [%d] %.6f (+- %.6f) %.6f (+- %.6f) %% %.6f (+- %.6f) %.6f (+- %.6f) %% , %.6f (+- %.6f) %.6f (+- %.6f)" % (i+1,
                        np.mean(loss_ce[i]), np.std(loss_ce[i]), np.mean(loss_err[i]), np.std(loss_err[i]),
                            np.mean(loss_ce_f[i]), np.std(loss_ce_f[i]), np.mean(loss_err_f[i]), np.std(loss_err_f[i]),
                                np.mean(loss_fro[i]), np.std(loss_fro[i]), np.mean(loss_l1[i]), np.std(loss_l1[i]))
            logging.info("%s ;; (%.3f min., %.3f sec / batch)" % (text_log, total / 60.0, total / iter_count))
            logging.info("estimated time until max. step = {0.days:02}:{0.hours:02}:{0.minutes:02}:"\
            "{0.seconds:02}".format(relativedelta(seconds=int((args.step_count - (iter_idx + 1)) * total))))
            # /Training summary of a epoch

            # ========================================================================================================================
            # ========================================================================================================================
            # Evaluation of an epoch

            # Clear training loss, prepare to compute evaluation loss
            total = 0
            iter_count = 0
            loss_ce_avg = []
            loss_err_avg = []
            loss_ce_c_avg = []
            loss_err_c_avg = []
            loss_ce_f_avg = []
            loss_err_f_avg = []
            loss_fro_avg = []
            loss_l1_avg = []
            loss_fro_fb = []
            loss_l1_fb = []
            for i in range(args.n_bands):
                loss_ce[i] = []
                loss_err[i] = []
                loss_ce_f[i] = []
                loss_err_f[i] = []
                loss_fro[i] = []
                loss_l1[i] = []

            model_waveform.eval()
            for param in model_waveform.parameters():
                param.requires_grad = False
            logging.info("Evaluation data")
            batch_feat_for_gen = None
            
            while True:
                # ========================================================================================================================
                # Evaluation of an batch
                with torch.no_grad():
                    start = time.time()
                    # fullband, subbands,
                    batch_x_fb, batch_x, batch_x_c, batch_x_f, batch_feat, c_idx, utt_idx, featfile, x_bs, f_bs, x_ss, f_ss, n_batch_utt, \
                        del_index_utt, max_slen, max_flen, idx_select, idx_select_full, slens_acc, flens_acc = next(generator_eval)
                    # All evaluation batches processed once (c_idx == -1)
                    if c_idx < 0:
                        ### Waveform check
                        pqmf = PQMF(args.n_bands).cuda()
                        subband_waveforms = model_waveform.generate(batch_feat_for_gen)
                        waveforms = pqmf.synthesis(subband_waveforms)[:,0] # (B, 1, T) => (B, T)
                        wave0_emphed = waveforms[0].cpu().data.numpy() # (B, T) => (T,)
                        waveform_raw = librosa.effects.deemphasis(wave0_emphed, coef=0.85)
                        wave = np.clip(waveform_raw, -1, 0.999969482421875)
                        write_audio_to_tensorboard(writer, iter_idx, wave, args.fs)
                        ### /Waveform check
                        # Evaluation of this epoch is ended.                        
                        break
                    
                    # Preserve batch_feat for sample waveform generation.
                    batch_feat_for_gen = batch_feat


                    x_es = x_ss+x_bs
                    f_es = f_ss+f_bs
                    logging.info(f'{x_ss*args.n_bands} {x_bs*args.n_bands} {x_es*args.n_bands} {x_ss} {x_bs} {x_es} {f_ss} {f_bs} {f_es} {max_slen*args.n_bands} {max_slen} {max_flen}')
                    f_ss_pad_left = f_ss-pad_left
                    if f_es <= max_flen:
                        f_es_pad_right = f_es+pad_right
                    else:
                        f_es_pad_right = max_flen+pad_right
                    if x_ss > 0:
                        if x_es <= max_slen:
                            # maybe coarse/fine
                            # range: x_ss-1 <= T < x_es-1,    so, width==x_bs
                            batch_x_c_prev = batch_x_c[:,x_ss-1:x_es-1]
                            batch_x_f_prev = batch_x_f[:,x_ss-1:x_es-1]
                            if args.lpc > 0:
                                if x_ss-args.lpc >= 0:
                                    # range: (x_ss - args.lpc) <= T < (x_es - 1)
                                    batch_x_c_lpc = batch_x_c[:,x_ss-args.lpc:x_es-1]
                                    batch_x_f_lpc = batch_x_f[:,x_ss-args.lpc:x_es-1]
                                else:
                                    batch_x_c_lpc = F.pad(batch_x_c[:,:x_es-1], (0, 0, -(x_ss-args.lpc), 0), "constant", args.c_pad)
                                    batch_x_f_lpc = F.pad(batch_x_f[:,:x_es-1], (0, 0, -(x_ss-args.lpc), 0), "constant", args.f_pad)
                            # range: x_ss <= T < x_es
                            batch_x_c = batch_x_c[:,x_ss:x_es]
                            batch_x_f = batch_x_f[:,x_ss:x_es]
                            batch_x_fb = batch_x_fb[:,x_ss*args.n_bands:x_es*args.n_bands]
                        # Beyond array end (same processing with proper end)
                        else:
                            batch_x_c_prev = batch_x_c[:,x_ss-1:-1]
                            batch_x_f_prev = batch_x_f[:,x_ss-1:-1]
                            if args.lpc > 0:
                                if x_ss-args.lpc >= 0:
                                    batch_x_c_lpc = batch_x_c[:,x_ss-args.lpc:-1]
                                    batch_x_f_lpc = batch_x_f[:,x_ss-args.lpc:-1]
                                else:
                                    batch_x_c_lpc = F.pad(batch_x_c[:,:-1], (0, 0, -(x_ss-args.lpc), 0), "constant", args.c_pad)
                                    batch_x_f_lpc = F.pad(batch_x_f[:,:-1], (0, 0, -(x_ss-args.lpc), 0), "constant", args.f_pad)
                            batch_x_c = batch_x_c[:,x_ss:]
                            batch_x_f = batch_x_f[:,x_ss:]
                            batch_x_fb = batch_x_fb[:,x_ss*args.n_bands:]
                        # /Beyond array end
                    else:
                        batch_x_c_prev = F.pad(batch_x_c[:,:x_es-1], (0, 0, 1, 0), "constant", args.c_pad)
                        batch_x_f_prev = F.pad(batch_x_f[:,:x_es-1], (0, 0, 1, 0), "constant", args.f_pad)
                        if args.lpc > 0:
                            batch_x_c_lpc = F.pad(batch_x_c[:,:x_es-1], (0, 0, args.lpc, 0), "constant", args.c_pad)
                            batch_x_f_lpc = F.pad(batch_x_f[:,:x_es-1], (0, 0, args.lpc, 0), "constant", args.f_pad)
                        batch_x_c = batch_x_c[:,:x_es]
                        batch_x_f = batch_x_f[:,:x_es]
                        batch_x_fb = batch_x_fb[:,:x_es*args.n_bands]
                    if f_ss_pad_left >= 0 and f_es_pad_right <= max_flen: # pad left and right available
                        batch_feat = batch_feat[:,f_ss_pad_left:f_es_pad_right]
                    elif f_es_pad_right <= max_flen: # pad right available, left need additional replicate
                        batch_feat = F.pad(batch_feat[:,:f_es_pad_right].transpose(1,2), (-f_ss_pad_left,0), "replicate").transpose(1,2)
                    elif f_ss_pad_left >= 0: # pad left available, right need additional replicate
                        batch_feat = F.pad(batch_feat[:,f_ss_pad_left:max_flen].transpose(1,2), (0,f_es_pad_right-max_flen), "replicate").transpose(1,2)
                    else: # pad left and right need additional replicate
                        batch_feat = F.pad(batch_feat[:,:max_flen].transpose(1,2), (-f_ss_pad_left,f_es_pad_right-max_flen), "replicate").transpose(1,2)

                    # RNN #####
                    if f_ss > 0:
                        if len(del_index_utt) > 0:
                            h_x = torch.FloatTensor(np.delete(h_x.cpu().data.numpy(), del_index_utt, axis=1)).to(device)
                            h_x_2 = torch.FloatTensor(np.delete(h_x_2.cpu().data.numpy(), del_index_utt, axis=1)).to(device)
                            h_f = torch.FloatTensor(np.delete(h_f.cpu().data.numpy(), del_index_utt, axis=1)).to(device)
                        if args.lpc > 0:
                            batch_x_c_output, batch_x_f_output, h_x, h_x_2, h_f \
                                = model_waveform(batch_feat, batch_x_c_prev, batch_x_f_prev, batch_x_c, h=h_x, h_2=h_x_2, h_f=h_f,
                                        x_c_lpc=batch_x_c_lpc, x_f_lpc=batch_x_f_lpc)
                        else:
                            batch_x_c_output, batch_x_f_output, h_x, h_x_2, h_f \
                                = model_waveform(batch_feat, batch_x_c_prev, batch_x_f_prev, batch_x_c, h=h_x, h_2=h_x_2, h_f=h_f)
                    else:
                        if args.lpc > 0:
                            batch_x_c_output, batch_x_f_output, h_x, h_x_2, h_f \
                                = model_waveform(batch_feat, batch_x_c_prev, batch_x_f_prev, batch_x_c, x_c_lpc=batch_x_c_lpc, x_f_lpc=batch_x_f_lpc)
                        else:
                            batch_x_c_output, batch_x_f_output, h_x, h_x_2, h_f \
                                = model_waveform(batch_feat, batch_x_c_prev, batch_x_f_prev, batch_x_c)
                    # /RNN #####

                    u = torch.empty_like(batch_x_c_output)
                    logits_gumbel = F.softmax(batch_x_c_output - torch.log(-torch.log(torch.clamp(u.uniform_(), eps, eps_1))), dim=-1)
                    logits_gumbel_norm_1hot = F.threshold(logits_gumbel / torch.max(logits_gumbel,-1,keepdim=True)[0], eps_1, 0)
                    sample_indices_c = torch.sum(logits_gumbel_norm_1hot*indices_1hot,-1)
                    logits_gumbel = F.softmax(batch_x_f_output - torch.log(-torch.log(torch.clamp(u.uniform_(), eps, eps_1))), dim=-1)
                    logits_gumbel_norm_1hot = F.threshold(logits_gumbel / torch.max(logits_gumbel,-1,keepdim=True)[0], eps_1, 0)
                    sample_indices_f = torch.sum(logits_gumbel_norm_1hot*indices_1hot,-1)
                    batch_x_output = decode_mu_law_torch(sample_indices_c*args.cf_dim+sample_indices_f).transpose(1,2)
                    batch_x = decode_mu_law_torch(batch_x_c*args.cf_dim+batch_x_f).transpose(1,2)
                    # batch_x_output_*f*ull*b*and: subbands => fullband
                    batch_x_output_fb = pqmf.synthesis(batch_x_output)[:,0]

                    # samples check
                    #i = np.random.randint(0, batch_x_c_output.shape[0])
                    #logging.info("%s" % (os.path.join(os.path.basename(os.path.dirname(featfile[i])),os.path.basename(featfile[i]))))
                    #logging.info("%lf %lf" % (torch.min(batch_x_c_output), torch.max(batch_x_c_output)))
                    #logging.info("%lf %lf" % (torch.min(batch_x_f_output), torch.max(batch_x_f_output)))
                    #i = np.random.randint(0, batch_sc_output.shape[0])
                    #i_spk = spk_list.index(os.path.basename(os.path.dirname(featfile[i])).split("-")[0])
                    #logging.info("%s %d" % (os.path.join(os.path.basename(os.path.dirname(featfile[i])),os.path.basename(featfile[i])), i_spk+1))
                    #spk_post = F.softmax(batch_sc_output[i,:,:], dim=-1)[:,:,i_spk]
                    #logging.info(spk_post.mean())
                    #logging.info(spk_post.var())
                    #check_samples = batch_x_c[i,5:10].long()
                    #logging.info(check_samples)

                    # handle short ending
                    if len(idx_select) > 0:
                        logging.info('len_idx_select: '+str(len(idx_select)))
                        batch_loss_ce_select = 0
                        batch_loss_err_select = 0
                        batch_loss_ce_f_select = 0
                        batch_loss_err_f_select = 0
                        batch_loss_fro_fb_select = 0
                        batch_loss_l1_fb_select = 0
                        batch_loss_fro_select = 0
                        batch_loss_l1_select = 0
                        for j in range(len(idx_select)):
                            k = idx_select[j]
                            slens_utt = slens_acc[k]
                            slens_utt_fb = slens_utt*args.n_bands
                            flens_utt = flens_acc[k]
                            logging.info('%s %d %d %d' % (featfile[k], slens_utt, slens_utt_fb, flens_utt))
                            batch_x_c_output_ = batch_x_c_output[k,:slens_utt]
                            batch_x_f_output_ = batch_x_f_output[k,:slens_utt]
                            batch_x_c_ = batch_x_c[k,:slens_utt]
                            batch_x_f_ = batch_x_f[k,:slens_utt]
                            # T x n_bands x 32 --> (T x n_bands) x 32 --> T x n_bands
                            batch_loss_ce_select += torch.mean(criterion_ce(batch_x_c_output_.reshape(-1, args.cf_dim), batch_x_c_.reshape(-1)).reshape(-1, args.n_bands), 0) # n_bands
                            batch_loss_ce_f_select += torch.mean(criterion_ce(batch_x_f_output_.reshape(-1, args.cf_dim), batch_x_f_.reshape(-1)).reshape(-1, args.n_bands), 0) # n_bands
                            batch_loss_err_select += torch.mean(torch.sum(100*criterion_l1(F.softmax(batch_x_c_output_, dim=-1), F.one_hot(batch_x_c_, num_classes=args.cf_dim).float()), -1), 0) # n_bands
                            batch_loss_err_f_select += torch.mean(torch.sum(100*criterion_l1(F.softmax(batch_x_f_output_, dim=-1), F.one_hot(batch_x_f_, num_classes=args.cf_dim).float()), -1), 0) # n_bands
                            batch_loss_fro_fb_select_, batch_loss_l1_fb_select_ = criterion_stft_fb(batch_x_output_fb[k,:slens_utt_fb], batch_x_fb[k,:slens_utt_fb])
                            batch_loss_fro_fb_select += batch_loss_fro_fb_select_
                            batch_loss_l1_fb_select += batch_loss_l1_fb_select_
                            batch_loss_fro_select_, batch_loss_l1_select_ = criterion_stft(batch_x_output[k,:,:slens_utt], batch_x[k,:,:slens_utt])
                            batch_loss_fro_select += batch_loss_fro_select_
                            batch_loss_l1_select += batch_loss_l1_select_
                        batch_loss_ce_select /= len(idx_select)
                        batch_loss_err_select /= len(idx_select)
                        batch_loss_ce_f_select /= len(idx_select)
                        batch_loss_err_f_select /= len(idx_select)
                        batch_loss_fro_fb_select = (batch_loss_fro_fb_select/len(idx_select)).item()
                        batch_loss_l1_fb_select = (batch_loss_l1_fb_select/len(idx_select)).item()
                        batch_loss_fro_select /= len(idx_select)
                        batch_loss_l1_select /= len(idx_select)
                        batch_loss_ce_c_select_avg = batch_loss_ce_select.mean().item()
                        batch_loss_err_c_select_avg = batch_loss_err_select.mean().item()
                        batch_loss_ce_f_select_avg = batch_loss_ce_f_select.mean().item()
                        batch_loss_err_f_select_avg = batch_loss_err_f_select.mean().item()
                        batch_loss_ce_select_avg = (batch_loss_ce_c_select_avg + batch_loss_ce_f_select_avg)/2
                        batch_loss_err_select_avg = (batch_loss_err_c_select_avg + batch_loss_err_f_select_avg)/2
                        batch_loss_fro_select_avg = batch_loss_fro_select.mean().item()
                        batch_loss_l1_select_avg = batch_loss_l1_select.mean().item()
                        total_eval_loss["eval/loss_ce"].append(batch_loss_ce_select_avg)
                        total_eval_loss["eval/loss_err"].append(batch_loss_err_select_avg)
                        total_eval_loss["eval/loss_ce_c"].append(batch_loss_ce_c_select_avg)
                        total_eval_loss["eval/loss_err_c"].append(batch_loss_err_c_select_avg)
                        total_eval_loss["eval/loss_ce_f"].append(batch_loss_ce_f_select_avg)
                        total_eval_loss["eval/loss_err_f"].append(batch_loss_err_f_select_avg)
                        total_eval_loss["eval/loss_fro"].append(batch_loss_fro_select_avg)
                        total_eval_loss["eval/loss_l1"].append(batch_loss_l1_select_avg)
                        total_eval_loss["eval/loss_fro_fb"].append(batch_loss_fro_fb_select)
                        total_eval_loss["eval/loss_l1_fb"].append(batch_loss_l1_fb_select)
                        loss_ce_avg.append(batch_loss_ce_select_avg)
                        loss_err_avg.append(batch_loss_err_select_avg)
                        loss_ce_c_avg.append(batch_loss_ce_c_select_avg)
                        loss_err_c_avg.append(batch_loss_err_c_select_avg)
                        loss_ce_f_avg.append(batch_loss_ce_f_select_avg)
                        loss_err_f_avg.append(batch_loss_err_f_select_avg)
                        loss_fro_avg.append(batch_loss_fro_select_avg)
                        loss_l1_avg.append(batch_loss_l1_select_avg)
                        loss_fro_fb.append(batch_loss_fro_fb_select)
                        loss_l1_fb.append(batch_loss_l1_fb_select)
                        for i in range(args.n_bands):
                            total_eval_loss["eval/loss_ce_c-%d"%(i+1)].append(batch_loss_ce_select[i].item())
                            total_eval_loss["eval/loss_err_c-%d"%(i+1)].append(batch_loss_err_select[i].item())
                            total_eval_loss["eval/loss_ce_f-%d"%(i+1)].append(batch_loss_ce_f_select[i].item())
                            total_eval_loss["eval/loss_err_f-%d"%(i+1)].append(batch_loss_err_f_select[i].item())
                            total_eval_loss["eval/loss_fro-%d"%(i+1)].append(batch_loss_fro_select[i].item())
                            total_eval_loss["eval/loss_l1-%d"%(i+1)].append(batch_loss_l1_select[i].item())
                            loss_ce[i].append(batch_loss_ce_select[i].item())
                            loss_err[i].append(batch_loss_err_select[i].item())
                            loss_ce_f[i].append(batch_loss_ce_f_select[i].item())
                            loss_err_f[i].append(batch_loss_err_f_select[i].item())
                            loss_fro[i].append(batch_loss_fro_select[i].item())
                            loss_l1[i].append(batch_loss_l1_select[i].item())
                        if len(idx_select_full) > 0:
                            logging.info('len_idx_select_full: '+str(len(idx_select_full)))
                            batch_x_c = torch.index_select(batch_x_c,0,idx_select_full)
                            batch_x_f = torch.index_select(batch_x_f,0,idx_select_full)
                            batch_x = torch.index_select(batch_x,0,idx_select_full)
                            batch_x_fb = torch.index_select(batch_x_fb,0,idx_select_full)
                            batch_x_c_output = torch.index_select(batch_x_c_output,0,idx_select_full)
                            batch_x_f_output = torch.index_select(batch_x_f_output,0,idx_select_full)
                            batch_x_output = torch.index_select(batch_x_output,0,idx_select_full)
                            batch_x_output_fb = torch.index_select(batch_x_output_fb,0,idx_select_full)
                            n_batch_utt = len(idx_select_full)
                        else:
                            logging.info("batch loss select (%.3f sec)" % (time.time() - start))
                            iter_count += 1
                            total += time.time() - start
                            continue

                    # loss
                    batch_loss_ce_ = torch.mean(torch.mean(criterion_ce(batch_x_c_output.reshape(-1, args.cf_dim), batch_x_c.reshape(-1)).reshape(n_batch_utt, -1, args.n_bands), 1), 0) # n_bands
                    batch_loss_err_ = torch.mean(torch.mean(torch.sum(100*criterion_l1(F.softmax(batch_x_c_output, dim=-1), F.one_hot(batch_x_c, num_classes=args.cf_dim).float()), -1), 1), 0) # n_bands
                    batch_loss_ce_f_ = torch.mean(torch.mean(criterion_ce(batch_x_f_output.reshape(-1, args.cf_dim), batch_x_f.reshape(-1)).reshape(n_batch_utt, -1, args.n_bands), 1), 0) # n_bands
                    batch_loss_err_f_ = torch.mean(torch.mean(torch.sum(100*criterion_l1(F.softmax(batch_x_f_output, dim=-1), F.one_hot(batch_x_f, num_classes=args.cf_dim).float()), -1), 1), 0) # n_bands
                    batch_loss_ce_c_avg = batch_loss_ce_.mean().item()
                    batch_loss_err_c_avg = batch_loss_err_.mean().item()
                    batch_loss_ce_f_avg = batch_loss_ce_f_.mean().item()
                    batch_loss_err_f_avg = batch_loss_err_f_.mean().item()
                    batch_loss_ce_avg = (batch_loss_ce_c_avg + batch_loss_ce_f_avg) / 2
                    batch_loss_err_avg = (batch_loss_err_c_avg + batch_loss_err_f_avg) / 2
                    total_eval_loss["eval/loss_ce"].append(batch_loss_ce_avg)
                    total_eval_loss["eval/loss_err"].append(batch_loss_err_avg)
                    total_eval_loss["eval/loss_ce_c"].append(batch_loss_ce_c_avg)
                    total_eval_loss["eval/loss_err_c"].append(batch_loss_err_c_avg)
                    total_eval_loss["eval/loss_ce_f"].append(batch_loss_ce_f_avg)
                    total_eval_loss["eval/loss_err_f"].append(batch_loss_err_f_avg)
                    loss_ce_avg.append(batch_loss_ce_avg)
                    loss_err_avg.append(batch_loss_err_avg)
                    loss_ce_c_avg.append(batch_loss_ce_c_avg)
                    loss_err_c_avg.append(batch_loss_err_c_avg)
                    loss_ce_f_avg.append(batch_loss_ce_f_avg)
                    loss_err_f_avg.append(batch_loss_err_f_avg)
                    batch_loss_fro_, batch_loss_l1_ = criterion_stft(batch_x_output, batch_x)
                    for i in range(args.n_bands):
                        batch_loss_ce[i] = batch_loss_ce_[i].item()
                        batch_loss_err[i] = batch_loss_err_[i].item()
                        batch_loss_ce_f[i] = batch_loss_ce_f_[i].item()
                        batch_loss_err_f[i] = batch_loss_err_f_[i].item()
                        batch_loss_fro[i] = batch_loss_fro_[:,i].mean().item()
                        batch_loss_l1[i] = batch_loss_l1_[:,i].mean().item()
                        total_eval_loss["eval/loss_ce_c-%d"%(i+1)].append(batch_loss_ce[i])
                        total_eval_loss["eval/loss_err_c-%d"%(i+1)].append(batch_loss_err[i])
                        total_eval_loss["eval/loss_ce_f-%d"%(i+1)].append(batch_loss_ce_f[i])
                        total_eval_loss["eval/loss_err_f-%d"%(i+1)].append(batch_loss_err_f[i])
                        total_eval_loss["eval/loss_fro-%d"%(i+1)].append(batch_loss_fro[i])
                        total_eval_loss["eval/loss_l1-%d"%(i+1)].append(batch_loss_l1[i])
                        loss_ce[i].append(batch_loss_ce[i])
                        loss_err[i].append(batch_loss_err[i])
                        loss_ce_f[i].append(batch_loss_ce_f[i])
                        loss_err_f[i].append(batch_loss_err_f[i])
                        loss_fro[i].append(batch_loss_fro[i])
                        loss_l1[i].append(batch_loss_l1[i])
                    batch_loss_fro_avg = batch_loss_fro_.mean().item()
                    batch_loss_l1_avg = batch_loss_l1_.mean().item()
                    batch_loss_fro_fb_, batch_loss_l1_fb_ = criterion_stft_fb(batch_x_output_fb, batch_x_fb)
                    batch_loss_fro_fb = batch_loss_fro_fb_.mean().item()
                    batch_loss_l1_fb = batch_loss_l1_fb_.mean().item()
                    total_eval_loss["eval/loss_fro"].append(batch_loss_fro_avg)
                    total_eval_loss["eval/loss_l1"].append(batch_loss_l1_avg)
                    total_eval_loss["eval/loss_fro_fb"].append(batch_loss_fro_fb)
                    total_eval_loss["eval/loss_l1_fb"].append(batch_loss_l1_fb)
                    loss_fro_avg.append(batch_loss_fro_avg)
                    loss_l1_avg.append(batch_loss_l1_avg)
                    loss_fro_fb.append(batch_loss_fro_fb)
                    loss_l1_fb.append(batch_loss_l1_fb)

                    text_log = "batch eval loss [%d] %d %d %d %d %d : %.3f %.3f %% %.3f %.3f %% %.3f %.3f %% , %.3f %.3f , %.3f %.3f" % (c_idx+1, max_slen, x_ss, x_bs,
                        f_ss, f_bs, batch_loss_ce_avg, batch_loss_err_avg, batch_loss_ce_c_avg, batch_loss_err_c_avg,
                            batch_loss_ce_f_avg, batch_loss_err_f_avg, batch_loss_fro_avg, batch_loss_l1_avg, batch_loss_fro_fb, batch_loss_l1_fb)
                    for i in range(args.n_bands):
                        text_log += " [%d] %.3f %.3f %% %.3f %.3f %% , %.3f %.3f" % (i+1,
                            batch_loss_ce[i], batch_loss_err[i], batch_loss_ce_f[i], batch_loss_err_f[i], batch_loss_fro[i], batch_loss_l1[i])

                    logging.info("%s (%.3f sec)" % (text_log, time.time() - start))
                    iter_count += 1
                    total += time.time() - start
                # /Evaluation of an batch
                # ========================================================================================================================
            logging.info('sme %d' % (epoch_idx + 1))
            for key in total_eval_loss.keys():
                total_eval_loss[key] = np.mean(total_eval_loss[key])
                logging.info(f"(Steps: {iter_idx}) {key} = {total_eval_loss[key]:.4f}.")
            write_to_tensorboard(writer, iter_idx, total_eval_loss)
            total_eval_loss = defaultdict(list)
            eval_loss_ce_avg = np.mean(loss_ce_avg)
            eval_loss_ce_avg_std = np.std(loss_ce_avg)
            eval_loss_err_avg = np.mean(loss_err_avg)
            eval_loss_err_avg_std = np.std(loss_err_avg)
            eval_loss_ce_c_avg = np.mean(loss_ce_c_avg)
            eval_loss_ce_c_avg_std = np.std(loss_ce_c_avg)
            eval_loss_err_c_avg = np.mean(loss_err_c_avg)
            eval_loss_err_c_avg_std = np.std(loss_err_c_avg)
            eval_loss_ce_f_avg = np.mean(loss_ce_f_avg)
            eval_loss_ce_f_avg_std = np.std(loss_ce_f_avg)
            eval_loss_err_f_avg = np.mean(loss_err_f_avg)
            eval_loss_err_f_avg_std = np.std(loss_err_f_avg)
            eval_loss_fro_avg = np.mean(loss_fro_avg)
            eval_loss_fro_avg_std = np.std(loss_fro_avg)
            eval_loss_l1_avg = np.mean(loss_l1_avg)
            eval_loss_l1_avg_std = np.std(loss_l1_avg)
            eval_loss_fro_fb = np.mean(loss_fro_fb)
            eval_loss_fro_fb_std = np.std(loss_fro_fb)
            eval_loss_l1_fb = np.mean(loss_l1_fb)
            eval_loss_l1_fb_std = np.std(loss_l1_fb)
            for i in range(args.n_bands):
                eval_loss_ce[i] = np.mean(loss_ce[i])
                eval_loss_ce_std[i] = np.std(loss_ce[i])
                eval_loss_err[i] = np.mean(loss_err[i])
                eval_loss_err_std[i] = np.std(loss_err[i])
                eval_loss_ce_f[i] = np.mean(loss_ce_f[i])
                eval_loss_ce_f_std[i] = np.std(loss_ce_f[i])
                eval_loss_err_f[i] = np.mean(loss_err_f[i])
                eval_loss_err_f_std[i] = np.std(loss_err_f[i])
                eval_loss_fro[i] = np.mean(loss_fro[i])
                eval_loss_fro_std[i] = np.std(loss_fro[i])
                eval_loss_l1[i] = np.mean(loss_l1[i])
                eval_loss_l1_std[i] = np.std(loss_l1[i])

            # Logging
            text_log = "(EPOCH:%d) average evaluation loss = %.6f (+- %.6f) %.6f (+- %.6f) %% "\
                    "%.6f (+- %.6f) %.6f (+- %.6f) %% %.6f (+- %.6f) %.6f (+- %.6f) %% , %.6f (+- %.6f) %.6f (+- %.6f) , %.6f (+- %.6f) %.6f (+- %.6f) " % (epoch_idx + 1,
                    eval_loss_ce_avg, eval_loss_ce_avg_std, eval_loss_err_avg, eval_loss_err_avg_std,
                        eval_loss_ce_c_avg, eval_loss_ce_c_avg_std, eval_loss_err_c_avg, eval_loss_err_c_avg_std,
                            eval_loss_ce_f_avg, eval_loss_ce_f_avg_std, eval_loss_err_f_avg, eval_loss_err_f_avg_std,
                                eval_loss_fro_avg, eval_loss_fro_avg_std, eval_loss_l1_avg, eval_loss_l1_avg_std,
                                    eval_loss_fro_fb, eval_loss_fro_fb_std, eval_loss_l1_fb, eval_loss_l1_fb_std)
            for i in range(args.n_bands):
                text_log += " [%d] %.6f (+- %.6f) %.6f (+- %.6f) %% %.6f (+- %.6f) %.6f (+- %.6f) %% , %.6f (+- %.6f) %.6f (+- %.6f) " % (i+1,
                        eval_loss_ce[i], eval_loss_ce_std[i], eval_loss_err[i], eval_loss_err_std[i],
                            eval_loss_ce_f[i], eval_loss_ce_f_std[i], eval_loss_err_f[i], eval_loss_err_f_std[i],
                                eval_loss_fro[i], eval_loss_fro_std[i], eval_loss_l1[i], eval_loss_l1_std[i])
            logging.info("%s ;; (%.3f min., %.3f sec / batch)" % (text_log, total / 60.0, total / iter_count))
            # /Logging

            if (not sparse_min_flag) and (iter_idx + 1 >= t_ends[idx_stage]):
                sparse_check_flag = True
            if (not sparse_min_flag and sparse_check_flag) \
                or (not converge_checkpoint_flag and iter_idx + 1 >= converge_checkpoint_flag) \
                or (float(round(Decimal(str(eval_loss_err_avg-min_eval_loss_err_avg)),2)) <= 0.28 and \
                    float(round(Decimal(str(eval_loss_l1_avg-min_eval_loss_l1_avg)),2)) <= 0.06 and \
                    float(round(Decimal(str(eval_loss_l1_fb-min_eval_loss_l1_fb)),2)) <= 0.09 and \
                    (float(round(Decimal(str(eval_loss_ce_avg+eval_loss_ce_avg_std-(min_eval_loss_ce_avg+min_eval_loss_ce_avg_std))),2)) <= 0.01 or \
                        float(round(Decimal(str(eval_loss_ce_avg-min_eval_loss_ce_avg)),2)) <= 0.01)):
                round_eval_loss_min_eval_err_avg = float(round(Decimal(str(eval_loss_err_avg-min_eval_loss_err_avg)),2))
                if (round_eval_loss_min_eval_err_avg <= 0) or not err_flag or (not sparse_min_flag and sparse_check_flag) \
                    or (not converge_checkpoint_flag and iter_idx + 1 >= converge_checkpoint_step):
                    if sparse_min_flag:
                        if round_eval_loss_min_eval_err_avg > 0:
                            err_flag = True
                        elif round_eval_loss_min_eval_err_avg <= 0:
                            err_flag = False
                    elif sparse_check_flag:
                        sparse_min_flag = True
                        err_flag = False
                    if not converge_checkpoint_flag and iter_idx + 1 >= converge_checkpoint_step:
                        logging.info(f"converge_checkpoint_flag {iter_idx + 1} {converge_checkpoint_step}")
                        converge_checkpoint_flag = True
                        err_flag = False
                    min_eval_loss_ce_avg = eval_loss_ce_avg
                    min_eval_loss_ce_avg_std = eval_loss_ce_avg_std
                    min_eval_loss_err_avg = eval_loss_err_avg
                    min_eval_loss_err_avg_std = eval_loss_err_avg_std
                    min_eval_loss_ce_c_avg = eval_loss_ce_c_avg
                    min_eval_loss_ce_c_avg_std = eval_loss_ce_c_avg_std
                    min_eval_loss_err_c_avg = eval_loss_err_c_avg
                    min_eval_loss_err_c_avg_std = eval_loss_err_c_avg_std
                    min_eval_loss_ce_f_avg = eval_loss_ce_f_avg
                    min_eval_loss_ce_f_avg_std = eval_loss_ce_f_avg_std
                    min_eval_loss_err_f_avg = eval_loss_err_f_avg
                    min_eval_loss_err_f_avg_std = eval_loss_err_f_avg_std
                    min_eval_loss_fro_avg = eval_loss_fro_avg
                    min_eval_loss_fro_avg_std = eval_loss_fro_avg_std
                    min_eval_loss_l1_avg = eval_loss_l1_avg
                    min_eval_loss_l1_avg_std = eval_loss_l1_avg_std
                    min_eval_loss_fro_fb = eval_loss_fro_fb
                    min_eval_loss_fro_fb_std = eval_loss_fro_fb_std
                    min_eval_loss_l1_fb = eval_loss_l1_fb
                    min_eval_loss_l1_fb_std = eval_loss_l1_fb_std
                    for i in range(args.n_bands):
                        min_eval_loss_ce[i] = eval_loss_ce[i]
                        min_eval_loss_ce_std[i] = eval_loss_ce_std[i]
                        min_eval_loss_err[i] = eval_loss_err[i]
                        min_eval_loss_err_std[i] = eval_loss_err_std[i]
                        min_eval_loss_ce_f[i] = eval_loss_ce_f[i]
                        min_eval_loss_ce_f_std[i] = eval_loss_ce_f_std[i]
                        min_eval_loss_err_f[i] = eval_loss_err_f[i]
                        min_eval_loss_err_f_std[i] = eval_loss_err_f_std[i]
                        min_eval_loss_fro[i] = eval_loss_fro[i]
                        min_eval_loss_fro_std[i] = eval_loss_fro_std[i]
                        min_eval_loss_l1[i] = eval_loss_l1[i]
                        min_eval_loss_l1_std[i] = eval_loss_l1_std[i]
                    min_idx = epoch_idx
                    change_min_flag = True
            if change_min_flag:
                text_log = "min_eval_loss = %.6f (+- %.6f) %.6f (+- %.6f) %% "\
                        "%.6f (+- %.6f) %.6f (+- %.6f) %% %.6f (+- %.6f) %.6f (+- %.6f) %% , %.6f (+- %.6f) %.6f (+- %.6f) %.6f (+- %.6f) %.6f (+- %.6f) " % (
                        min_eval_loss_ce_avg, min_eval_loss_ce_avg_std, min_eval_loss_err_avg, min_eval_loss_err_avg_std,
                            min_eval_loss_ce_c_avg, min_eval_loss_ce_c_avg_std, min_eval_loss_err_c_avg, min_eval_loss_err_c_avg_std,
                                min_eval_loss_ce_f_avg, min_eval_loss_ce_f_avg_std, min_eval_loss_err_f_avg, min_eval_loss_err_f_avg_std,
                                    min_eval_loss_fro_avg, min_eval_loss_fro_avg_std, min_eval_loss_l1_avg, min_eval_loss_l1_avg_std,
                                        min_eval_loss_fro_fb, min_eval_loss_fro_fb_std, min_eval_loss_l1_fb, min_eval_loss_l1_fb_std)
                for i in range(args.n_bands):
                    text_log += " [%d] %.6f (+- %.6f) %.6f (+- %.6f) %% %.6f (+- %.6f) %.6f (+- %.6f) %% , %.6f (+- %.6f) %.6f (+- %.6f) " % (i+1,
                            min_eval_loss_ce[i], min_eval_loss_ce_std[i], min_eval_loss_err[i], min_eval_loss_err_std[i],
                                min_eval_loss_ce_f[i], min_eval_loss_ce_f_std[i], min_eval_loss_err_f[i], min_eval_loss_err_f_std[i],
                                    min_eval_loss_fro[i], min_eval_loss_fro_std[i], min_eval_loss_l1[i], min_eval_loss_l1_std[i])
                logging.info("%s min_idx=%d" % (text_log, min_idx+1))
            # /Evaluation of an epoch
            # ========================================================================================================================
            # ========================================================================================================================

            # Save checkpoint with N epoch interval (Best epoch is saved even if interval is not yet come)
            #if ((epoch_idx + 1) % args.save_interval_epoch == 0) or (epoch_min_flag):
            #    logging.info('save epoch:%d' % (epoch_idx+1))
            #    save_checkpoint(args.expdir, model_waveform, optimizer, numpy_random_state, torch_random_state, epoch_idx + 1)

            # Save checkpoint every epoch
            logging.info('save epoch:%d' % (epoch_idx+1))
            save_checkpoint(args.expdir, model_waveform, optimizer,
                min_eval_loss_ce_avg, min_eval_loss_ce_avg_std, min_eval_loss_err_avg, min_eval_loss_err_avg_std,
                    min_eval_loss_l1_avg, min_eval_loss_l1_fb, err_flag, converge_checkpoint_flag,
                    iter_idx, min_idx, numpy_random_state, torch_random_state, epoch_idx + 1)
            
            # ========================================================================================================================
            # ========================================================================================================================
            # Next epoch
            # Clear evaluation loss, prepare to start training
            total = 0
            iter_count = 0
            loss_ce_avg = []
            loss_err_avg = []
            loss_ce_c_avg = []
            loss_err_c_avg = []
            loss_ce_f_avg = []
            loss_err_f_avg = []
            loss_fro_avg = []
            loss_l1_avg = []
            loss_fro_fb = []
            loss_l1_fb = []
            for i in range(args.n_bands):
                loss_ce[i] = []
                loss_err[i] = []
                loss_ce_f[i] = []
                loss_err_f[i] = []
                loss_fro[i] = []
                loss_l1[i] = []

            epoch_idx += 1

            # Restore stashed state
            np.random.set_state(numpy_random_state)
            torch.set_rng_state(torch_random_state)
            
            model_waveform.train()
            for param in model_waveform.parameters():
                param.requires_grad = True
            for param in model_waveform.scale_in.parameters():
                param.requires_grad = False
            if args.lpc > 0:
                for param in model_waveform.logits.parameters():
                    param.requires_grad = False

            if iter_idx < args.step_count:
                start = time.time()
                logging.info("==%d EPOCH==" % (epoch_idx+1))
                logging.info("Training data")
                # Stepping the generator is equal to starting next epoch.
                batch_x_fb, batch_x, batch_x_c, batch_x_f, batch_feat, c_idx, utt_idx, featfile, x_bs, f_bs, x_ss, f_ss, n_batch_utt, \
                    del_index_utt, max_slen, max_flen, idx_select, idx_select_full, slens_acc, flens_acc = next(generator)
            else:
                # Training Finished.
                break
            # /Next epoch
            # ========================================================================================================================
            # ========================================================================================================================

        # Forward
        logging.info("%d iteration [%d]" % (iter_idx+1, epoch_idx+1))

        x_es = x_ss+x_bs
        f_es = f_ss+f_bs
        logging.info(f'{x_ss*args.n_bands} {x_bs*args.n_bands} {x_es*args.n_bands} {x_ss} {x_bs} {x_es} {f_ss} {f_bs} {f_es} {max_slen*args.n_bands} {max_slen} {max_flen}')
        f_ss_pad_left = f_ss-pad_left
        if f_es <= max_flen:
            f_es_pad_right = f_es+pad_right
        else:
            f_es_pad_right = max_flen+pad_right
        if x_ss > 0:
            if x_es <= max_slen:
                batch_x_c_prev = batch_x_c[:,x_ss-1:x_es-1]
                batch_x_f_prev = batch_x_f[:,x_ss-1:x_es-1]
                if args.lpc > 0:
                    if x_ss-args.lpc >= 0:
                        batch_x_c_lpc = batch_x_c[:,x_ss-args.lpc:x_es-1]
                        batch_x_f_lpc = batch_x_f[:,x_ss-args.lpc:x_es-1]
                    else:
                        batch_x_c_lpc = F.pad(batch_x_c[:,:x_es-1], (0, 0, -(x_ss-args.lpc), 0), "constant", args.c_pad)
                        batch_x_f_lpc = F.pad(batch_x_f[:,:x_es-1], (0, 0, -(x_ss-args.lpc), 0), "constant", args.f_pad)
                batch_x_c = batch_x_c[:,x_ss:x_es]
                batch_x_f = batch_x_f[:,x_ss:x_es]
                batch_x = batch_x[:,x_ss:x_es].transpose(1,2)
                batch_x_fb = batch_x_fb[:,x_ss*args.n_bands:x_es*args.n_bands]
            else:
                batch_x_c_prev = batch_x_c[:,x_ss-1:-1]
                batch_x_f_prev = batch_x_f[:,x_ss-1:-1]
                if args.lpc > 0:
                    if x_ss-args.lpc >= 0:
                        batch_x_c_lpc = batch_x_c[:,x_ss-args.lpc:-1]
                        batch_x_f_lpc = batch_x_f[:,x_ss-args.lpc:-1]
                    else:
                        batch_x_c_lpc = F.pad(batch_x_c[:,:-1], (0, 0, -(x_ss-args.lpc), 0), "constant", args.c_pad)
                        batch_x_f_lpc = F.pad(batch_x_f[:,:-1], (0, 0, -(x_ss-args.lpc), 0), "constant", args.f_pad)
                batch_x_c = batch_x_c[:,x_ss:]
                batch_x_f = batch_x_f[:,x_ss:]
                batch_x = batch_x[:,x_ss:].transpose(1,2)
                batch_x_fb = batch_x_fb[:,x_ss*args.n_bands:]
        else:
            batch_x_c_prev = F.pad(batch_x_c[:,:x_es-1], (0, 0, 1, 0), "constant", args.c_pad)
            batch_x_f_prev = F.pad(batch_x_f[:,:x_es-1], (0, 0, 1, 0), "constant", args.f_pad)
            if args.lpc > 0:
                batch_x_c_lpc = F.pad(batch_x_c[:,:x_es-1], (0, 0, args.lpc, 0), "constant", args.c_pad)
                batch_x_f_lpc = F.pad(batch_x_f[:,:x_es-1], (0, 0, args.lpc, 0), "constant", args.f_pad)
            batch_x_c = batch_x_c[:,:x_es]
            batch_x_f = batch_x_f[:,:x_es]
            batch_x = batch_x[:,:x_es].transpose(1,2)
            batch_x_fb = batch_x_fb[:,:x_es*args.n_bands]
        if f_ss_pad_left >= 0 and f_es_pad_right <= max_flen: # pad left and right available
            batch_feat = batch_feat[:,f_ss_pad_left:f_es_pad_right]
        elif f_es_pad_right <= max_flen: # pad right available, left need additional replicate
            batch_feat = F.pad(batch_feat[:,:f_es_pad_right].transpose(1,2), (-f_ss_pad_left,0), "replicate").transpose(1,2)
        elif f_ss_pad_left >= 0: # pad left available, right need additional replicate
            batch_feat = F.pad(batch_feat[:,f_ss_pad_left:max_flen].transpose(1,2), (0,f_es_pad_right-max_flen), "replicate").transpose(1,2)
        else: # pad left and right need additional replicate
            batch_feat = F.pad(batch_feat[:,:max_flen].transpose(1,2), (-f_ss_pad_left,f_es_pad_right-max_flen), "replicate").transpose(1,2)

        if f_ss > 0:
            if len(del_index_utt) > 0:
                h_x = torch.FloatTensor(np.delete(h_x.cpu().data.numpy(), del_index_utt, axis=1)).to(device)
                h_x_2 = torch.FloatTensor(np.delete(h_x_2.cpu().data.numpy(), del_index_utt, axis=1)).to(device)
                h_f = torch.FloatTensor(np.delete(h_f.cpu().data.numpy(), del_index_utt, axis=1)).to(device)
            if args.lpc > 0:
                batch_x_c_output, batch_x_f_output, h_x, h_x_2, h_f \
                    = model_waveform(batch_feat, batch_x_c_prev, batch_x_f_prev, batch_x_c, h=h_x, h_2=h_x_2, h_f=h_f,
                            x_c_lpc=batch_x_c_lpc, x_f_lpc=batch_x_f_lpc, do=True)
            else:
                batch_x_c_output, batch_x_f_output, h_x, h_x_2, h_f \
                    = model_waveform(batch_feat, batch_x_c_prev, batch_x_f_prev, batch_x_c, h=h_x, h_2=h_x_2, h_f=h_f, do=True)
        else:
            if args.lpc > 0:
                batch_x_c_output, batch_x_f_output, h_x, h_x_2, h_f \
                    = model_waveform(batch_feat, batch_x_c_prev, batch_x_f_prev, batch_x_c, x_c_lpc=batch_x_c_lpc, x_f_lpc=batch_x_f_lpc, do=True)
            else:
                batch_x_c_output, batch_x_f_output, h_x, h_x_2, h_f \
                    = model_waveform(batch_feat, batch_x_c_prev, batch_x_f_prev, batch_x_c, do=True)
        u = torch.empty_like(batch_x_c_output)
        logits_gumbel = F.softmax(batch_x_c_output - torch.log(-torch.log(torch.clamp(u.uniform_(), eps, eps_1))), dim=-1)
        logits_gumbel_norm_1hot = F.threshold(logits_gumbel / torch.max(logits_gumbel,-1,keepdim=True)[0], eps_1, 0)
        sample_indices_c = torch.sum(logits_gumbel_norm_1hot*indices_1hot,-1)
        logits_gumbel = F.softmax(batch_x_f_output - torch.log(-torch.log(torch.clamp(u.uniform_(), eps, eps_1))), dim=-1)
        logits_gumbel_norm_1hot = F.threshold(logits_gumbel / torch.max(logits_gumbel,-1,keepdim=True)[0], eps_1, 0)
        sample_indices_f = torch.sum(logits_gumbel_norm_1hot*indices_1hot,-1)
        batch_x_output = decode_mu_law_torch(sample_indices_c*args.cf_dim+sample_indices_f).transpose(1,2)
        batch_x_output_fb = pqmf.synthesis(batch_x_output)[:,0]
        # /Forward?

        # samples check
        #i = np.random.randint(0, batch_x_c_output.shape[0])
        #logging.info("%s" % (os.path.join(os.path.basename(os.path.dirname(featfile[i])),os.path.basename(featfile[i]))))
        #logging.info("%lf %lf" % (torch.min(batch_x_c_output), torch.max(batch_x_c_output)))
        #logging.info("%lf %lf" % (torch.min(batch_x_f_output), torch.max(batch_x_f_output)))
        #with torch.no_grad():
        #    i = np.random.randint(0, batch_sc_output.shape[0])
        #    i_spk = spk_list.index(os.path.basename(os.path.dirname(featfile[i])).split("-")[0])
        #    logging.info("%s %d" % (os.path.join(os.path.basename(os.path.dirname(featfile[i])),os.path.basename(featfile[i])), i_spk+1))
        #    check_samples = batch_x[i,5:10].long()
        #    logging.info(torch.index_select(F.softmax(batch_x_c_output[i,5:10], dim=-1), 1, check_samples))
        #    logging.info(check_samples)

        # handle short ending
        batch_loss = 0
        if len(idx_select) > 0:
            logging.info('len_idx_select: '+str(len(idx_select)))
            batch_loss_ce_select = 0
            batch_loss_err_select = 0
            batch_loss_ce_f_select = 0
            batch_loss_err_f_select = 0
            batch_loss_fro_fb_select = 0
            batch_loss_l1_fb_select = 0
            batch_loss_fro_select = 0
            batch_loss_l1_select = 0
            for j in range(len(idx_select)):
                k = idx_select[j]
                slens_utt = slens_acc[k]
                slens_utt_fb = slens_utt*args.n_bands
                flens_utt = flens_acc[k]
                batch_x_c_output_ = batch_x_c_output[k,:slens_utt]
                batch_x_f_output_ = batch_x_f_output[k,:slens_utt]
                batch_x_c_ = batch_x_c[k,:slens_utt]
                batch_x_f_ = batch_x_f[k,:slens_utt]
                # T x n_bands x 32 --> (T x n_bands) x 32 --> T x n_bands
                batch_loss_ce_select_ = torch.mean(criterion_ce(batch_x_c_output_.reshape(-1, args.cf_dim), batch_x_c_.reshape(-1)).reshape(-1, args.n_bands), 0) # n_bands
                batch_loss_ce_f_select_ = torch.mean(criterion_ce(batch_x_f_output_.reshape(-1, args.cf_dim), batch_x_f_.reshape(-1)).reshape(-1, args.n_bands), 0) # n_bands
                batch_loss_ce_select += batch_loss_ce_select_
                batch_loss_ce_f_select += batch_loss_ce_f_select_
                batch_loss_err_select_ = torch.mean(torch.sum(criterion_l1(F.softmax(batch_x_c_output_, dim=-1), F.one_hot(batch_x_c_, num_classes=args.cf_dim).float()), -1), 0) # n_bands
                batch_loss_err_f_select_ = torch.mean(torch.sum(criterion_l1(F.softmax(batch_x_f_output_, dim=-1), F.one_hot(batch_x_f_, num_classes=args.cf_dim).float()), -1), 0) # n_bands
                batch_loss_fro_fb_select_, batch_loss_l1_fb_select_ = criterion_stft_fb(batch_x_output_fb[k,:slens_utt_fb], batch_x_fb[k,:slens_utt_fb])
                batch_loss_fro_fb_select += batch_loss_fro_fb_select_
                batch_loss_l1_fb_select += batch_loss_l1_fb_select_
                batch_loss_fro_select_, batch_loss_l1_select_ = criterion_stft(batch_x_output[k,:,:slens_utt], batch_x[k,:,:slens_utt])
                batch_loss += batch_loss_ce_select_.sum() + batch_loss_ce_f_select_.sum() \
                                + batch_loss_ce_select_.mean() + batch_loss_ce_f_select_.mean() \
                                + ((batch_loss_err_select_.sum() + batch_loss_err_f_select_.sum())/factors) \
                                + batch_loss_err_select_.mean() + batch_loss_err_f_select_.mean() \
                                + batch_loss_fro_fb_select_ + batch_loss_l1_fb_select_ \
                                + batch_loss_fro_select_.sum() + batch_loss_l1_select_.sum()
                batch_loss_fro_select += batch_loss_fro_select_
                batch_loss_l1_select += batch_loss_l1_select_
                logging.info('%s %d %d %d' % (featfile[k], slens_utt, slens_utt_fb, flens_utt))
                batch_loss_err_select += 100*batch_loss_err_select_
                batch_loss_err_f_select += 100*batch_loss_err_f_select_
            batch_loss_ce_select /= len(idx_select)
            batch_loss_err_select /= len(idx_select)
            batch_loss_ce_f_select /= len(idx_select)
            batch_loss_err_f_select /= len(idx_select)
            batch_loss_fro_fb_select = (batch_loss_fro_fb_select/len(idx_select)).item()
            batch_loss_l1_fb_select = (batch_loss_l1_fb_select/len(idx_select)).item()
            batch_loss_fro_select /= len(idx_select)
            batch_loss_l1_select /= len(idx_select)
            batch_loss_ce_c_select_avg = batch_loss_ce_select.mean().item()
            batch_loss_err_c_select_avg = batch_loss_err_select.mean().item()
            batch_loss_ce_f_select_avg = batch_loss_ce_f_select.mean().item()
            batch_loss_err_f_select_avg = batch_loss_err_f_select.mean().item()
            batch_loss_ce_select_avg = (batch_loss_ce_c_select_avg + batch_loss_ce_f_select_avg)/2
            batch_loss_err_select_avg = (batch_loss_err_c_select_avg + batch_loss_err_f_select_avg)/2
            batch_loss_fro_select_avg = batch_loss_fro_select.mean().item()
            batch_loss_l1_select_avg = batch_loss_l1_select.mean().item()
            total_train_loss["train/loss_ce"].append(batch_loss_ce_select_avg)
            total_train_loss["train/loss_err"].append(batch_loss_err_select_avg)
            total_train_loss["train/loss_ce_c"].append(batch_loss_ce_c_select_avg)
            total_train_loss["train/loss_err_c"].append(batch_loss_err_c_select_avg)
            total_train_loss["train/loss_ce_f"].append(batch_loss_ce_f_select_avg)
            total_train_loss["train/loss_err_f"].append(batch_loss_err_f_select_avg)
            total_train_loss["train/loss_fro"].append(batch_loss_fro_select_avg)
            total_train_loss["train/loss_l1"].append(batch_loss_l1_select_avg)
            total_train_loss["train/loss_fro_fb"].append(batch_loss_fro_fb_select)
            total_train_loss["train/loss_l1_fb"].append(batch_loss_l1_fb_select)
            loss_ce_avg.append(batch_loss_ce_select_avg)
            loss_err_avg.append(batch_loss_err_select_avg)
            loss_ce_c_avg.append(batch_loss_ce_c_select_avg)
            loss_err_c_avg.append(batch_loss_err_c_select_avg)
            loss_ce_f_avg.append(batch_loss_ce_f_select_avg)
            loss_err_f_avg.append(batch_loss_err_f_select_avg)
            loss_fro_avg.append(batch_loss_fro_select_avg)
            loss_l1_avg.append(batch_loss_l1_select_avg)
            loss_fro_fb.append(batch_loss_fro_fb_select)
            loss_l1_fb.append(batch_loss_l1_fb_select)
            for i in range(args.n_bands):
                total_train_loss["train/loss_ce_c-%d"%(i+1)].append(batch_loss_ce_select[i].item())
                total_train_loss["train/loss_err_c-%d"%(i+1)].append(batch_loss_err_select[i].item())
                total_train_loss["train/loss_ce_f-%d"%(i+1)].append(batch_loss_ce_f_select[i].item())
                total_train_loss["train/loss_err_f-%d"%(i+1)].append(batch_loss_err_f_select[i].item())
                total_train_loss["train/loss_fro-%d"%(i+1)].append(batch_loss_fro_select[i].item())
                total_train_loss["train/loss_l1-%d"%(i+1)].append(batch_loss_l1_select[i].item())
                loss_ce[i].append(batch_loss_ce_select[i].item())
                loss_err[i].append(batch_loss_err_select[i].item())
                loss_ce_f[i].append(batch_loss_ce_f_select[i].item())
                loss_err_f[i].append(batch_loss_err_f_select[i].item())
                loss_fro[i].append(batch_loss_fro_select[i].item())
                loss_l1[i].append(batch_loss_l1_select[i].item())
            if len(idx_select_full) > 0:
                logging.info('len_idx_select_full: '+str(len(idx_select_full)))
                batch_x_c = torch.index_select(batch_x_c,0,idx_select_full)
                batch_x_f = torch.index_select(batch_x_f,0,idx_select_full)
                batch_x = torch.index_select(batch_x,0,idx_select_full)
                batch_x_fb = torch.index_select(batch_x_fb,0,idx_select_full)
                batch_x_c_output = torch.index_select(batch_x_c_output,0,idx_select_full)
                batch_x_f_output = torch.index_select(batch_x_f_output,0,idx_select_full)
                batch_x_output = torch.index_select(batch_x_output,0,idx_select_full)
                batch_x_output_fb = torch.index_select(batch_x_output_fb,0,idx_select_full)
                n_batch_utt = len(idx_select_full)
            elif batch_loss > 0:
                optimizer.zero_grad()
                batch_loss.backward()
                flag = False
                for name, param in model_waveform.named_parameters():
                    if param.requires_grad:
                        grad_norm = param.grad.norm()
                        if torch.isnan(grad_norm) or torch.isinf(grad_norm):
                            flag = True
                if flag:
                    logging.info("explode grad")
                    optimizer.zero_grad()
                    continue
                torch.nn.utils.clip_grad_norm_(model_waveform.parameters(), 10)
                optimizer.step()

                with torch.no_grad():
                    if idx_stage < args.n_stage-1 and iter_idx + 1 == t_starts[idx_stage+1]:
                        idx_stage += 1
                    if idx_stage > 0:
                        sparsify(model_waveform, iter_idx + 1, t_starts[idx_stage], t_ends[idx_stage], args.interval, densities[idx_stage], densities_p=densities[idx_stage-1])
                    else:
                        sparsify(model_waveform, iter_idx + 1, t_starts[idx_stage], t_ends[idx_stage], args.interval, densities[idx_stage])

                logging.info("batch loss select %.3f (%.3f sec)" % (batch_loss.item(), time.time() - start))
                iter_idx += 1
                #if iter_idx % args.save_interval_iter == 0:
                #    logging.info('save iter:%d' % (iter_idx))
                #    save_checkpoint(args.expdir, model_waveform, optimizer, np.random.get_state(), torch.get_rng_state(), iter_idx)
                iter_count += 1
                if iter_idx % args.log_interval_steps == 0:
                    logging.info('smt')
                    for key in total_train_loss.keys():
                        total_train_loss[key] = np.mean(total_train_loss[key])
                        logging.info(f"(Steps: {iter_idx}) {key} = {total_train_loss[key]:.4f}.")
                    write_to_tensorboard(writer, iter_idx, total_train_loss)
                    # Reset `total_train_loss`
                    total_train_loss = defaultdict(list)
                total += time.time() - start
                continue
            else:
                continue

        # loss
        batch_loss_ce_ = torch.mean(criterion_ce(batch_x_c_output.reshape(-1, args.cf_dim), batch_x_c.reshape(-1)).reshape(n_batch_utt, -1, args.n_bands), 1) # B x n_bands
        batch_loss_err_ = torch.mean(torch.sum(criterion_l1(F.softmax(batch_x_c_output, dim=-1), F.one_hot(batch_x_c, num_classes=args.cf_dim).float()), -1), 1) # B x n_bands
        batch_loss_ce_f_ = torch.mean(criterion_ce(batch_x_f_output.reshape(-1, args.cf_dim), batch_x_f.reshape(-1)).reshape(n_batch_utt, -1, args.n_bands), 1) # B x n_bands
        batch_loss_err_f_ = torch.mean(torch.sum(criterion_l1(F.softmax(batch_x_f_output, dim=-1), F.one_hot(batch_x_f, num_classes=args.cf_dim).float()), -1), 1) # B x n_bands
        #logging.info(f'{batch_loss_err_.mean()}')
        #logging.info(f'{batch_loss_err_f_.mean()}')
        #logging.info(f'{batch_loss_err__.mean()}')
        #logging.info(f'{batch_loss_err_f__.mean()}')
        batch_loss += ((batch_loss_err_.sum() + batch_loss_err_f_.sum())/factors) \
                        + batch_loss_err_.mean(-1).sum() + batch_loss_err_f_.mean(-1).sum()
        batch_loss_err_ = 100*batch_loss_err_.mean(0) # n_bands
        batch_loss_err_f_ = 100*batch_loss_err_f_.mean(0) # n_bands

        batch_loss_ce_c_avg = batch_loss_ce_.mean().item()
        batch_loss_err_c_avg = batch_loss_err_.mean().item()
        batch_loss_ce_f_avg = batch_loss_ce_f_.mean().item()
        batch_loss_err_f_avg = batch_loss_err_f_.mean().item()
        batch_loss_ce_avg = (batch_loss_ce_c_avg + batch_loss_ce_f_avg) / 2
        batch_loss_err_avg = (batch_loss_err_c_avg + batch_loss_err_f_avg) / 2
        total_train_loss["train/loss_ce"].append(batch_loss_ce_avg)
        total_train_loss["train/loss_err"].append(batch_loss_err_avg)
        total_train_loss["train/loss_ce_c"].append(batch_loss_ce_c_avg)
        total_train_loss["train/loss_err_c"].append(batch_loss_err_c_avg)
        total_train_loss["train/loss_ce_f"].append(batch_loss_ce_f_avg)
        total_train_loss["train/loss_err_f"].append(batch_loss_err_f_avg)
        loss_ce_avg.append(batch_loss_ce_avg)
        loss_err_avg.append(batch_loss_err_avg)
        loss_ce_c_avg.append(batch_loss_ce_c_avg)
        loss_err_c_avg.append(batch_loss_err_c_avg)
        loss_ce_f_avg.append(batch_loss_ce_f_avg)
        loss_err_f_avg.append(batch_loss_err_f_avg)
        batch_loss_fro_, batch_loss_l1_ = criterion_stft(batch_x_output, batch_x)
        for i in range(args.n_bands):
            batch_loss_ce[i] = batch_loss_ce_[:,i].mean().item()
            batch_loss_err[i] = batch_loss_err_[i].item()
            batch_loss_ce_f[i] = batch_loss_ce_f_[:,i].mean().item()
            batch_loss_err_f[i] = batch_loss_err_f_[i].item()
            batch_loss_fro[i] = batch_loss_fro_[:,i].mean().item()
            batch_loss_l1[i] = batch_loss_l1_[:,i].mean().item()
            total_train_loss["train/loss_ce_c-%d"%(i+1)].append(batch_loss_ce[i])
            total_train_loss["train/loss_err_c-%d"%(i+1)].append(batch_loss_err[i])
            total_train_loss["train/loss_ce_f-%d"%(i+1)].append(batch_loss_ce_f[i])
            total_train_loss["train/loss_err_f-%d"%(i+1)].append(batch_loss_err_f[i])
            total_train_loss["train/loss_fro-%d"%(i+1)].append(batch_loss_fro[i])
            total_train_loss["train/loss_l1-%d"%(i+1)].append(batch_loss_l1[i])
            loss_ce[i].append(batch_loss_ce[i])
            loss_err[i].append(batch_loss_err[i])
            loss_ce_f[i].append(batch_loss_ce_f[i])
            loss_err_f[i].append(batch_loss_err_f[i])
            loss_fro[i].append(batch_loss_fro[i])
            loss_l1[i].append(batch_loss_l1[i])
        batch_loss_fro_avg = batch_loss_fro_.mean().item()
        batch_loss_l1_avg = batch_loss_l1_.mean().item()
        batch_loss_fro_fb_, batch_loss_l1_fb_ = criterion_stft_fb(batch_x_output_fb, batch_x_fb)
        batch_loss_fro_fb = batch_loss_fro_fb_.mean().item()
        batch_loss_l1_fb = batch_loss_l1_fb_.mean().item()
        total_train_loss["train/loss_fro"].append(batch_loss_fro_avg)
        total_train_loss["train/loss_l1"].append(batch_loss_l1_avg)
        total_train_loss["train/loss_fro_fb"].append(batch_loss_fro_fb)
        total_train_loss["train/loss_l1_fb"].append(batch_loss_l1_fb)
        loss_fro_avg.append(batch_loss_fro_avg)
        loss_l1_avg.append(batch_loss_l1_avg)
        loss_fro_fb.append(batch_loss_fro_fb)
        loss_l1_fb.append(batch_loss_l1_fb)
        ## Total
        batch_loss += batch_loss_ce_.sum() + batch_loss_ce_f_.sum() \
                        + batch_loss_ce_.mean(-1).sum() + batch_loss_ce_f_.mean(-1).sum() \
                            + batch_loss_fro_.sum() + batch_loss_l1_.sum() \
                                + batch_loss_fro_fb_.sum() + batch_loss_l1_fb_.sum()
        ## /Total
        # /loss

        # == Backprop. & Param. Optim. ==
        #logging.info(model_waveform.logits_c.weight[:,0])
        #logging.info(model_waveform.logits_f.weight[:,0])

        optimizer.zero_grad()
        batch_loss.backward()
        flag = False
        for name, param in model_waveform.named_parameters():
            if param.requires_grad:
                grad_norm = param.grad.norm()
                if torch.isnan(grad_norm) or torch.isinf(grad_norm):
                    flag = True
        if flag:
            logging.info("explode grad")
            optimizer.zero_grad()
            continue
        torch.nn.utils.clip_grad_norm_(model_waveform.parameters(), 10)
        optimizer.step()
        # == /Backprop. & Param. Optim. ==

        #logging.info(model_waveform.logits_c.weight[:,0])
        #logging.info(model_waveform.logits_f.weight[:,0])
        #logging.info(model_waveform.logits_sgns_c.weight[:,0].tanh())
        #logging.info(model_waveform.logits_mags_c.weight[:,0].exp())
        #logging.info(model_waveform.logits_sgns_f.weight[:,0])
        #logging.info(model_waveform.logits_sgns_f.weight[:,0].tanh())
        #logging.info(model_waveform.logits_mags_f.weight[:,0].exp())

        # == Sparsification ==
        with torch.no_grad():
            if idx_stage < args.n_stage-1 and iter_idx + 1 == t_starts[idx_stage+1]:
                idx_stage += 1
            if idx_stage > 0:
                sparsify(model_waveform, iter_idx + 1, t_starts[idx_stage], t_ends[idx_stage], args.interval, densities[idx_stage], densities_p=densities[idx_stage-1])
            else:
                sparsify(model_waveform, iter_idx + 1, t_starts[idx_stage], t_ends[idx_stage], args.interval, densities[idx_stage])
        # == /Sparsification ==

        # == Training summary of this batch ==
        text_log = "batch loss [%d] %d %d %d %d %d : %.3f %.3f %% %.3f %.3f %% %.3f %.3f %% , %.3f %.3f , %.3f %.3f" % (c_idx+1, max_slen, x_ss, x_bs,
            f_ss, f_bs, batch_loss_ce_avg, batch_loss_err_avg, batch_loss_ce_c_avg, batch_loss_err_c_avg,
                batch_loss_ce_f_avg, batch_loss_err_f_avg, batch_loss_fro_avg, batch_loss_l1_avg, batch_loss_fro_fb, batch_loss_l1_fb)
        for i in range(args.n_bands):
            text_log += " [%d] %.3f %.3f %% %.3f %.3f %% , %.3f %.3f" % (i+1,
                batch_loss_ce[i], batch_loss_err[i], batch_loss_ce_f[i], batch_loss_err_f[i], batch_loss_fro[i], batch_loss_l1[i])
        logging.info("%s (%.3f sec)" % (text_log, time.time() - start))
        # == /Training summary of this batch ==

        iter_idx += 1
        #if iter_idx % args.save_interval_iter == 0:
        #    logging.info('save iter:%d' % (iter_idx))
        #    save_checkpoint(args.expdir, model_waveform, optimizer, np.random.get_state(), torch.get_rng_state(), iter_idx)
        iter_count += 1
        
        # Logging (Log & Tensorboard)
        if iter_idx % args.log_interval_steps == 0:
            logging.info('smt')
            for key in total_train_loss.keys():
                total_train_loss[key] = np.mean(total_train_loss[key])
                logging.info(f"(Steps: {iter_idx}) {key} = {total_train_loss[key]:.4f}.")
            write_to_tensorboard(writer, iter_idx, total_train_loss)
            # Reset `total_train_loss`
            total_train_loss = defaultdict(list)
        # /Logging (Log & Tensorboard)

        total += time.time() - start

        # /Training of a batch
        # =============================================================================================================================================
        # =============================================================================================================================================
        # =============================================================================================================================================

    logging.info("Maximum step is reached, please check the development optimum index, or continue training by increasing maximum step.")


if __name__ == "__main__":
    main()
