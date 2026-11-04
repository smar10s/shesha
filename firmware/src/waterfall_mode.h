/* firmware/src/waterfall_mode.h — the software PSD producer.
 *
 * Runs one worker thread per core, each transforming disjoint blocks of the
 * sample stream (wf_claim.h).  The worker count is a knob because measurement
 * says a second worker only pays above fs_max: at or below it one core
 * already transforms every sample, and the extra thread adds duty for no
 * coverage.  See docs/psd-waterfall.md.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include "producer.h"
#include "window.h"
#include "wf_workers_policy.h"

#define WATERFALL_DEFAULT_FFT_SIZE 4096

int  waterfall_mode_enter(void);
void waterfall_mode_exit(void);

/* Workers actually running now.  `count` is the number of worker threads
 * alive right now (zero while the producer is stopped, starting, or if every
 * worker failed), and is what RESP_STATUS reports.  The effective target is
 * computed in enter() from the current sample rate and FFT size. */
int  waterfall_mode_worker_count(void);

/* True while the producer is started and at least one worker is alive. */
bool waterfall_mode_running(void);

/* True from the moment enter() starts workers until exit() joins them. */
bool waterfall_mode_active(void);

/* The user's request, clamped to the core count.  Takes effect on the next
 * enter(), so callers cycle the producer.  The fs_max clamp is applied in
 * enter(); see wf_workers_policy.h. */
void waterfall_mode_set_worker_count(int n);

/* The request as stored, before the fs_max clamp.  Configuration rollback
 * needs the same value the user last asked for. */
int  waterfall_mode_worker_requested(void);

/*
 * No config fan-out, deliberately.  Every worker has its own waterfall_t, so
 * a setter would seem to need one -- but every config path in main.c already
 * brackets its mutation with exit()/enter(), and exit() destroys workers 1..
 * while enter() rebuilds them from g_waterfall's current settings.  A
 * fan-out would therefore only ever write to instances that are about to be
 * freed.
 *
 * The invariant that makes this safe: workers 1.. exist only while the
 * producer is running, and nothing mutates g_waterfall while it runs.
 */

/* Coverage over the recent window, summed across workers.  Coverage is a
 * property of the producer set: with two workers each transforms about half
 * the frames, so one worker's counters alone read about half the truth. */
void waterfall_mode_get_coverage(uint64_t *samples_transformed,
                                 uint64_t *samples_skipped);

/* Drain every worker's ring.  `out` must hold WF_MAX_WORKERS *
 * RING_SLOTS_PSD slots of `slot_sz` bytes.  Returns the slot count.
 * Slots arrive grouped by worker, NOT in frame order — the caller orders
 * them by psd_slot_t.frame_index. */
int  waterfall_mode_pop_all(void *out, size_t slot_sz);

void drain_waterfall(const drain_ctx_t *ctx);
