#include "wifi_mode.h"
#include "wifi_rx.h"
#include "wifi_scan.h"
#include "spsc_ring.h"
#include "ring_slots.h"
#include "wifi_protocol.h"
#include "thread_stats.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

extern atomic_bool g_running;
extern spsc_ring_t *g_wifi_ringbuf;

static pthread_t g_producer_thread;
static atomic_bool g_producer_running = false;

extern double monotonic_sec(void);

static void *run(void *arg)
{
    (void)arg;
    thread_stats_name_self("wifi-producer");
    wifi_slot_t slot;

    while (atomic_load(&g_running) && atomic_load(&g_producer_running)) {
        if (wifi_rx_decode_one(&slot.frame) == 0) {
            slot.ts = monotonic_sec();
            spsc_ring_push(g_wifi_ringbuf, &slot);
        }
    }
    return NULL;
}

int wifi_mode_enter(void)
{
    /* Idempotency guard, matching waterfall_mode_enter and fm_mode_enter.
     * Without it a second enter overwrote g_producer_thread and
     * g_wifi_ringbuf, leaking the old thread (never joinable, and left
     * running against a freed ringbuf) plus three allocations.  Reachable
     * on every FM TX resume via tx_resume_producer(), and via CMD_SET_FPGA
     * disable. */
    if (atomic_load(&g_producer_running)) return 0;

    if (wifi_rx_init() != 0) {
        fprintf(stderr, "wifi_mode: wifi_rx_init failed\n");
        return -1;
    }
    g_wifi_ringbuf = spsc_ring_create(sizeof(wifi_slot_t), RING_SLOTS_WIFI);
    if (!g_wifi_ringbuf) {
        wifi_rx_cleanup();
        return -1;
    }
    atomic_store(&g_producer_running, true);
    if (pthread_create(&g_producer_thread, NULL, run, NULL) != 0) {
        fprintf(stderr, "wifi_mode: pthread_create failed\n");
        atomic_store(&g_producer_running, false);
        spsc_ring_destroy(g_wifi_ringbuf);
        g_wifi_ringbuf = NULL;
        wifi_rx_cleanup();
        return -1;
    }
    printf("mode: wifi\n");
    return 0;
}

void wifi_mode_exit(void)
{
    if (atomic_load(&g_producer_running)) {
        atomic_store(&g_producer_running, false);
        pthread_join(g_producer_thread, NULL);
    }
    spsc_ring_destroy(g_wifi_ringbuf);
    g_wifi_ringbuf = NULL;
    wifi_rx_cleanup();
}

bool wifi_mode_running(void)
{
    return atomic_load(&g_producer_running);
}

bool wifi_mode_active(void)
{
    return atomic_load(&g_producer_running);
}

void drain_wifi(const drain_ctx_t *ctx)
{
    static wifi_slot_t *wifi_slots;

    if (!g_wifi_ringbuf) return;

    if (!wifi_slots) {
        wifi_slots = calloc(WIFI_POP_MAX, sizeof(wifi_slot_t));
        if (!wifi_slots) return;
    }

    size_t start_seq;
    int n = spsc_ring_pop_all(g_wifi_ringbuf, wifi_slots,
                              WIFI_POP_MAX, &start_seq);

    for (int i = 0; i < n; i++) {
        wifi_rx_frame_t *f = &wifi_slots[i].frame;
        float rx_gain = f->rx_gain;

        wifi_frame_class_t fclass = wifi_classify_frame(
            f->psdu, f->psdu_len, f->fcs_ok, f->rate_mbps, f->length);

        wifi_meta_t meta;
        memset(&meta, 0, sizeof(meta));
        wifi_ie_summary_t ies;
        memset(&ies, 0, sizeof(ies));
        wifi_ie_summary_t *ies_ptr = NULL;

        if (f->fcs_ok && f->psdu_len >= 24) {
            wifi_parse_header(f->psdu, f->psdu_len, &meta);
            fclass = wifi_refine_class(fclass, &meta);
            if (fclass == WIFI_FCLASS_BEACON || fclass == WIFI_FCLASS_PROBE_RESP) {
                wifi_parse_ies(f->psdu, f->psdu_len, &ies);
                if (ies.channel != 0 || ies.security != 0 || ies.phy_flags != 0) {
                    meta.meta_flags |= WIFI_META_HAS_IE_SUMMARY;
                    ies_ptr = &ies;
                }
            }
        }

        size_t frame_len = proto_build_wifi(ctx->sendbuf,
                                             (uint32_t)(start_seq + i),
                                             wifi_slots[i].ts,
                                             f, rx_gain, fclass, &meta, ies_ptr);
        ctx->sink(ctx->sendbuf, frame_len, ctx->sink_user);
    }
}
