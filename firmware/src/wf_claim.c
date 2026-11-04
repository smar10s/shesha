#include "wf_claim.h"

void wf_claim_init(wf_claim_t *c, size_t n)
{
    c->n = n;
    c->workers = 1;
    atomic_store(&c->next, 0);
    atomic_store(&c->origin, 0);
    atomic_store(&c->produced, 0);
}

void wf_claim_set_workers(wf_claim_t *c, int workers)
{
    c->workers = workers < 1 ? 1 : workers;
}

uint32_t wf_claim_lag_frames(const wf_claim_t *c, uint32_t budget)
{
    /* W workers hold W blocks in flight, so the newest claimed block starts
     * out (W - 1) frames ahead of the oldest one still in progress.  Add that
     * back, or the cap measures the queue rather than the backlog and
     * abandons blocks that were never late. */
    return budget + (uint32_t)(c->workers - 1);
}

void wf_claim_reset(wf_claim_t *c, size_t n)
{
    int workers = c->workers;
    wf_claim_init(c, n);
    c->workers = workers;
}

uint64_t wf_claim_reserve(wf_claim_t *c, uint64_t produced)
{
    uint64_t off = atomic_load(&c->next);
    for (;;) {
        if (produced < (uint64_t)c->n || off > produced - (uint64_t)c->n)
            return WF_CLAIM_NONE;
        if (off > UINT64_MAX - (uint64_t)c->n)
            return WF_CLAIM_NONE;

        if (atomic_compare_exchange_weak(&c->next, &off, off + c->n))
            return off;
        /* Lost the race: `off` now holds the block another worker reserved. */
    }
}

uint32_t wf_claim_origin(wf_claim_t *c, uint32_t fresh)
{
    uint64_t cur = atomic_load(&c->origin);
    if (cur & WF_CLAIM_ORIGIN_SET)
        return (uint32_t)cur;

    /* Race to publish.  One compare-and-swap carries both the value and its
     * validity, so a worker that loses cannot overwrite the winner's.  It
     * deliberately does not touch `produced`: seeding the count here used to
     * be a second store, and a worker observing between the CAS and that
     * store had its total rewound. */
    uint64_t want = (uint64_t)fresh | WF_CLAIM_ORIGIN_SET;
    uint64_t expect = 0;
    if (atomic_compare_exchange_strong(&c->origin, &expect, want))
        return fresh;

    /* Lost: `expect` now holds the winner's value, which is the real one. */
    return (uint32_t)expect;
}

uint64_t wf_claim_observe(wf_claim_t *c, uint32_t wr, uint32_t ring)
{
    uint64_t origin = atomic_load(&c->origin);
    if (!(origin & WF_CLAIM_ORIGIN_SET))
        return 0;                       /* no reference to count from yet */
    origin &= 0xFFFFFFFFu;              /* drop the validity bit */

    uint64_t cur = atomic_load(&c->produced);
    for (;;) {
        /* The last pointer reading is where offset zero plus everything
         * counted since then sits in the ring.  Deriving it from the
         * origin keeps the count a full 64-bit word instead of a field
         * packed beside a stored pointer. */
        uint32_t last = (uint32_t)((origin + cur) & (ring - 1));

        if (wr == last)
            return cur;                 /* nothing new since the last read */

        /*
         * Unwrap against the last reading ANY worker took, derived from the
         * shared total.  Sharing that is the whole point: the pointer
         * carries 25 bits, so a worker unwrapping from its own private
         * history recovers the total only modulo the ring, and one joining
         * after the first wrap is permanently short.
         *
         * But a shared last_wr means readings can arrive out of order: two
         * workers poll concurrently, and the one that read the pointer a
         * moment earlier can publish second.  Its pointer is BEHIND last_wr,
         * which is indistinguishable from a wrap by magnitude alone.  Treating
         * it as one adds a whole ring to the total -- measured at 13% of calls
         * under four-way contention, inflating the count 66,000-fold.
         *
         * A real wrap always lands near the bottom of the ring having left
         * from near the top, so only a backward step of more than half a ring
         * is a wrap.  Anything smaller is a stale reading, and the correct
         * response is to ignore it: the published total already includes a
         * newer reading than ours.
         */
        uint32_t forward = (wr >= last) ? (wr - last) : 0;
        if (wr < last) {
            uint32_t back = last - wr;
            if (back < ring / 2)
                return cur;             /* stale reading, not a wrap */
            forward = (ring - last) + wr;
        }

        uint64_t want = cur + forward;
        if (atomic_compare_exchange_weak(&c->produced, &cur, want))
            return want;

        /* Lost the race.  `cur` now accounts for a reading at least as
         * recent as ours, so the retry either finds our reading stale or
         * folds in only what is newer. */
    }
}

uint64_t wf_claim_abandon(wf_claim_t *c, uint64_t produced,
                          size_t block_samples)
{
    return wf_claim_skip_forward(c, produced) + (uint64_t)block_samples;
}

uint64_t wf_claim_skip_forward(wf_claim_t *c, uint64_t produced)
{
    /*
     * Resume two frames back from the write pointer, not one.
     *
     * The obvious target is the newest whole frame, at produced - n.  But the
     * claim is where the NEXT block starts, so publishing that makes the
     * following block [produced, produced + n) -- entirely in the ADC's
     * future.  Whoever claims it waits a full frame period for samples that
     * do not exist yet, and with a capture timeout shorter than that period
     * it times out instead.
     *
     * Backing off one more frame means the skip lands on a block that is
     * fully written and the one after it is complete by the time anyone
     * claims it.  It costs one frame of extra latency, well inside
     * WF_MAX_LAG_FRAMES.
     */
    if (produced < 2 * c->n)
        return 0;                       /* not enough stream to skip into */

    /* Land on a frame boundary: the frame index must stay meaningful, and an
     * unaligned claim would make every later index a fraction. */
    uint64_t target = ((produced - 2 * c->n) / c->n) * c->n;

    uint64_t expect = atomic_load(&c->next);
    while (expect < target) {
        if (atomic_compare_exchange_weak(&c->next, &expect, target))
            return target - expect;
        /* expect now holds the value another worker published; loop to
         * re-test, which falls out if that worker jumped further. */
    }
    return 0;
}
