/* firmware/src/wf_workers_policy.h — how many PSD producer cores to use.
 *
 * Measurement says a second producer core is exactly 1.00x gain at or below
 * fs_max: one core already transforms every sample, so the extra worker only
 * waits -- and waiting polls the DMA write pointer, costing 14-17 points of
 * duty for no coverage.  Above fs_max the second core converts almost
 * perfectly into coverage (docs/psd-waterfall.md, "The second core").
 *
 * So the worker count is what the user asked for, clamped to one at or below
 * fs_max.  The decision is pure arithmetic over the measured C(N) table, so
 * it lives here and is tested on the host; a boundary mistake silently spends
 * a core for nothing.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

/* The Zynq 7010 has two Cortex-A9 cores, and one worker is pinned to each. */
#define WF_MAX_WORKERS 2

/* Sample rate, in Hz, at or below which one worker already covers the whole
 * stream for an FFT of `n` samples.  An untabulated size falls back to the
 * nearest tabulated one; the daemon only ever passes a power of two. */
uint64_t wf_workers_fs_max_hz(size_t n);

/* Effective worker count: `requested` clamped to [1, WF_MAX_WORKERS], then
 * reduced to one when `fs_hz` is at or below fs_max.  Never lowers below one
 * and never raises above the request. */
int wf_workers_effective(int requested, uint64_t fs_hz, size_t n);
