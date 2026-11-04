/*
 * Host tests for capture-time metadata in the mode drains.
 *
 * The discriminator is the live radio snapshot: each slot is stamped with
 * frequency A, the snapshot is then moved to B, and the drain must still
 * emit A.  The waterfall case also proves the newest slot is chosen by
 * frame_index rather than by position in the pop buffer.
 */
#include "../src/producer.h"
#include "../src/ring_slots.h"
#include "../src/spsc_ring.h"
#include "../src/protocol.h"
#include "../src/radio.h"
#include "../src/waterfall.h"
#include "../src/waterfall_mode.h"
#include "../src/iq_mode.h"
#include "../src/iq_pack.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_CAPTURED 16
#define SEND_BUF_SIZE (1 << 16)

/* --- globals the mode modules expect ------------------------------------ */

waterfall_t *g_waterfall;
spsc_ring_t *g_ringbuf;
spsc_ring_t *g_iq_ringbuf;
radio_state_t g_radio;

/* --- deterministic host substitutes ------------------------------------- */

static double g_now = 100.0;
static size_t g_fft_size = 8;
static int g_wf_marker;

double monotonic_sec(void)
{
    return g_now;
}

size_t waterfall_get_fft_size(const waterfall_t *wf)
{
    (void)wf;
    return g_fft_size;
}

void radio_capture_publish(const radio_state_t *state)
{
    (void)state;
}

void radio_capture_snapshot(uint64_t *freq_hz, uint32_t *sample_rate_hz)
{
    *freq_hz = g_radio.freq_hz;
    *sample_rate_hz = g_radio.sample_rate_hz;
}

/* --- capture sink ------------------------------------------------------- */

typedef struct {
    int count;
    size_t len[MAX_CAPTURED];
    uint8_t frame[MAX_CAPTURED][SEND_BUF_SIZE];
} capture_t;

static capture_t g_capture;

static void capture_frame(const uint8_t *frame, size_t len, void *user)
{
    capture_t *cap = user;
    if (cap->count >= MAX_CAPTURED)
        return;
    if (len > SEND_BUF_SIZE)
        return;
    cap->len[cap->count] = len;
    memcpy(cap->frame[cap->count], frame, len);
    cap->count++;
}

static float frame_freq_mhz(const uint8_t *frame)
{
    float f;
    memcpy(&f, frame + FRAME_HEADER_SIZE + 8, sizeof(f));
    return f;
}

static uint32_t frame_u32(const uint8_t *frame, size_t off)
{
    uint32_t v;
    memcpy(&v, frame + FRAME_HEADER_SIZE + off, sizeof(v));
    return v;
}

static uint16_t frame_u16(const uint8_t *frame, size_t off)
{
    uint16_t v;
    memcpy(&v, frame + FRAME_HEADER_SIZE + off, sizeof(v));
    return v;
}

static uint8_t frame_u8(const uint8_t *frame, size_t off)
{
    return frame[FRAME_HEADER_SIZE + off];
}

static uint32_t frame_seq(const uint8_t *frame)
{
    uint32_t seq;
    memcpy(&seq, frame + 5, sizeof(seq));
    return seq;
}

static double frame_ts(const uint8_t *frame)
{
    double ts;
    memcpy(&ts, frame + FRAME_HEADER_SIZE, sizeof(ts));
    return ts;
}

/* --- harness ------------------------------------------------------------ */

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* --- IQ ----------------------------------------------------------------- */

static void push_iq(uint64_t freq_hz, uint32_t rate_hz, uint8_t bits,
                    uint16_t n, double ts)
{
    iq_slot_t *slot = calloc(1, sizeof(*slot));
    if (!slot)
        return;
    slot->ts = ts;
    slot->n = n;
    slot->bits = bits;
    slot->freq_hz = freq_hz;
    slot->sample_rate_hz = rate_hz;
    for (uint16_t i = 0; i < n; i++) {
        slot->packed[i * 3 + 0] = (uint8_t)i;
        slot->packed[i * 3 + 1] = (uint8_t)(i + 1);
        slot->packed[i * 3 + 2] = (uint8_t)(i + 2);
    }
    spsc_ring_push(g_iq_ringbuf, slot);
    free(slot);
}

static void test_iq_uses_slot_metadata(void)
{
    const uint64_t freq_a = 433920000;
    const uint32_t rate_a = 2100000;
    const uint64_t freq_b = 915000000;
    const uint32_t rate_b = 4000000;

    memset(&g_capture, 0, sizeof(g_capture));
    g_iq_ringbuf = spsc_ring_create(sizeof(iq_slot_t), 8);
    CHECK(g_iq_ringbuf != NULL);
    if (!g_iq_ringbuf)
        return;

    for (int i = 0; i < 3; i++)
        push_iq(freq_a, rate_a, IQ_BITS_12, 16, 500.0 + i);

    uint8_t *sendbuf = malloc(SEND_BUF_SIZE);
    CHECK(sendbuf != NULL);
    if (!sendbuf) {
        spsc_ring_destroy(g_iq_ringbuf);
        g_iq_ringbuf = NULL;
        return;
    }

    double last_broadcast = 0;
    drain_ctx_t ctx = {
        .sendbuf        = sendbuf,
        .sink           = capture_frame,
        .sink_user      = &g_capture,
        .last_broadcast = &last_broadcast,
        .target_fps     = 50,
        .accum          = ACCUM_NONE,
    };

    drain_iq(&ctx);
    CHECK(g_capture.count == 3);
    for (int i = 0; i < g_capture.count; i++) {
        const uint8_t *f = g_capture.frame[i];
        CHECK(f[0] == DATA_IQ);
        CHECK(frame_seq(f) == (uint32_t)i);
        CHECK(fabsf(frame_freq_mhz(f) - (float)freq_a / 1e6f) < 0.001f);
        CHECK(frame_u32(f, 12) == rate_a);
        CHECK(frame_u8(f, 16) == IQ_BITS_12);
        CHECK(frame_u16(f, 18) == 16);
        CHECK(frame_ts(f) == 500.0 + i);
    }

    /* Move the live snapshot after the slots were stamped.  The drain sees
     * a different radio state but must keep each slot's own metadata. */
    g_radio.freq_hz = freq_b;
    g_radio.sample_rate_hz = rate_b;

    for (int i = 0; i < 2; i++)
        push_iq(freq_a, rate_a, IQ_BITS_8, 8, 600.0 + i);

    drain_iq(&ctx);
    CHECK(g_capture.count == 5);
    for (int i = 3; i < g_capture.count; i++) {
        const uint8_t *f = g_capture.frame[i];
        CHECK(fabsf(frame_freq_mhz(f) - (float)freq_a / 1e6f) < 0.001f);
        CHECK(frame_u32(f, 12) == rate_a);
        CHECK(frame_u8(f, 16) == IQ_BITS_8);
        CHECK(frame_u16(f, 18) == 8);
        CHECK(frame_seq(f) == (uint32_t)i);
    }

    free(sendbuf);
    spsc_ring_destroy(g_iq_ringbuf);
    g_iq_ringbuf = NULL;
    memset(&g_radio, 0, sizeof(g_radio));
}

/* --- PSD ---------------------------------------------------------------- */

static void push_psd(uint64_t frame_index, uint64_t freq_hz, double ts,
                     float base, uint32_t peak_bin, float peak)
{
    size_t slot_sz = psd_slot_size(g_fft_size);
    psd_slot_t *slot = malloc(slot_sz);
    if (!slot)
        return;
    slot->ts = ts;
    slot->frame_index = frame_index;
    slot->freq_hz = freq_hz;
    slot->sample_rate_hz = 3000000;
    for (size_t i = 0; i < g_fft_size; i++)
        slot->power[i] = base;
    if (peak_bin < g_fft_size)
        slot->power[peak_bin] = peak;
    spsc_ring_push(g_ringbuf, slot);
    free(slot);
}

static void test_waterfall_uses_newest_slot_metadata(void)
{
    const uint64_t freq_a = 100000000;
    const uint64_t freq_b = 200000000;
    const uint64_t freq_c = 300000000;
    const uint64_t freq_d = 400000000;

    memset(&g_capture, 0, sizeof(g_capture));
    g_fft_size = 8;
    g_waterfall = (waterfall_t *)&g_wf_marker;
    g_ringbuf = spsc_ring_create(psd_slot_size(g_fft_size), 16);
    CHECK(g_ringbuf != NULL);
    if (!g_ringbuf) {
        g_waterfall = NULL;
        return;
    }

    uint8_t *sendbuf = malloc(SEND_BUF_SIZE);
    CHECK(sendbuf != NULL);
    if (!sendbuf) {
        spsc_ring_destroy(g_ringbuf);
        g_ringbuf = NULL;
        g_waterfall = NULL;
        return;
    }

    /* Older A carries the larger peak and newer B is flat.  ACCUM_MAX must
     * pick A's peak while the metadata still comes from B; ACCUM_NONE would
     * emit B's flat bin, so the bin assertion discriminates the two. */
    push_psd(10, freq_a, 1001.0, 1.0f, 0, 4.0f);
    push_psd(11, freq_b, 1002.0, 1.0f, g_fft_size, 1.0f);

    double last_broadcast = 0;
    drain_ctx_t ctx = {
        .sendbuf        = sendbuf,
        .sink           = capture_frame,
        .sink_user      = &g_capture,
        .last_broadcast = &last_broadcast,
        .target_fps     = 50,
        .accum          = ACCUM_MAX,
    };

    drain_waterfall(&ctx);
    CHECK(g_capture.count == 1);
    if (g_capture.count == 1) {
        const uint8_t *f = g_capture.frame[0];
        CHECK(f[0] == DATA_PSD);
        CHECK(frame_seq(f) == 11);
        CHECK(fabsf(frame_freq_mhz(f) - (float)freq_b / 1e6f) < 0.001f);
        CHECK(frame_ts(f) == 1002.0);
        CHECK(frame_u32(f, 20) == g_fft_size);
        float bin0;
        memcpy(&bin0, f + FRAME_HEADER_SIZE + 24, sizeof(bin0));
        CHECK(fabsf(bin0 - 10.0f * log10f(4.0f)) < 0.01f);
    }

    /* Newest slot pushed first: prove selection follows frame_index, not
     * pop-buffer order.  Move the live radio somewhere else as well. */
    g_radio.freq_hz = 500000000;
    g_radio.sample_rate_hz = 4000000;
    push_psd(21, freq_c, 2021.0, 7.0f, g_fft_size, 7.0f);
    push_psd(20, freq_d, 2020.0, 9.0f, g_fft_size, 9.0f);

    g_now = 101.0;
    memset(&g_capture, 0, sizeof(g_capture));
    ctx.accum = ACCUM_NONE;
    drain_waterfall(&ctx);
    CHECK(g_capture.count == 1);
    if (g_capture.count == 1) {
        const uint8_t *f = g_capture.frame[0];
        CHECK(frame_seq(f) == 21);
        CHECK(fabsf(frame_freq_mhz(f) - (float)freq_c / 1e6f) < 0.001f);
        CHECK(frame_ts(f) == 2021.0);
        float bin0;
        memcpy(&bin0, f + FRAME_HEADER_SIZE + 24, sizeof(bin0));
        CHECK(fabsf(bin0 - 10.0f * log10f(7.0f)) < 0.01f);
    }

    free(sendbuf);
    spsc_ring_destroy(g_ringbuf);
    g_ringbuf = NULL;
    g_waterfall = NULL;
    memset(&g_radio, 0, sizeof(g_radio));
}

static void test_waterfall_average_uses_linear_power(void)
{
    const uint64_t freq = 100000000;

    memset(&g_capture, 0, sizeof(g_capture));
    g_fft_size = 8;
    g_waterfall = (waterfall_t *)&g_wf_marker;
    g_ringbuf = spsc_ring_create(psd_slot_size(g_fft_size), 16);
    CHECK(g_ringbuf != NULL);
    if (!g_ringbuf) {
        g_waterfall = NULL;
        return;
    }

    uint8_t *sendbuf = malloc(SEND_BUF_SIZE);
    CHECK(sendbuf != NULL);
    if (!sendbuf) {
        spsc_ring_destroy(g_ringbuf);
        g_ringbuf = NULL;
        g_waterfall = NULL;
        return;
    }

    /* Two slots whose bin 0 powers average to 2.5.  Averaging the dB values
     * gives about 3.89 dB, whose linear equivalent is the geometric mean
     * sqrt(2*3) ~= 2.449, not 2.5.  The assertion sees that difference. */
    push_psd(30, freq, 3000.0, 1.0f, 0, 2.0f);
    push_psd(31, freq, 3001.0, 1.0f, 0, 3.0f);

    double last_broadcast = 0;
    drain_ctx_t ctx = {
        .sendbuf        = sendbuf,
        .sink           = capture_frame,
        .sink_user      = &g_capture,
        .last_broadcast = &last_broadcast,
        .target_fps     = 50,
        .accum          = ACCUM_AVG,
    };

    drain_waterfall(&ctx);
    CHECK(g_capture.count == 1);
    if (g_capture.count == 1) {
        const uint8_t *f = g_capture.frame[0];
        CHECK(frame_seq(f) == 31);
        CHECK(frame_ts(f) == 3001.0);
        float bin0;
        memcpy(&bin0, f + FRAME_HEADER_SIZE + 24, sizeof(bin0));
        CHECK(fabsf(bin0 - 10.0f * log10f(2.5f)) < 0.01f);
        float bin1;
        memcpy(&bin1, f + FRAME_HEADER_SIZE + 28, sizeof(bin1));
        CHECK(fabsf(bin1) < 0.01f);
    }

    free(sendbuf);
    spsc_ring_destroy(g_ringbuf);
    g_ringbuf = NULL;
    g_waterfall = NULL;
    memset(&g_radio, 0, sizeof(g_radio));
}

int main(void)
{
    TEST(test_iq_uses_slot_metadata);
    TEST(test_waterfall_uses_newest_slot_metadata);
    TEST(test_waterfall_average_uses_linear_power);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
