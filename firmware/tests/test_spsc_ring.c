/* test_spsc_ring.c — host tests for the unified SPSC ring buffer.
 *
 * Validates: basic push/pop, lap-tear detection via sequence numbers,
 * and pop_all batch semantics.
 */
#include "../src/spsc_ring.h"

#include <stdio.h>
#include <string.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* --- basic creation --- */

static void test_create_destroy(void)
{
    spsc_ring_t *r = spsc_ring_create(sizeof(int), 8);
    CHECK(r != NULL);
    CHECK(r->slot_size == sizeof(int));
    CHECK(r->capacity == 8);
    CHECK(spsc_ring_available(r) == 0);
    spsc_ring_destroy(r);
}

/* --- pop_all returns all unread --- */

static void test_pop_all_basic(void)
{
    spsc_ring_t *r = spsc_ring_create(sizeof(int), 8);
    for (int i = 10; i < 15; i++)
        spsc_ring_push(r, &i);

    int out[8];
    size_t start_seq = 999;
    int n = spsc_ring_pop_all(r, out, 8, &start_seq);
    CHECK(n == 5);
    CHECK(start_seq == 0);
    CHECK(out[0] == 10);
    CHECK(out[4] == 14);
    CHECK(spsc_ring_available(r) == 0);
    spsc_ring_destroy(r);
}

/* --- pop_all with max_items limit --- */

static void test_pop_all_capped(void)
{
    spsc_ring_t *r = spsc_ring_create(sizeof(int), 16);
    for (int i = 0; i < 10; i++)
        spsc_ring_push(r, &i);

    int out[4];
    size_t start_seq = 0;
    int n = spsc_ring_pop_all(r, out, 4, &start_seq);
    CHECK(n == 4);
    /* Takes most recent 4: values 6,7,8,9 at seq 6,7,8,9 */
    CHECK(start_seq == 6);
    CHECK(out[0] == 6);
    CHECK(out[3] == 9);
    spsc_ring_destroy(r);
}

/* --- lap detection: producer overwrites unread slots --- */

static void test_lap_detection(void)
{
    spsc_ring_t *r = spsc_ring_create(sizeof(int), 4);

    /* Push 4 items (fills ring) */
    for (int i = 0; i < 4; i++)
        spsc_ring_push(r, &i);

    /* Push 3 more — overwrites slots 0,1,2 */
    for (int i = 10; i < 13; i++)
        spsc_ring_push(r, &i);

    /* Consumer was at read_seq=0, write_seq=7.  7-0=7 > capacity=4,
     * so oldest available is seq 3 (7-4). */
    int out[8];
    size_t start_seq = 0;
    int n = spsc_ring_pop_all(r, out, 8, &start_seq);
    CHECK(n == 4);       /* only capacity items available */
    CHECK(start_seq == 3); /* gap: consumer lost seq 0,1,2 */
    CHECK(out[0] == 3);   /* value at seq 3 (original push i=3) */
    CHECK(out[1] == 10);  /* seq 4 */
    CHECK(out[2] == 11);  /* seq 5 */
    CHECK(out[3] == 12);  /* seq 6 */
    spsc_ring_destroy(r);
}

/* --- empty ring returns 0 --- */

static void test_empty_ring(void)
{
    spsc_ring_t *r = spsc_ring_create(sizeof(int), 4);
    int out[4];
    size_t seq = 0;
    CHECK(spsc_ring_pop_all(r, out, 4, &seq) == 0);
    spsc_ring_destroy(r);
}

/* --- sequential read: no gap after normal consumption --- */

static void test_sequential_reads_no_gap(void)
{
    spsc_ring_t *r = spsc_ring_create(sizeof(int), 8);
    int val, out;
    size_t seq;

    /* Push, pop, push, pop — interleaved */
    val = 100; spsc_ring_push(r, &val);
    CHECK(spsc_ring_pop_all(r, &out, 1, &seq) == 1);
    CHECK(out == 100 && seq == 0);

    val = 200; spsc_ring_push(r, &val);
    val = 300; spsc_ring_push(r, &val);
    int batch[8];
    size_t start;
    int n = spsc_ring_pop_all(r, batch, 8, &start);
    CHECK(n == 2);
    CHECK(start == 1);  /* seq 1 and 2 */
    CHECK(batch[0] == 200);
    CHECK(batch[1] == 300);

    spsc_ring_destroy(r);
}

/* --- large slot (struct) --- */

typedef struct {
    float data[1024];
    uint32_t n;
    uint32_t rate;
} big_slot_t;

static void test_large_slot(void)
{
    spsc_ring_t *r = spsc_ring_create(sizeof(big_slot_t), 4);
    CHECK(r != NULL);

    big_slot_t in = { .n = 512, .rate = 44100 };
    in.data[0] = 1.5f;
    in.data[511] = -0.5f;
    spsc_ring_push(r, &in);

    big_slot_t out;
    size_t seq;
    CHECK(spsc_ring_pop_all(r, &out, 1, &seq) == 1);
    CHECK(out.n == 512);
    CHECK(out.rate == 44100);
    CHECK(out.data[0] == 1.5f);
    CHECK(out.data[511] == -0.5f);
    spsc_ring_destroy(r);
}

/* --- sequence numbers are monotonic across many pushes --- */

static void test_sequence_monotonic(void)
{
    spsc_ring_t *r = spsc_ring_create(sizeof(int), 4);
    for (int i = 0; i < 1000; i++) {
        size_t seq = spsc_ring_push(r, &i);
        CHECK(seq == (size_t)i);
    }
    /* After 1000 pushes, only last 4 are readable */
    int out[4];
    size_t start;
    int n = spsc_ring_pop_all(r, out, 4, &start);
    CHECK(n == 4);
    CHECK(start == 996);
    CHECK(out[0] == 996);
    CHECK(out[3] == 999);
    spsc_ring_destroy(r);
}

int main(void)
{
    TEST(test_create_destroy);
    TEST(test_pop_all_basic);
    TEST(test_pop_all_capped);
    TEST(test_lap_detection);
    TEST(test_empty_ring);
    TEST(test_sequential_reads_no_gap);
    TEST(test_large_slot);
    TEST(test_sequence_monotonic);

    printf("\n%s (%d failures)\n", g_fails ? "FAILED" : "all checks passed",
           g_fails);
    return g_fails ? 1 : 0;
}
