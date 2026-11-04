#include "wf_fft.h"

#include <fftw3.h>
#include <stdlib.h>

struct wf_fft_plan {
    size_t n;
    fftwf_plan forward;
};

wf_fft_plan *wf_fft_plan_create_n(size_t n)
{
    wf_fft_plan *plan = malloc(sizeof(*plan));
    if (!plan)
        return NULL;

    plan->n = n;

    /*
     * The guru split-complex planner takes arrays only to decide in-place
     * versus out-of-place; FFTW_ESTIMATE does not execute, and the execute
     * call re-supplies the arrays, so this scratch lives only as long as the
     * planning call.  Four regions so the input and output pointers differ,
     * which is what makes the plan out-of-place.
     */
    float *scratch = fftwf_malloc(4 * n * sizeof(float));
    if (!scratch) {
        free(plan);
        return NULL;
    }
    float *re_in  = scratch;
    float *im_in  = scratch + n;
    float *re_out = scratch + 2 * n;
    float *im_out = scratch + 3 * n;

    fftwf_iodim dim = { .n = (int)n, .is = 1, .os = 1 };
    plan->forward = fftwf_plan_guru_split_dft(
        1, &dim, 0, NULL, re_in, im_in, re_out, im_out,
        FFTW_ESTIMATE | FFTW_UNALIGNED | FFTW_PRESERVE_INPUT);

    fftwf_free(scratch);

    if (!plan->forward) {
        free(plan);
        return NULL;
    }
    return plan;
}

void wf_fft_plan_destroy(wf_fft_plan *plan)
{
    if (plan) {
        fftwf_destroy_plan(plan->forward);
        free(plan);
    }
}

void wf_fft_forward_split(wf_fft_plan *plan,
                          const float *in_real, const float *in_imag,
                          float *out_real, float *out_imag)
{
    /* FFTW's execute API is not const-correct.  The out-of-place split plan
     * was made with FFTW_PRESERVE_INPUT, so it reads the input, never writes
     * it. */
    fftwf_execute_split_dft(plan->forward, (float *)in_real,
                            (float *)in_imag, out_real, out_imag);
}
