/*
 * waterfall_set_fft_size() must reset the recent-coverage window along with
 * the lifetime counters.  The window is indexed in sample counts of the old
 * frame length, so carrying it across a size change reports a transformed
 * total for frames that never existed.
 *
 * The FFT and DMA are stubbed: this test is about the counters, and the host
 * has no radio.
 */
#include "../src/waterfall.h"
#include "../src/wf_claim.h"
#include "../src/dma_rx.h"
#include "../src/hal.h"
#include "../src/wf_fft.h"

#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

#define N 512u
#define DDR_WORDS 65536u

static uint32_t fake_wr;
static uint32_t ddr[DDR_WORDS];

struct wf_fft_plan {
    size_t n;
};

static struct wf_fft_plan fake_plan;

wf_fft_plan *wf_fft_plan_create_n(size_t n)
{
    fake_plan.n = n;
    return &fake_plan;
}

void wf_fft_plan_destroy(wf_fft_plan *plan)
{
    (void)plan;
}

void wf_fft_forward_split(wf_fft_plan *plan,
                          const float *in_real, const float *in_imag,
                          float *out_real, float *out_imag)
{
    (void)in_real;
    (void)in_imag;
    for (size_t i = 0; i < plan->n; i++) {
        out_real[i] = 0.0f;
        out_imag[i] = 0.0f;
    }
}

void adc_clip_add(uint64_t clips, uint64_t samples)
{
    (void)clips;
    (void)samples;
}

uint32_t dma_rx_wr_ptr(void)
{
    return fake_wr;
}

volatile uint32_t *hal_ddr_rx_buf(void)
{
    return ddr;
}

static void test_fft_size_change_resets_recent_coverage(void)
{
    wf_claim_t claim;
    float pow[2 * N];

    wf_claim_init(&claim, N);
    (void)wf_claim_origin(&claim, 0);

    waterfall_t *wf = waterfall_create(N, WINDOW_HANN, &claim);
    CHECK(wf != NULL);
    if (!wf)
        return;

    /* One frame, no skips. */
    fake_wr = N;
    CHECK(waterfall_process(wf, pow) == 0);

    /* A second frame that had to jump forward, so the skip counter is
     * nonzero too. */
    fake_wr = N + 10u * N;
    CHECK(waterfall_process(wf, pow) == 0);

    uint64_t transformed = 0;
    uint64_t skipped = 0;
    waterfall_get_recent_coverage(wf, &transformed, &skipped);
    CHECK(transformed == 2u * N);
    CHECK(skipped > 0);

    CHECK(waterfall_set_fft_size(wf, 2u * N) == 0);

    transformed = 99;
    skipped = 99;
    waterfall_get_recent_coverage(wf, &transformed, &skipped);
    CHECK(transformed == 0);
    CHECK(skipped == 0);

    waterfall_destroy(wf);
}

int main(void)
{
    TEST(test_fft_size_change_resets_recent_coverage);

    if (fails) {
        printf("%d check(s) failed\n", fails);
        return 1;
    }
    printf("all checks passed\n");
    return 0;
}
