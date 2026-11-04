/* firmware/src/wf_claim.h — the shared read position for PSD producers.
 *
 * One producer can keep a private read pointer, because nothing else reads.
 * Two cannot: both would latch the same DMA write pointer and transform the
 * same samples, costing two cores and covering what one already covered.  So
 * the read position moves out of waterfall_t and becomes this: a counter that
 * hands out disjoint blocks of the sample stream.
 *
 * Offsets are monotonic sample counts from where the producer started, not
 * ring addresses.  That keeps them comparable across workers and makes the
 * frame index -- offset divided by frame length -- a global ordering the drain
 * and the wire can both use.
 *
 * All functions are safe to call from any producer thread.  None of them
 * touch hardware, so the logic is host-tested.
 */
#pragma once

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    _Atomic uint64_t next;   /* next sample offset to hand out */
    size_t           n;      /* frame length, in samples */

    /*
     * How many samples the ADC has produced since offset zero.
     *
     * Shared, and necessarily so.  The count is a running total but the write
     * pointer only carries 25 bits, so it wraps every 33.5 M samples -- 3.4 s
     * at 10 MSPS.  A worker that derived its own total from the current
     * pointer could only ever recover it modulo the ring, so a worker joining
     * after the first wrap starts short by whole ring multiples and never
     * catches up.  Observed on target: worker 1 was handed offsets 14x beyond
     * its own count and timed out on every single capture, 0 frames against
     * 133 timeouts.
     *
     * A full 64-bit word, not a field packed beside the pointer.  The old
     * packed form gave the count 39 bits and wrapped after 2^39 samples --
     * 7.6 hours at 20 MSPS.  The last pointer reading is now derived from
     * `origin` plus this count, so advancing the total is still one
     * compare-and-swap on the hottest shared value in the producer.
     */
    _Atomic uint64_t produced;

    /*
     * Where offset zero is, as a DMA ring address.  Shared, because the
     * offsets are only meaningful relative to one agreed origin: a worker
     * that latched its own would resolve every offset to the wrong samples.
     *
     * Validity is packed into the same word as the value (bit 32) so that
     * latching it is one compare-and-swap.  A separate flag would need the
     * value stored before the flag and would still let a losing worker
     * clobber the winner's value in between.
     */
    _Atomic uint64_t origin;

    /*
     * How many workers draw from this claim.
     *
     * The latency cap is per-block, but W workers hold W blocks in flight at
     * once, so the newest claimed block is already W frames ahead of the
     * oldest one still being transformed.  A cap of WF_MAX_LAG_FRAMES
     * measured against the newest therefore leaves only (cap - W) frames of
     * real headroom, and blocks get abandoned that were never actually late:
     * the claim outran the workers, not the ADC outrunning the claim.
     * Measured at 2 workers, 10 MSPS, N=4096: 25 M samples skipped against
     * 151 M transformed, holding coverage at ~88% where one worker's own cap
     * would have allowed ~100%.
     *
     * So the cap scales with the worker count, which keeps the per-block
     * headroom -- and therefore the display latency bound -- the same
     * regardless of how many workers are running.
     */
    int              workers;
} wf_claim_t;

/* Set when `origin` holds a latched ring address. */
#define WF_CLAIM_ORIGIN_SET (1ULL << 32)

/* Start handing out blocks of `n` samples from offset zero. */
void wf_claim_init(wf_claim_t *c, size_t n);

/* How many workers draw from this claim.  Set before they start; it scales
 * the latency cap so each block keeps the same headroom. */
void wf_claim_set_workers(wf_claim_t *c, int workers);

/* Frames of backlog a single block may accumulate, given the worker count
 * and a per-block budget. */
uint32_t wf_claim_lag_frames(const wf_claim_t *c, uint32_t budget);

/* Restart at offset zero with a new frame length.  Offsets measured in the
 * old length index nothing in the new one, so a size change must reset.
 * Also drops the origin, so the next producer to run re-latches it. */
void wf_claim_reset(wf_claim_t *c, size_t n);

/*
 * The ring address of offset zero, latching `fresh` if nobody has yet.
 *
 * Every worker calls this and they all get the same answer, which is the
 * point: a late-starting worker must measure from the origin the first one
 * established, not from where the write pointer happens to be when it wakes
 * up.
 */
uint32_t wf_claim_origin(wf_claim_t *c, uint32_t fresh);

/*
 * Fold the current DMA write pointer into the shared produced count and
 * return the total.
 *
 * `ring` is the ring size, needed to unwrap the pointer, and must be a power
 * of two no larger than 2^25.  Callers must poll often enough that the
 * pointer advances less than a full ring between calls, which the capture
 * loop's 50 us sleep beats by three orders of magnitude.
 *
 * The origin must be latched first; before that there is no reference to
 * count from and the call returns zero.  Callers that lose the CAS still
 * return a total that includes their own reading, because the winner's total
 * is at least as new.
 */
uint64_t wf_claim_observe(wf_claim_t *c, uint32_t wr, uint32_t ring);

/*
 * Reserve the next block only if the ADC has already written all of it.
 *
 * Returns the block's starting offset, or WF_CLAIM_NONE when the stream has
 * not reached it.  A refusal leaves the claim untouched, so a worker that
 * times out cannot walk the read position into the future: `next` never
 * exceeds `produced`.
 */
#define WF_CLAIM_NONE UINT64_MAX

uint64_t wf_claim_reserve(wf_claim_t *c, uint64_t produced);

/* Position of a block in the global frame order, for merging and for the
 * wire sequence number. */
static inline uint64_t wf_claim_frame_index(const wf_claim_t *c,
                                            uint64_t offset)
{
    return offset / c->n;
}

/*
 * Abandon everything up to the newest whole frame the ADC has produced, and
 * return how many samples that gave up (zero if the claim was already level
 * with or ahead of there).
 *
 * Published by compare-and-swap rather than a store, and only ever forwards.
 * Two workers can find their blocks stale in the same instant; the furthest
 * jump must win, or a worker that had already skipped would be dragged back
 * onto samples it deliberately gave up on.
 *
 * The returned count is the coverage denominator's loss term, so it must be
 * the real advance and not the intended one -- a worker that races and loses
 * abandoned nothing.
 */
uint64_t wf_claim_skip_forward(wf_claim_t *c, uint64_t produced);

/*
 * Give up a block the caller reserved and skip the claim forward to the live
 * part of the stream.  Returns the total samples lost, which is the claim
 * advance plus the caller's own block: the advance starts at `next`, already
 * past that block, so counting only the return of skip_forward undercounts
 * coverage by one frame per skip.
 *
 * Several workers may abandon their blocks in the same instant.  Each adds
 * its own block; only the worker that wins the claim CAS also adds the
 * advance.  Together they account for the whole abandoned span.
 */
uint64_t wf_claim_abandon(wf_claim_t *c, uint64_t produced,
                          size_t block_samples);
