#ifndef MWDLP10NET_PRIVATE_H
#define MWDLP10NET_PRIVATE_H

#include "mwdlp10net.h"
#include "nnet.h"
#include "nnet_data.h"

//PLT_Dec20
/*
    define followings on nnet_data.h:
    RNN_MAIN_NEURONS, RNN_SUB_NEURONS,
    MID_OUT, N_QUANTIZE, SQRT_QUANTIZE,
    FEATURE_CONV_OUT_SIZE, N_MBANDS,
    DLPC_ORDER, PQMF_ORDER,
    N_SAMPLE_BANDS, FEATURES_DIM,
    CONV_KERNEL_1, FEATURE_CONV_STATE_SIZE, FEATURES_DIM, FEATURE_CONV_DELAY
*/
#define RNN_MAIN_NEURONS_2 RNN_MAIN_NEURONS * 2
#define RNN_SUB_NEURONS_2 RNN_SUB_NEURONS * 2

#define RNN_MAIN_NEURONS_3 RNN_MAIN_NEURONS * 3
#define RNN_SUB_NEURONS_3 RNN_SUB_NEURONS * 3

#define RNN_MAIN_NEURONS_3_SQRT_QUANTIZE RNN_MAIN_NEURONS_3 * SQRT_QUANTIZE
#define RNN_SUB_NEURONS_3_SQRT_QUANTIZE RNN_SUB_NEURONS_3 * SQRT_QUANTIZE

#define NO_DLPC (DLPC_ORDER == 0)
#define NO_DLPC_MBANDS NO_DLPC * N_MBANDS

#define LPC_ORDER_MBANDS DLPC_ORDER * N_MBANDS
#define LPC_ORDER_MBANDS_2 LPC_ORDER_MBANDS * 2
#define LPC_ORDER_MBANDS_4 LPC_ORDER_MBANDS_2 * 2
#define LPC_ORDER_1_MBANDS (DLPC_ORDER - 1) * N_MBANDS

#define MID_OUT_MBANDS MID_OUT * N_MBANDS
#define MID_OUT_MBANDS_2 MID_OUT_MBANDS * 2

#define LPC_ORDER_MBANDS_3 LPC_ORDER_MBANDS * 3
#define LPC_ORDER_MBANDS_2_MID_OUT_MBANDS (LPC_ORDER_MBANDS_2 + MID_OUT_MBANDS)
#define LPC_ORDER_MBANDS_3_MID_OUT_MBANDS (LPC_ORDER_MBANDS_3 + MID_OUT_MBANDS)
#define LPC_ORDER_MBANDS_4_MID_OUT_MBANDS (LPC_ORDER_MBANDS_4 + MID_OUT_MBANDS)

#define FEATURE_CONV_STATE_SIZE_1 (FEATURE_CONV_STATE_SIZE - FEATURES_DIM)

/*
PQMF_DELAY is actually the number of samples on each of the left/right side of the current sample
for the kaiser window, i.e., half of the value of PQMF_ORDER (even number).
*/
#define PQMF_DELAY PQMF_ORDER / 2
#define PQMF_ORDER_MBANDS PQMF_ORDER * N_MBANDS
#define N_MBANDS_SQR N_MBANDS * N_MBANDS

/*
A bit confusing, but PQMF_ORDER is the number of taps for kaiser window in pqmf.py.
So, it has to be an even number because covering left and right sides of the current sample t.
Because the number of points in kaiser window is 1+PQMF_ORDER, i.e, current_sample+(left+right).
*/
#define TAPS (PQMF_ORDER + 1)
#define TAPS_MBANDS TAPS * N_MBANDS

/*
DLPC_ORDER is the number of coefficients for data-driven LPC,
i.e., the number of previous samples considered in the LP computation.
*/
#define MDENSE_OUT_DUALFC (DLPC_ORDER * 2 + MID_OUT)
#define MDENSE_OUT_DUALFC_MBANDS MDENSE_OUT_DUALFC * N_MBANDS
#define MDENSE_OUT_DUALFC_2_MBANDS MDENSE_OUT_DUALFC_MBANDS * 2
#define MDENSE_OUT_FC (DLPC_ORDER * 2 + SQRT_QUANTIZE)
#define MDENSE_OUT_FC_MBANDS MDENSE_OUT_FC * N_MBANDS
#define SQRT_QUANTIZE_MBANDS SQRT_QUANTIZE * N_MBANDS

#define INIT_LAST_SAMPLE SQRT_QUANTIZE / 2

/*
MAX_N_OUTPUT either from FIRST n-outputs [due to remainder of (PQMF_DELAY+1) % N-BANDS
    because first samples are supposed to be PQMF_DELAY+1, but if the N-BANDS are not divisible
    by PQMF_DELAY, the remainder samples are actually the very first samples because the multiband synthesis
    is done in a multiple of N-BANDS, where 1 contribution to PQMF_DELAY+1 is automatically added
    after each synthesis]
    FIRST_N_OUTPUT = (((PQMF_DELAY / NBANDS) + (PQMF_DELAY % NBANDS)) * NBANDS) % PQMF_DELAY
    We want to find what is the minimum number of samples to reach the PQMF_DELAY,
    in a multiple of NBANDS, then take the remainder with respect to PQMF_DELAY
    as the very first output if exists.
LAST n-outputs [due to frame- and pqmf-delays, w/ right-side replicate- and zero-padding, respectively]
*/
//need to add as (PQMF_DELAY + 0) to make the remainder operation works
#define FIRST_N_OUTPUT ((((PQMF_DELAY / N_MBANDS) + (PQMF_DELAY % N_MBANDS)) * N_MBANDS) % (PQMF_DELAY + 0))
#define MAX_N_OUTPUT IMAX((FIRST_N_OUTPUT + 1) * N_SAMPLE_BANDS * N_MBANDS, \
                    N_SAMPLE_BANDS * FEATURE_CONV_DELAY * N_MBANDS + PQMF_DELAY)

#define FIRST_N_OUTPUT_MBANDS FIRST_N_OUTPUT * N_MBANDS
#define PQMF_DELAY_MBANDS PQMF_DELAY * N_MBANDS


//PLT_Sep21
struct MWDLP10NetState {
    MWDLP10NNetState nnet;
    float mu_law_10_table[N_QUANTIZE];
    short last_coarse[LPC_ORDER_MBANDS+NO_DLPC_MBANDS];
    short last_fine[LPC_ORDER_MBANDS+NO_DLPC_MBANDS];
    int frame_count;
    int sample_count;
    int first_flag;
    float deemph_mem;
    //upsample-bands,zero-pad-right,NBxNB
    float buffer_output[N_MBANDS_SQR];
    /*
        in_state pqmf_synth filt.,(ORD+1)*NB+(NB-1)*NB=ORD*NB+NB*NB
        for the very first output, zeros to the left of the very first [{ORD-1}-th] as:
        [[0,...,0]_1st,[0,...,0]_2nd,...,[[(1st,...,NB-th)*NB]_1st,[0,...0]_2nd,...,[0,...,0]_NB-th]]_{ORD+1}]
        for NB-bands and kaiser_length=ORD+1, where at each time-index, the dimension is NB*NB
        nonzeros for (1st*NB) and zeros for the (2nd-to-NB)*NB
        it then shifts to the left for every new output
    */
    float pqmf_state[PQMF_ORDER_MBANDS+N_MBANDS_SQR];
    //first in_state pqmf_synth filt.,(ORD+1)*NB+(FIRST_N_OUTPUT-1)*NB=ORD*NB+FIRST_N_OUTPUT*NB
    float first_pqmf_state[PQMF_ORDER_MBANDS+FIRST_N_OUTPUT_MBANDS];
    //last in_state pqmf_synth filt.,(ORD+1)*NB+(ORD//2-1)*NB=ORD*NB+DELAY*NB
    float last_pqmf_state[PQMF_ORDER_MBANDS+PQMF_DELAY_MBANDS];
#if defined(WINDOWS_SYS) || defined (GNU_EXT)
    RNGState rng_state;
#endif
};


#endif
