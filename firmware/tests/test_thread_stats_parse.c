/*
 * Host unit tests for the /proc line parsers in thread_stats.c.
 *
 * These run on the build host, not the Pluto: the formats are pure string
 * manipulation, and the cases that matter most are ones the device will
 * never produce.  A thread whose comm contains a space or a parenthesis
 * breaks naive tokenisation of /proc/<tid>/stat, and the daemon's own
 * thread names never contain either — so the device cannot exercise it.
 *
 * Build and run:  make test-firmware
 */
#include "../src/thread_stats.h"

#include <stdio.h>
#include <string.h>

static int failures;

#define CHECK(cond) do {                                                  \
    if (!(cond)) {                                                        \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
        failures++;                                                       \
    }                                                                     \
} while (0)

#define RUN(fn) do { printf("%s\n", #fn); fn(); } while (0)

/* A real /proc/<tid>/stat line.  utime is field 14, stime field 15. */
static void test_stat_line_plain_comm(void)
{
    char line[] = "795 (cat) R 650 649 649 0 -1 4194304 65 0 0 0 "
                  "111 222 0 0 20 0 1 0 32899 2252800 62";
    uint64_t utime = 0, stime = 0;

    CHECK(thread_stats_parse_stat_line(line, &utime, &stime) == 0);
    CHECK(utime == 111);
    CHECK(stime == 222);
}

static void test_stat_line_comm_containing_spaces(void)
{
    char line[] = "42 (my thread name) S 1 1 1 0 -1 4194304 0 0 0 0 "
                  "7 9 0 0 20 0 3 0 100 200 5";
    uint64_t utime = 0, stime = 0;

    CHECK(thread_stats_parse_stat_line(line, &utime, &stime) == 0);
    CHECK(utime == 7);
    CHECK(stime == 9);
}

static void test_stat_line_comm_containing_parens(void)
{
    /* The kernel does not escape comm, so the only reliable anchor is the
     * LAST ')' in the line. */
    char line[] = "42 (weird):) S 1 1 1 0 -1 4194304 0 0 0 0 "
                  "31 41 0 0 20 0 3 0 100 200 5";
    uint64_t utime = 0, stime = 0;

    CHECK(thread_stats_parse_stat_line(line, &utime, &stime) == 0);
    CHECK(utime == 31);
    CHECK(stime == 41);
}

static void test_stat_line_zero_times(void)
{
    char line[] = "1 (init) S 0 1 1 0 -1 4194560 100 200 0 0 "
                  "0 0 0 0 20 0 1 0 5 1000 300";
    uint64_t utime = 99, stime = 99;

    CHECK(thread_stats_parse_stat_line(line, &utime, &stime) == 0);
    CHECK(utime == 0);
    CHECK(stime == 0);
}

static void test_stat_line_rejects_missing_paren(void)
{
    char line[] = "795 cat R 650 649 649 0 -1 4194304 65 0 0 0 1 2";
    uint64_t utime = 0, stime = 0;

    CHECK(thread_stats_parse_stat_line(line, &utime, &stime) == -1);
}

static void test_stat_line_rejects_truncated_line(void)
{
    char line[] = "795 (cat) R 650 649 649";
    uint64_t utime = 0, stime = 0;

    CHECK(thread_stats_parse_stat_line(line, &utime, &stime) == -1);
}

/* /proc/stat per-core line: "cpuN user nice system idle iowait irq ..." */
static void test_cpu_line_sums_all_fields(void)
{
    char line[] = "cpu0 100 20 30 500 40 5 5 0 0 0\n";
    uint64_t total = 0, idle = 0;

    CHECK(thread_stats_parse_cpu_line(line, &total, &idle) == 0);
    CHECK(total == 700);
}

static void test_cpu_line_idle_is_idle_plus_iowait(void)
{
    char line[] = "cpu0 100 20 30 500 40 5 5 0 0 0\n";
    uint64_t total = 0, idle = 0;

    CHECK(thread_stats_parse_cpu_line(line, &total, &idle) == 0);
    CHECK(idle == 540);   /* 500 idle + 40 iowait */
}

static void test_cpu_line_handles_short_field_list(void)
{
    /* Older kernels expose fewer columns; missing ones are simply absent. */
    char line[] = "cpu0 10 0 5 85\n";
    uint64_t total = 0, idle = 0;

    CHECK(thread_stats_parse_cpu_line(line, &total, &idle) == 0);
    CHECK(total == 100);
    CHECK(idle == 85);
}

static void test_cpu_line_accepts_second_core(void)
{
    char line[] = "cpu1 1 2 3 4\n";
    uint64_t total = 0, idle = 0;

    CHECK(thread_stats_parse_cpu_line(line, &total, &idle) == 0);
    CHECK(total == 10);
    CHECK(idle == 4);
}

static void test_cpu_line_accepts_double_digit_core(void)
{
    char line[] = "cpu12 1 2 3 4\n";
    uint64_t total = 0, idle = 0;

    CHECK(thread_stats_parse_cpu_line(line, &total, &idle) == 0);
    CHECK(total == 10);
}

static void test_cpu_line_rejects_the_aggregate(void)
{
    /*
     * The bare "cpu" line sums for_each_possible_cpu(), which on this
     * platform includes CPU1 — present but held offline by maxcpus=1.  Its
     * idle accounting for an offline core is not predictable: measured
     * advancing at 200 jiffies/s shortly after boot against a real 100 Hz.
     * Per-core lines are emitted only for ONLINE cpus, so summing those
     * counts exactly the cores that can run work.
     */
    char line[] = "cpu  100 20 30 500 40 5 5 0 0 0\n";
    uint64_t total = 0, idle = 0;

    CHECK(thread_stats_parse_cpu_line(line, &total, &idle) == -1);
}

static void test_cpu_line_rejects_non_cpu_line(void)
{
    char line[] = "intr 123 456 789\n";
    uint64_t total = 0, idle = 0;

    CHECK(thread_stats_parse_cpu_line(line, &total, &idle) == -1);
}

static void test_cpu_line_rejects_lookalike_key(void)
{
    char line[] = "cpufreq 1 2 3 4\n";
    uint64_t total = 0, idle = 0;

    CHECK(thread_stats_parse_cpu_line(line, &total, &idle) == -1);
}

int main(void)
{
    RUN(test_stat_line_plain_comm);
    RUN(test_stat_line_comm_containing_spaces);
    RUN(test_stat_line_comm_containing_parens);
    RUN(test_stat_line_zero_times);
    RUN(test_stat_line_rejects_missing_paren);
    RUN(test_stat_line_rejects_truncated_line);
    RUN(test_cpu_line_sums_all_fields);
    RUN(test_cpu_line_idle_is_idle_plus_iowait);
    RUN(test_cpu_line_handles_short_field_list);
    RUN(test_cpu_line_accepts_second_core);
    RUN(test_cpu_line_accepts_double_digit_core);
    RUN(test_cpu_line_rejects_the_aggregate);
    RUN(test_cpu_line_rejects_non_cpu_line);
    RUN(test_cpu_line_rejects_lookalike_key);

    if (failures) {
        printf("\n%d check(s) FAILED\n", failures);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
