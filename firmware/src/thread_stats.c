#include "thread_stats.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif

void thread_stats_name_self(const char *name)
{
#ifdef __linux__
    /* prctl rather than pthread_setname_np: the latter is a GNU extension
     * needing _GNU_SOURCE, which this build does not define. */
    prctl(PR_SET_NAME, name, 0, 0, 0);
#else
    (void)name;   /* host unit-test build */
#endif
}

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)ts.tv_nsec / 1000ULL;
}

/*
 * utime and stime are fields 14 and 15 of /proc/<tid>/stat, but field 2 is
 * comm — an arbitrary string in parentheses that may itself contain spaces
 * and parentheses.  Tokenising from the start of the line is therefore
 * unsafe.  Seek to the LAST ')' instead: everything after it is field 3
 * onward, unambiguously, so utime is the 12th token past it.
 *
 * Mutates `line` (strtok_r).
 */
int thread_stats_parse_stat_line(char *line, uint64_t *utime, uint64_t *stime)
{
    char *p = strrchr(line, ')');
    if (!p)
        return -1;

    char *save = NULL;
    char *tok = strtok_r(p + 1, " ", &save);   /* field 3 */

    /* Advance to field 13, leaving 14 and 15 next. */
    for (int field = 4; field <= 13 && tok; field++)
        tok = strtok_r(NULL, " ", &save);
    if (!tok)
        return -1;

    tok = strtok_r(NULL, " ", &save);          /* field 14 */
    if (!tok)
        return -1;
    *utime = strtoull(tok, NULL, 10);

    tok = strtok_r(NULL, " ", &save);          /* field 15 */
    if (!tok)
        return -1;
    *stime = strtoull(tok, NULL, 10);

    return 0;
}

/*
 * One per-core line of /proc/stat: "cpuN <user> <nice> <system> <idle>
 * <iowait> <irq> <softirq> <steal> ...".  Summing every field gives the
 * wall-clock jiffy count for that core, which is the reference the daemon's
 * own threads are measured against — without it, CPU burned by other
 * processes (iiod runs three threads of its own) is invisible.
 *
 * Only per-core "cpuN" lines are accepted; the bare "cpu" aggregate is
 * rejected.  The aggregate sums for_each_possible_cpu(), which on this
 * platform includes both cores.  The kernel emits per-core lines only for
 * ONLINE cpus, so summing those counts exactly the cores that can run work.
 * With dual-core enabled (required for shesha), both cpu0 and cpu1 lines
 * are present and accounted for.
 *
 * Mutates `line` (strtok_r).
 */
int thread_stats_parse_cpu_line(char *line, uint64_t *total, uint64_t *idle)
{
    if (strncmp(line, "cpu", 3) != 0)
        return -1;
    /* Require at least one digit, so "cpu " and "cpufreq" are both out. */
    if (line[3] < '0' || line[3] > '9')
        return -1;

    const char *p = line + 3;
    while (*p >= '0' && *p <= '9')
        p++;
    if (*p != ' ' && *p != '\t')
        return -1;

    *total = 0;
    *idle = 0;

    char *save = NULL;
    char *tok = strtok_r((char *)p, " \t\n", &save);
    if (!tok)
        return -1;

    for (int i = 0; tok; i++) {
        uint64_t v = strtoull(tok, NULL, 10);
        *total += v;
        if (i == 3 || i == 4)   /* idle, iowait */
            *idle += v;
        tok = strtok_r(NULL, " \t\n", &save);
    }
    return 0;
}

static int read_task_times(const char *tid, uint64_t *utime, uint64_t *stime)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/self/task/%s/stat", tid);

    FILE *f = fopen(path, "r");
    if (!f)
        return -1;

    char line[512];
    char *got = fgets(line, sizeof(line), f);
    fclose(f);
    if (!got)
        return -1;

    return thread_stats_parse_stat_line(line, utime, stime);
}

/* Name and both context-switch counters all live in /proc/<tid>/status, so
 * one pass over it covers everything /proc/<tid>/stat does not. */
static int read_task_status(const char *tid, thread_stat_t *t)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/self/task/%s/status", tid);

    FILE *f = fopen(path, "r");
    if (!f)
        return -1;

    char line[256];
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "Name:", 5) == 0) {
            char *v = line + 5;
            while (*v == ' ' || *v == '\t')
                v++;
            size_t n = strcspn(v, "\n");
            if (n >= THREAD_STATS_NAME_LEN)
                n = THREAD_STATS_NAME_LEN - 1;
            memcpy(t->name, v, n);
            t->name[n] = '\0';
        } else if (strncmp(line, "voluntary_ctxt_switches:", 24) == 0) {
            t->nvcsw = strtoull(line + 24, NULL, 10);
        } else if (strncmp(line, "nonvoluntary_ctxt_switches:", 27) == 0) {
            t->nivcsw = strtoull(line + 27, NULL, 10);
        }
    }
    fclose(f);
    return 0;
}

static uint32_t read_vm_rss_kb(void)
{
    FILE *f = fopen("/proc/self/status", "r");
    if (!f)
        return 0;

    char line[256];
    uint32_t kb = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "VmRSS:", 6) == 0) {
            kb = (uint32_t)strtoul(line + 6, NULL, 10);
            break;
        }
    }
    fclose(f);
    return kb;
}

/* Sum every ONLINE core's line in /proc/stat, and count them.  The core
 * count matters to the reader: per-thread percentages are shares of one
 * core, so the ceiling on their sum is n_cpus * 100. */
static int read_cpu_totals(uint64_t *total, uint64_t *idle, uint16_t *n_cpus)
{
    FILE *f = fopen("/proc/stat", "r");
    if (!f)
        return -1;

    *total = 0;
    *idle = 0;
    *n_cpus = 0;

    char line[512];
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "cpu", 3) != 0)
            break;   /* cpu lines are contiguous and come first */
        uint64_t t = 0, i = 0;
        if (thread_stats_parse_cpu_line(line, &t, &i) != 0)
            continue;   /* the bare aggregate line */
        *total += t;
        *idle += i;
        (*n_cpus)++;
    }
    fclose(f);

    return *n_cpus > 0 ? 0 : -1;
}

int thread_stats_sample(thread_stats_t *out)
{
    memset(out, 0, sizeof(*out));

    out->wall_us = now_us();
    long tck = sysconf(_SC_CLK_TCK);
    out->clk_tck = (uint32_t)(tck > 0 ? tck : 100);
    out->vm_rss_kb = read_vm_rss_kb();
    read_cpu_totals(&out->cpu_total, &out->cpu_idle, &out->n_cpus);

    DIR *d = opendir("/proc/self/task");
    if (!d)
        return -1;

    struct dirent *e;
    while ((e = readdir(d)) && out->n_threads < THREAD_STATS_MAX) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9')
            continue;

        thread_stat_t *t = &out->threads[out->n_threads];
        memset(t, 0, sizeof(*t));
        t->tid = (uint32_t)strtoul(e->d_name, NULL, 10);

        /* A thread can exit between readdir() and the reads below; that is
         * normal, not an error, so it is simply skipped. */
        if (read_task_times(e->d_name, &t->utime, &t->stime) != 0)
            continue;
        read_task_status(e->d_name, t);

        out->n_threads++;
    }
    closedir(d);

    return 0;
}

static void put_u16(uint8_t *b, uint16_t v) { memcpy(b, &v, 2); }
static void put_u32(uint8_t *b, uint32_t v) { memcpy(b, &v, 4); }
static void put_u64(uint8_t *b, uint64_t v) { memcpy(b, &v, 8); }

size_t thread_stats_serialize(uint8_t *buf, const thread_stats_t *s)
{
    put_u64(buf +  0, s->wall_us);
    put_u64(buf +  8, s->cpu_total);
    put_u64(buf + 16, s->cpu_idle);
    put_u32(buf + 24, s->clk_tck);
    put_u32(buf + 28, s->vm_rss_kb);
    put_u16(buf + 32, s->n_cpus);
    put_u16(buf + 34, s->n_threads);

    size_t off = THREAD_STATS_HEADER_BYTES;
    for (uint16_t i = 0; i < s->n_threads; i++) {
        const thread_stat_t *t = &s->threads[i];
        memset(buf + off, 0, THREAD_STATS_NAME_LEN);
        memcpy(buf + off, t->name, strnlen(t->name, THREAD_STATS_NAME_LEN - 1));
        put_u32(buf + off + 16, t->tid);
        put_u64(buf + off + 20, t->utime);
        put_u64(buf + off + 28, t->stime);
        put_u64(buf + off + 36, t->nvcsw);
        put_u64(buf + off + 44, t->nivcsw);
        off += THREAD_STATS_RECORD_BYTES;
    }
    return off;
}
