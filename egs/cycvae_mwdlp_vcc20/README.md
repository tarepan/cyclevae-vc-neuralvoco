# CycleVAE-MWDLP VC

Dataset: [Voice Conversion Challenge 2020 dataset](http://vc-challenge.org/).

Real-time implementation is based on [LPCNet](https://github.com/mozilla/LPCNet/).

## How to Use
Preprocessing -> 4-step Training -> Clang model compiling -> 2 Demo  

Three core scripts: `download_vcc20.sh`, `run.sh` and `run_realtime.sh`  
Manually update variables in the script, then run it.  

| Purpose                          |       Script        |     Variables              | Notes           |
| -------------------------------- | ------------------- | -------------------------- |---------------- |
| Data download                    | `download_vcc20.sh` |    -                       |                 |
| Preprocessing                    | `run.sh`            | `stage=0init123` & `n_jobs=` | thread number |
| VC model training                | `run.sh`            | `stage=4` & `GPU_device=X` | take ~ 2.5 days |
| Vocoder training                 | `run.sh`            | `stage=5` & `GPU_device=X` | take ~ 4   days |
| VC fine-tuning w/ fixed vocoder  | `run.sh`            | `stage=6` & `GPU_device=X` | take ~ 2.5 days |
| VC decoder FT w/ fixed Enc & Voc | `run.sh`            | `stage=6` & `GPU_device=X` | take ~ 2.5 days |
| Compile CPU real-time program    | `run_realtime.sh`   | `stage=0`                  |                 |
| Decode w/ target speaker points  | `run_realtime.sh`   | `stage=3` & `spks_src_dec=` & `spks_trg_dec=`||
| Decode w/ interp. speaker points | `run_realtime.sh`   | `stage=4` & `spks_src_dec=` & `n_interp=` |  |
