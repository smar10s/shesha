/* The shared frame claim: two producers must transform disjoint blocks, and
 * must agree on where to resume after falling behind.
 *
 * Tested on the host because the interesting cases are races.  On target they
 * are rare and untriggerable on demand; here they are just function calls in
 * a chosen order.
 */
#include "../src/wf_claim.h"

#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* The ADC has written far enough for any block these tests ask for. */
#define ANY_PRODUCED UINT64_MAX

/* Blocks must be disjoint and contiguous: this is what makes two workers
 * worth having instead of two workers doing the same work twice. */
static void test_claims_are_disjoint_and_contiguous(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);

    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 0);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 1024);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 2048);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 3072);
}

/* Frame index is the claim offset divided by the frame length, so it orders
 * frames across workers.  The drain merges on it and the wire carries it. */
static void test_frame_index_orders_across_workers(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 4096);

    uint64_t a = wf_claim_reserve(&c, ANY_PRODUCED);     /* worker 0 */
    uint64_t b = wf_claim_reserve(&c, ANY_PRODUCED);     /* worker 1 */
    uint64_t d = wf_claim_reserve(&c, ANY_PRODUCED);     /* worker 0 again */

    CHECK(wf_claim_frame_index(&c, a) == 0);
    CHECK(wf_claim_frame_index(&c, b) == 1);
    CHECK(wf_claim_frame_index(&c, d) == 2);
}

/* Falling behind: the claim jumps forward, and reports how many samples were
 * given up so coverage stays honest. */
static void test_skip_advances_toward_the_write_pointer(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1000);

    (void)wf_claim_reserve(&c, ANY_PRODUCED);            /* claim is now 1000 */
    /* 50000 produced.  Resume at 48000, two frames back -- see below for
     * why not 49000.  Everything from 1000 to 48000 is abandoned. */
    CHECK(wf_claim_skip_forward(&c, 50000) == 47000);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 48000);
}

/*
 * The block AFTER a skip must already be written.
 *
 * The claim names where the next block starts, so skipping to the newest
 * whole frame leaves the block after it entirely in the ADC's future.
 * Whoever draws that block waits a frame period for samples that do not
 * exist, and a capture timeout shorter than that period fires first.
 *
 * On target at 10 MSPS this starved the second worker completely: 1 frame
 * against 152 timeouts, with total coverage stuck at the one-core figure.
 */
static void test_skip_never_hands_out_a_future_block(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 4096);

    const uint64_t produced = 1000000;
    (void)wf_claim_skip_forward(&c, produced);

    /* Both the block the skip landed on and the one after it must be fully
     * written already. */
    uint64_t first = wf_claim_reserve(&c, ANY_PRODUCED);
    uint64_t second = wf_claim_reserve(&c, ANY_PRODUCED);
    CHECK(first + 4096 <= produced);
    CHECK(second + 4096 <= produced);
}

/* Early in a run there is not yet enough stream to back off into. */
static void test_skip_needs_two_frames_of_stream(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1000);

    CHECK(wf_claim_skip_forward(&c, 1500) == 0);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 0);
}

/*
 * A worker that abandons the block it reserved must count that block too.
 * skip_forward starts from `next`, which is already past the reserved block,
 * so its return is short by one frame per skip.  Two workers that both
 * abandon their blocks must together account for the whole abandoned span,
 * including the block the second one finds already skipped over.
 */
static void test_abandon_counts_the_reserved_block(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1000);

    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 0);   /* next is now 1000 */
    /* The skip advances 47000 from 1000 to 48000; the abandoned block adds
     * the last 1000, for the true 48000 samples given up. */
    CHECK(wf_claim_abandon(&c, 50000, 1000) == 48000);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 48000);
}

static void test_two_abandoned_blocks_are_both_counted(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1000);

    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 0);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 1000);

    /* The first worker's abandonment advances the claim far past both
     * blocks, so its own block is the only part left to add. */
    CHECK(wf_claim_abandon(&c, 50000, 1000) == 47000);
    /* The second worker already lost the claim race, but its reserved block
     * was still abandoned by it. */
    CHECK(wf_claim_abandon(&c, 50000, 1000) == 1000);
}

/* Two workers can notice staleness in the same instant.  Only the furthest
 * jump may win: a smaller one must not drag the claim backwards onto samples
 * a worker had already abandoned. */
static void test_concurrent_skip_keeps_the_furthest(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1000);

    /* Worker A saw 50000 produced and skipped to 48000. */
    CHECK(wf_claim_skip_forward(&c, 50000) == 48000);
    /* Worker B was slower and only saw 30000.  Its skip must be a no-op. */
    CHECK(wf_claim_skip_forward(&c, 30000) == 0);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 48000);
}

/* A skip that would not advance anything reports nothing abandoned.  Without
 * this the coverage denominator would inflate every time a worker raced and
 * lost. */
static void test_redundant_skip_abandons_nothing(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1000);
    (void)wf_claim_reserve(&c, ANY_PRODUCED);
    (void)wf_claim_reserve(&c, ANY_PRODUCED);            /* claim is now 2000 */

    CHECK(wf_claim_skip_forward(&c, 2500) == 0);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 2000);
}

/* Before a full frame exists there is nowhere to skip to. */
static void test_skip_before_first_frame_is_a_noop(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 4096);
    CHECK(wf_claim_skip_forward(&c, 100) == 0);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 0);
}

/* The skip must land on a frame boundary, or every later frame index would be
 * a fraction and the drain could not order frames at all. */
static void test_skip_lands_on_a_frame_boundary(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);

    /* 5000 produced, back off two frames to 2952, round down to 2048. */
    CHECK(wf_claim_skip_forward(&c, 5000) == 2048);
    uint64_t off = wf_claim_reserve(&c, ANY_PRODUCED);
    CHECK(off == 2048);
    CHECK(off % 1024 == 0);
    CHECK(wf_claim_frame_index(&c, off) == 2);
}

/* An FFT-size change restarts the claim: offsets measured in old frame
 * lengths index nothing in the new one. */
static void test_reset_restarts_the_claim(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    (void)wf_claim_reserve(&c, ANY_PRODUCED);
    (void)wf_claim_reserve(&c, ANY_PRODUCED);

    wf_claim_reset(&c, 4096);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 0);
    CHECK(wf_claim_reserve(&c, ANY_PRODUCED) == 4096);
}

/* --- reserving only written blocks -------------------------------------- */

/*
 * A block may be handed out only once the ADC has written all of it.
 *
 * Handing out a block that starts in the future makes the worker wait, time
 * out after 50 ms, and claim another.  Nothing ever pulls the claim back, so
 * a run of timeouts walks it arbitrarily far ahead of the stream and the PSD
 * freezes until the ADC catches up -- minutes, on target.
 */
static void test_reserve_requires_a_written_block(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);

    CHECK(wf_claim_reserve(&c, 0) == WF_CLAIM_NONE);
    CHECK(wf_claim_reserve(&c, 1023) == WF_CLAIM_NONE);   /* partial frame */
    CHECK(wf_claim_reserve(&c, 1024) == 0);
    CHECK(wf_claim_reserve(&c, 1024) == WF_CLAIM_NONE);   /* next not written */
    CHECK(wf_claim_reserve(&c, 2048) == 1024);
}

/* A refused reservation must not move the claim.  That is what turns a
 * timeout into permanent progress toward the future. */
static void test_reserve_refusal_does_not_advance(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);

    for (int i = 0; i < 1000; i++)
        CHECK(wf_claim_reserve(&c, 0) == WF_CLAIM_NONE);

    CHECK(wf_claim_reserve(&c, 1024) == 0);
}

/* A producer run starts a new stream: resetting the claim must discard an
 * offset left ahead of the ADC by the previous run.  Otherwise changing the
 * worker count or mode inherits the stall. */
static void test_reset_heals_an_ahead_claim(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);

    for (int i = 0; i < 100; i++) {
        uint64_t want = (uint64_t)i * 1024;
        CHECK(wf_claim_reserve(&c, UINT64_MAX / 2) == want);
    }

    wf_claim_reset(&c, 1024);
    CHECK(wf_claim_reserve(&c, 1024) == 0);
}

/* --- the shared origin -------------------------------------------------- */

/*
 * Claim offsets are counted from one agreed origin, so every worker must
 * resolve them against the same one.  A worker that latched its own would
 * read the wrong samples for every offset AND wait for sample counts measured
 * from a zero already in the past -- which starves a late worker silently
 * rather than failing.  Measured on target before this was shared: worker 1
 * ran at 17% duty and total coverage did not improve at all.
 */
static void test_origin_is_latched_once_and_shared(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);

    /* First caller wins and its value becomes everyone's. */
    CHECK(wf_claim_origin(&c, 5000) == 5000);
    /* A later worker, waking up at a different write pointer, gets the
     * origin that was already established -- not its own. */
    CHECK(wf_claim_origin(&c, 9999) == 5000);
    CHECK(wf_claim_origin(&c, 1) == 5000);
}

/* Zero is a legitimate ring address, so validity cannot be inferred from the
 * value being non-zero. */
static void test_origin_zero_is_a_real_value(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);

    CHECK(wf_claim_origin(&c, 0) == 0);
    CHECK(wf_claim_origin(&c, 7777) == 0);
}

/* A size change restarts the claim, so the origin must be re-latched too:
 * the old one pairs with offsets in the old frame length. */
static void test_reset_drops_the_origin(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    CHECK(wf_claim_origin(&c, 5000) == 5000);

    wf_claim_reset(&c, 4096);
    CHECK(wf_claim_origin(&c, 1234) == 1234);
}

/* Under contention exactly one origin must win, and every worker must come
 * away with that same one -- including the losers, whose own candidate must
 * not have overwritten the winner's. */
#define ORIGIN_THREADS 4

typedef struct {
    wf_claim_t *claim;
    uint32_t    candidate;
    uint32_t    got;
} origin_racer_t;

static void *race_origin(void *arg)
{
    origin_racer_t *r = arg;
    r->got = wf_claim_origin(r->claim, r->candidate);
    return NULL;
}

static void test_concurrent_origin_latch_agrees(void)
{
    for (int trial = 0; trial < 200; trial++) {
        wf_claim_t c;
        wf_claim_init(&c, 1024);

        origin_racer_t r[ORIGIN_THREADS];
        pthread_t th[ORIGIN_THREADS];
        for (int t = 0; t < ORIGIN_THREADS; t++) {
            r[t].claim = &c;
            r[t].candidate = 1000u * (uint32_t)(t + 1);
            r[t].got = 0xFFFFFFFFu;
            if (pthread_create(&th[t], NULL, race_origin, &r[t]) != 0) {
                printf("  FAIL could not start thread %d\n", t);
                g_fails++;
                return;
            }
        }
        for (int t = 0; t < ORIGIN_THREADS; t++)
            pthread_join(th[t], NULL);

        /* All agree, the winner is one of the candidates, and a later read
         * still returns it. */
        uint32_t agreed = r[0].got;
        bool all_agree = true, is_candidate = false;
        for (int t = 0; t < ORIGIN_THREADS; t++) {
            if (r[t].got != agreed) all_agree = false;
            if (agreed == 1000u * (uint32_t)(t + 1)) is_candidate = true;
        }
        if (!all_agree || !is_candidate ||
            wf_claim_origin(&c, 42) != agreed) {
            printf("  FAIL trial %d: agreed=%u all=%d cand=%d\n",
                   trial, agreed, (int)all_agree, (int)is_candidate);
            g_fails++;
            return;
        }
    }
}

/* --- shared ADC progress ------------------------------------------------ */

#define TEST_RING (1u << 25)

/* The running total must survive the write pointer wrapping, which it does
 * every 33.5 M samples -- 3.4 s at 10 MSPS. */
static void test_observe_accumulates_across_a_wrap(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    (void)wf_claim_origin(&c, 0);

    CHECK(wf_claim_observe(&c, 1000, TEST_RING) == 1000);
    CHECK(wf_claim_observe(&c, TEST_RING - 10, TEST_RING) == TEST_RING - 10);
    /* Pointer wraps past zero: the total must keep climbing, not reset. */
    CHECK(wf_claim_observe(&c, 90, TEST_RING) == TEST_RING + 90);
    CHECK(wf_claim_observe(&c, 100, TEST_RING) == TEST_RING + 100);
}

/* A repeat reading adds nothing: the DMA has not moved. */
static void test_observe_is_idempotent(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    (void)wf_claim_origin(&c, 0);

    CHECK(wf_claim_observe(&c, 4096, TEST_RING) == 4096);
    CHECK(wf_claim_observe(&c, 4096, TEST_RING) == 4096);
    CHECK(wf_claim_observe(&c, 4096, TEST_RING) == 4096);
}

/*
 * The produced total must be a full 64-bit count, not a fixed-width field
 * packed beside the write pointer.  A 39-bit count wraps after 2^39
 * samples -- 7.6 hours at 20 MSPS -- and the PSD then freezes because every
 * later reading is smaller than the last.
 *
 * Walk the pointer across a ring boundary repeatedly: each pair adds one
 * whole ring to the count.  The final total is past 2^39.
 */
static void test_progress_survives_past_39_bits(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    (void)wf_claim_origin(&c, 0);

    uint64_t expect = TEST_RING - 1;
    CHECK(wf_claim_observe(&c, TEST_RING - 1, TEST_RING) == expect);

    for (int i = 0; i < 16384; i++) {
        expect += 1;
        CHECK(wf_claim_observe(&c, 0, TEST_RING) == expect);
        expect += TEST_RING - 1;
        CHECK(wf_claim_observe(&c, TEST_RING - 1, TEST_RING) == expect);
    }
    CHECK(expect > (1ULL << 39));
}

/*
 * Progress is counted from the origin.  Until it is latched there is no
 * reference to count from, so an observation must not advance the total.
 * Latching the origin then establishes offset zero without rewinding a
 * count somebody else may already have published.
 */
static void test_observe_before_origin_is_inert(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);

    CHECK(wf_claim_observe(&c, 4096, TEST_RING) == 0);
    CHECK(wf_claim_origin(&c, 1000) == 1000);
    CHECK(wf_claim_observe(&c, 4096, TEST_RING) == 3096);
}

/* The origin seeds the count, so the first delta is measured from there
 * rather than from ring address zero. */
static void test_observe_starts_from_the_origin(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    (void)wf_claim_origin(&c, 1000000);

    CHECK(wf_claim_observe(&c, 1000000, TEST_RING) == 0);
    CHECK(wf_claim_observe(&c, 1004096, TEST_RING) == 4096);
}

/*
 * A worker that joins late must see the total the others have accumulated,
 * NOT a count it could derive from the current pointer.
 *
 * This is the bug that starved the second producer on target.  Each worker
 * kept a private total unwrapped from its own history, so one starting after
 * the stream had passed a ring boundary was short by whole ring multiples --
 * permanently.  It was handed claim offsets 14x past its own count and timed
 * out on every capture: 0 frames against 133 timeouts, with total coverage
 * stuck at the one-core figure.
 */
static void test_late_joiner_sees_the_accumulated_total(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 4096);
    (void)wf_claim_origin(&c, 0);

    /* Worker 0 runs the stream well past one full ring. */
    wf_claim_observe(&c, TEST_RING - 1, TEST_RING);
    wf_claim_observe(&c, 5000000, TEST_RING);          /* wrapped once */
    uint64_t established = wf_claim_observe(&c, 9000000, TEST_RING);
    CHECK(established > TEST_RING);

    /* Worker 1 wakes up and reads the same pointer.  It must get the full
     * total, not the ~9 M it would compute from the pointer alone. */
    uint64_t joiner = wf_claim_observe(&c, 9000000, TEST_RING);
    CHECK(joiner == established);
    CHECK(joiner > TEST_RING);
}

/*
 * Concurrent readings must neither lose ground nor double-count.
 *
 * Every thread reads one shared cursor, because that is what the hardware
 * looks like: there is a single DMA write pointer and it only advances.
 * Feeding threads independent pointer histories would model a pointer that
 * jumps backwards, which cannot happen and which observe() is entitled to
 * read as a wrap.
 */
#define PROGRESS_THREADS 4
#define PROGRESS_STEPS   4000
#define PROGRESS_STEP    64

typedef struct {
    wf_claim_t       *claim;
    _Atomic uint32_t *cursor;     /* the shared "DMA write pointer" */
    uint64_t          last;
    int               went_backwards;
} observer_t;

static void *observe_loop(void *arg)
{
    observer_t *o = arg;
    for (int i = 0; i < PROGRESS_STEPS; i++) {
        /* Advance the shared pointer, then read it: two workers polling the
         * same monotonic source. */
        uint32_t wr = atomic_fetch_add(o->cursor, PROGRESS_STEP)
                      + PROGRESS_STEP;
        uint64_t got = wf_claim_observe(o->claim, wr & (TEST_RING - 1),
                                        TEST_RING);
        if (got < o->last)
            o->went_backwards++;
        o->last = got;
    }
    return NULL;
}

static void test_concurrent_observe_never_goes_backwards(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    (void)wf_claim_origin(&c, 0);

    _Atomic uint32_t cursor = 0;
    observer_t o[PROGRESS_THREADS];
    pthread_t th[PROGRESS_THREADS];
    for (int t = 0; t < PROGRESS_THREADS; t++) {
        o[t] = (observer_t){ .claim = &c, .cursor = &cursor, .last = 0,
                             .went_backwards = 0 };
        if (pthread_create(&th[t], NULL, observe_loop, &o[t]) != 0) {
            printf("  FAIL could not start thread %d\n", t);
            g_fails++;
            return;
        }
    }
    for (int t = 0; t < PROGRESS_THREADS; t++)
        pthread_join(th[t], NULL);

    for (int t = 0; t < PROGRESS_THREADS; t++)
        CHECK(o[t].went_backwards == 0);

    /* The total must equal how far the pointer actually MOVED.  Not 4x it,
     * which double-counting would give, and not vastly more, which treating
     * out-of-order readings as ring wraps would give -- that inflated the
     * count 66,000-fold before observe() learned to tell the two apart. */
    uint32_t walked = atomic_load(&cursor);
    CHECK(walked == PROGRESS_THREADS * PROGRESS_STEPS * PROGRESS_STEP);
    CHECK(wf_claim_observe(&c, walked & (TEST_RING - 1), TEST_RING)
          == (uint64_t)walked);
}

/*
 * A reading that arrives out of order must be ignored, not mistaken for a
 * wrap.  Two workers poll concurrently and the one that sampled the pointer
 * a moment earlier can publish second, so its pointer sits behind the
 * published one -- which looks exactly like a wrap if only magnitude is
 * considered.
 */
static void test_stale_reading_is_not_a_wrap(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    (void)wf_claim_origin(&c, 0);

    CHECK(wf_claim_observe(&c, 1000000, TEST_RING) == 1000000);
    /* A slightly older reading: ignore it, do not add a ring. */
    CHECK(wf_claim_observe(&c, 999000, TEST_RING) == 1000000);
    CHECK(wf_claim_observe(&c, 500000, TEST_RING) == 1000000);
    /* Forward progress still works afterwards. */
    CHECK(wf_claim_observe(&c, 1001000, TEST_RING) == 1001000);
}

/* A genuine wrap leaves from near the top of the ring and lands near the
 * bottom, so it must still be counted. */
static void test_genuine_wrap_is_still_counted(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    (void)wf_claim_origin(&c, 0);

    CHECK(wf_claim_observe(&c, TEST_RING - 1000, TEST_RING)
          == TEST_RING - 1000);
    /* Backward by more than half a ring: a real wrap. */
    CHECK(wf_claim_observe(&c, 500, TEST_RING) == TEST_RING + 500);
}

/* --- the latency cap scales with the worker count ----------------------- */

/*
 * The cap bounds how stale a block may be when it is transformed.  W workers
 * hold W blocks in flight, so the newest claimed block starts out (W - 1)
 * frames ahead of the oldest one still in progress.  Measuring a fixed cap
 * against the newest therefore shrinks the real per-block headroom as workers
 * are added, and abandons blocks that were never late.
 *
 * Measured at 2 workers, 10 MSPS, N=4096 before this scaled: 25 M samples
 * skipped against 151 M transformed, pinning coverage at ~88%.
 */
static void test_lag_budget_scales_with_workers(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 4096);

    /* One worker: the budget is exactly the per-block budget. */
    CHECK(wf_claim_lag_frames(&c, 4) == 4);

    wf_claim_set_workers(&c, 2);
    CHECK(wf_claim_lag_frames(&c, 4) == 5);

    wf_claim_set_workers(&c, 4);
    CHECK(wf_claim_lag_frames(&c, 4) == 7);
}

/* A nonsensical worker count must not shrink the budget below the
 * single-worker case, which would make captures fail outright. */
static void test_lag_budget_floors_at_one_worker(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 4096);

    wf_claim_set_workers(&c, 0);
    CHECK(wf_claim_lag_frames(&c, 4) == 4);
    wf_claim_set_workers(&c, -3);
    CHECK(wf_claim_lag_frames(&c, 4) == 4);
}

/* An FFT-size change restarts the claim but must not forget how many
 * workers are drawing from it -- they are still running. */
static void test_reset_keeps_the_worker_count(void)
{
    wf_claim_t c;
    wf_claim_init(&c, 1024);
    wf_claim_set_workers(&c, 2);

    wf_claim_reset(&c, 4096);
    CHECK(wf_claim_lag_frames(&c, 4) == 5);
}

/* --- under real contention ---------------------------------------------- */

/*
 * The sequential tests above fix the arithmetic; this one fixes the property
 * the whole design rests on.  Two workers must never be handed the same
 * block, because the entire benefit of a second core is that it transforms
 * samples the first one did not.  Duplicate blocks would show up on target as
 * doubled duty with flat coverage -- expensive to notice, trivial to assert
 * here.
 */
#define CONTENTION_THREADS 4
#define CONTENTION_CLAIMS  20000
#define CONTENTION_N       64

typedef struct {
    wf_claim_t *claim;
    uint64_t    offsets[CONTENTION_CLAIMS];
} grabber_t;

static void *grab(void *arg)
{
    grabber_t *g = arg;
    for (int i = 0; i < CONTENTION_CLAIMS; i++)
        g->offsets[i] = wf_claim_reserve(g->claim, ANY_PRODUCED);
    return NULL;
}

static void test_concurrent_claims_never_collide(void)
{
    wf_claim_t c;
    wf_claim_init(&c, CONTENTION_N);

    static grabber_t g[CONTENTION_THREADS];
    pthread_t th[CONTENTION_THREADS];

    for (int t = 0; t < CONTENTION_THREADS; t++) {
        g[t].claim = &c;
        if (pthread_create(&th[t], NULL, grab, &g[t]) != 0) {
            printf("  FAIL could not start thread %d\n", t);
            g_fails++;
            return;
        }
    }
    for (int t = 0; t < CONTENTION_THREADS; t++)
        pthread_join(th[t], NULL);

    /* Every block handed out must be distinct and frame-aligned.  Mark each
     * frame index seen and check nothing was issued twice. */
    const int total = CONTENTION_THREADS * CONTENTION_CLAIMS;
    static unsigned char seen[CONTENTION_THREADS * CONTENTION_CLAIMS];
    int duplicates = 0, unaligned = 0, out_of_range = 0;

    for (int t = 0; t < CONTENTION_THREADS; t++) {
        for (int i = 0; i < CONTENTION_CLAIMS; i++) {
            uint64_t off = g[t].offsets[i];
            if (off % CONTENTION_N != 0) { unaligned++; continue; }
            uint64_t idx = off / CONTENTION_N;
            if (idx >= (uint64_t)total) { out_of_range++; continue; }
            if (seen[idx]) duplicates++;
            seen[idx] = 1;
        }
    }

    CHECK(duplicates == 0);
    CHECK(unaligned == 0);
    CHECK(out_of_range == 0);

    /* Contiguous too: with no skips, the claims must tile the range exactly,
     * leaving no frame unissued. */
    int missing = 0;
    for (int i = 0; i < total; i++)
        if (!seen[i]) missing++;
    CHECK(missing == 0);
}

/*
 * Skipping while others are claiming must not hand out a block twice or move
 * the claim backwards.  This is the case a plain store would break: a worker
 * publishing a stale target could rewind the claim onto frames already
 * issued, and two workers would then transform the same samples.
 */
static void *grab_and_skip(void *arg)
{
    grabber_t *g = arg;
    for (int i = 0; i < CONTENTION_CLAIMS; i++) {
        g->offsets[i] = wf_claim_reserve(g->claim, ANY_PRODUCED);
        /* Periodically pretend to have fallen behind, with a deliberately
         * stale view of how much the ADC produced. */
        if (i % 97 == 0)
            (void)wf_claim_skip_forward(g->claim,
                                        g->offsets[i] / 2 + CONTENTION_N);
    }
    return NULL;
}

static void test_skip_never_reissues_a_block(void)
{
    wf_claim_t c;
    wf_claim_init(&c, CONTENTION_N);

    static grabber_t g[CONTENTION_THREADS];
    pthread_t th[CONTENTION_THREADS];

    for (int t = 0; t < CONTENTION_THREADS; t++) {
        g[t].claim = &c;
        if (pthread_create(&th[t], NULL, grab_and_skip, &g[t]) != 0) {
            printf("  FAIL could not start thread %d\n", t);
            g_fails++;
            return;
        }
    }
    for (int t = 0; t < CONTENTION_THREADS; t++)
        pthread_join(th[t], NULL);

    /* Skips create holes -- that is their job -- so contiguity is not
     * expected here.  Uniqueness still is. */
    static unsigned char seen2[CONTENTION_THREADS * CONTENTION_CLAIMS * 2];
    const uint64_t cap = sizeof(seen2);
    int duplicates = 0, unaligned = 0;

    for (int t = 0; t < CONTENTION_THREADS; t++) {
        for (int i = 0; i < CONTENTION_CLAIMS; i++) {
            uint64_t off = g[t].offsets[i];
            if (off % CONTENTION_N != 0) { unaligned++; continue; }
            uint64_t idx = off / CONTENTION_N;
            if (idx >= cap) continue;       /* beyond the bitmap, not a fault */
            if (seen2[idx]) duplicates++;
            seen2[idx] = 1;
        }
    }

    CHECK(duplicates == 0);
    CHECK(unaligned == 0);
}

int main(void)
{
    TEST(test_claims_are_disjoint_and_contiguous);
    TEST(test_frame_index_orders_across_workers);
    TEST(test_skip_advances_toward_the_write_pointer);
    TEST(test_skip_never_hands_out_a_future_block);
    TEST(test_skip_needs_two_frames_of_stream);
    TEST(test_abandon_counts_the_reserved_block);
    TEST(test_two_abandoned_blocks_are_both_counted);
    TEST(test_concurrent_skip_keeps_the_furthest);
    TEST(test_redundant_skip_abandons_nothing);
    TEST(test_skip_before_first_frame_is_a_noop);
    TEST(test_skip_lands_on_a_frame_boundary);
    TEST(test_reset_restarts_the_claim);
    TEST(test_reserve_requires_a_written_block);
    TEST(test_reserve_refusal_does_not_advance);
    TEST(test_reset_heals_an_ahead_claim);
    TEST(test_origin_is_latched_once_and_shared);
    TEST(test_origin_zero_is_a_real_value);
    TEST(test_reset_drops_the_origin);
    TEST(test_concurrent_origin_latch_agrees);
    TEST(test_observe_accumulates_across_a_wrap);
    TEST(test_observe_is_idempotent);
    TEST(test_progress_survives_past_39_bits);
    TEST(test_observe_before_origin_is_inert);
    TEST(test_observe_starts_from_the_origin);
    TEST(test_late_joiner_sees_the_accumulated_total);
    TEST(test_concurrent_observe_never_goes_backwards);
    TEST(test_stale_reading_is_not_a_wrap);
    TEST(test_genuine_wrap_is_still_counted);
    TEST(test_lag_budget_scales_with_workers);
    TEST(test_lag_budget_floors_at_one_worker);
    TEST(test_reset_keeps_the_worker_count);
    TEST(test_concurrent_claims_never_collide);
    TEST(test_skip_never_reissues_a_block);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
