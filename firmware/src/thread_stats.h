#pragma once
#include <stdint.h>
#include <stddef.h>

/*
 * Per-thread CPU telemetry, read from /proc/self/task.
 *
 * Exists because the single core is the binding constraint on this
 * platform and nothing measured it.  Earlier TX/RX CPU figures were
 * sampled by hand, from code that was never committed, and could not be
 * reproduced afterwards — so a claim about whether TX and RX fit together
 * had no instrument behind it.
 *
 * Why /proc and not pthread_getcpuclockid():
 *
 *   pthread_getcpuclockid() gives nanosecond resolution, but it needs a
 *   live pthread_t.  Producer threads are created and joined on every mode
 *   switch and every TX, so a registry of pthread_t would have to be
 *   pruned on join and any miss is a use-after-free.  /proc entries simply
 *   vanish when a thread exits.  It also sees threads this daemon did not
 *   create — libiio's, for one — which a registry is blind to by
 *   construction.
 *
 * Why /proc/schedstat is not used:
 *
 *   It would give both ns-resolution runtime and runqueue wait time, but
 *   CONFIG_SCHEDSTATS is compiled out of the Pluto kernel: there is no
 *   /proc/self/schedstat and no kernel.sched_schedstats sysctl.  Jiffies at
 *   100 Hz give 0.2% resolution over a 5 s window, which is ample against
 *   a 38% budget.
 *
 * Counters are cumulative.  The daemon does no averaging: the client takes
 * two samples and differences them, so the measurement window is the
 * caller's choice and no filter state lives on the device.  That is what
 * keeps this a diagnostic and not an input to a control loop.
 *
 * Cost is two small /proc reads per thread, so this is a human-polled
 * endpoint.  Do not poll it at frame rate.
 */

/*
 * Upper bound on threads reported.  The daemon itself runs three at most —
 * the poll loop, one RX producer, one TX thread — so this is headroom for
 * threads created by libraries rather than an expected count.  It is a hard
 * cap on the serialised size, which main.c static-asserts against its
 * response buffer.
 */
#define THREAD_STATS_MAX      16
#define THREAD_STATS_NAME_LEN 16   /* == TASK_COMM_LEN */

typedef struct {
    char     name[THREAD_STATS_NAME_LEN];
    uint32_t tid;
    uint64_t utime;    /* jiffies in user mode */
    uint64_t stime;    /* jiffies in kernel mode */
    uint64_t nvcsw;    /* voluntary context switches */
    uint64_t nivcsw;   /* involuntary — i.e. preempted */
} thread_stat_t;

typedef struct {
    uint64_t wall_us;    /* CLOCK_MONOTONIC, defines the delta window */
    uint64_t cpu_total;  /* every field of /proc/stat's cpu line, summed */
    uint64_t cpu_idle;   /* idle + iowait */
    uint32_t clk_tck;    /* sysconf(_SC_CLK_TCK) */
    uint32_t vm_rss_kb;
    uint16_t n_cpus;     /* ONLINE cores; the ceiling on summed thread % */
    uint16_t n_threads;
    thread_stat_t threads[THREAD_STATS_MAX];
} thread_stats_t;

/* Sample every thread of this process.  Returns 0, or -1 if /proc could
 * not be read at all. */
int thread_stats_sample(thread_stats_t *out);

/*
 * Name the calling thread so telemetry is attributable to a subsystem
 * rather than to a bare tid.  Call it as the first statement of a thread
 * body: naming from the creating thread races a thread that exits
 * immediately, and self-naming cannot.
 *
 * Truncated to 15 characters by the kernel.
 *
 * NEVER call this on the main thread.  It rewrites /proc/<pid>/comm, and
 * `killall shesha-daemon` — which every deploy, run and stop target in the
 * Makefile depends on — matches on exactly that.  Renaming it would make
 * those targets silently stop killing the old daemon, leaving two
 * instances contending for the radio.  The main thread is identifiable
 * anyway: its tid equals the pid, and its comm already reads
 * "shesha-daemon".
 */
void thread_stats_name_self(const char *name);

/* Serialised size for n threads: 36-byte header + 52 bytes each. */
#define THREAD_STATS_HEADER_BYTES 36
#define THREAD_STATS_RECORD_BYTES 52
#define THREAD_STATS_MAX_BYTES \
    (THREAD_STATS_HEADER_BYTES + THREAD_STATS_MAX * THREAD_STATS_RECORD_BYTES)

/* Pack into the RESP_THREAD_STATS payload.  buf must hold at least
 * THREAD_STATS_MAX_BYTES.  Returns bytes written. */
size_t thread_stats_serialize(uint8_t *buf, const thread_stats_t *s);

/*
 * Line parsers, exported only so they can be unit tested on the host.
 *
 * Both are pure string manipulation over formats that are easy to get
 * subtly wrong and whose errors are silent: a misplaced field index yields
 * a plausible number rather than a failure.  The /proc/<tid>/stat case in
 * particular has to survive a comm containing spaces and parentheses,
 * which the daemon's own threads never produce — so only a synthetic test
 * can exercise it.  Return 0 on success, -1 on a malformed line.
 */
int thread_stats_parse_stat_line(char *line, uint64_t *utime, uint64_t *stime);
int thread_stats_parse_cpu_line(char *line, uint64_t *total, uint64_t *idle);
