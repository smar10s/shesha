"""Tests for per-thread CPU telemetry (RESP_THREAD_STATS).

The wire format is built byte-by-byte in the firmware, so these tests
reconstruct that exact byte order rather than reusing the client's own
struct string.  A parser built from the same format string it is tested
against cannot catch a field-order mistake — which is precisely how
_TX_STATS_STRUCT ended up with its signed slot on the wrong field.
"""
import struct

import pytest

from shesha.protocol import MsgType, ThreadStats, parse_thread_stats


CLK_TCK = 100


def _thread_blob(name: str, tid: int, utime: int, stime: int,
                 nvcsw: int, nivcsw: int) -> bytes:
    """One 52-byte per-thread record, in firmware field order."""
    blob = name.encode()[:15].ljust(16, b'\x00')
    blob += struct.pack('<I', tid)
    blob += struct.pack('<QQQQ', utime, stime, nvcsw, nivcsw)
    assert len(blob) == 52
    return blob


def _payload(wall_us: int, cpu_total: int, cpu_idle: int, vm_rss_kb: int,
             threads: list[bytes], clk_tck: int = CLK_TCK,
             n_cpus: int = 1) -> bytes:
    """Full RESP_THREAD_STATS payload: 36-byte header + N records."""
    head = struct.pack('<QQQ', wall_us, cpu_total, cpu_idle)
    head += struct.pack('<II', clk_tck, vm_rss_kb)
    head += struct.pack('<HH', n_cpus, len(threads))
    assert len(head) == 36
    return head + b''.join(threads)


def test_command_ids_do_not_collide():
    """New message types must not reuse an existing id."""
    assert MsgType.CMD_THREAD_STATS == 0x32
    assert MsgType.RESP_THREAD_STATS == 0x1A
    ids = [m.value for m in MsgType]
    assert len(ids) == len(set(ids)), "duplicate MsgType value"


def test_parse_header_fields():
    """Every header field lands in the right attribute."""
    payload = _payload(wall_us=1_234_567, cpu_total=90_000, cpu_idle=30_000,
                       vm_rss_kb=4096, threads=[])

    ts = parse_thread_stats(payload)

    assert ts.wall_us == 1_234_567
    assert ts.cpu_total_jiffies == 90_000
    assert ts.cpu_idle_jiffies == 30_000
    assert ts.clk_tck == 100
    assert ts.vm_rss_kb == 4096
    assert ts.n_cpus == 1
    assert ts.threads == []


def test_parse_thread_records():
    """Per-thread records parse in order with every field distinct."""
    payload = _payload(
        wall_us=0, cpu_total=0, cpu_idle=0, vm_rss_kb=0,
        threads=[
            _thread_blob('shesha-poll', 101, 11, 12, 13, 14),
            _thread_blob('tx-stream', 202, 21, 22, 23, 24),
        ],
    )

    ts = parse_thread_stats(payload)

    assert len(ts.threads) == 2
    a, b = ts.threads

    assert a.name == 'shesha-poll'
    assert a.tid == 101
    assert a.utime_jiffies == 11
    assert a.stime_jiffies == 12
    assert a.voluntary_switches == 13
    assert a.involuntary_switches == 14

    assert b.name == 'tx-stream'
    assert b.tid == 202
    assert b.utime_jiffies == 21
    assert b.stime_jiffies == 22
    assert b.voluntary_switches == 23
    assert b.involuntary_switches == 24


def test_thread_name_strips_nul_padding():
    """comm is a fixed 16-byte field; the name must come back unpadded."""
    payload = _payload(0, 0, 0, 0, [_thread_blob('fm-rx', 1, 0, 0, 0, 0)])

    ts = parse_thread_stats(payload)

    assert ts.threads[0].name == 'fm-rx'


def test_thread_name_uses_full_15_chars():
    """A name filling comm entirely must not be truncated by the parser."""
    name = 'abcdefghijklmno'  # 15 chars, the TASK_COMM_LEN maximum
    payload = _payload(0, 0, 0, 0, [_thread_blob(name, 1, 0, 0, 0, 0)])

    ts = parse_thread_stats(payload)

    assert ts.threads[0].name == name


def test_cpu_jiffies_sums_user_and_system():
    """A thread's CPU cost is user plus system time, not just user."""
    payload = _payload(0, 0, 0, 0, [_thread_blob('w', 1, 30, 12, 0, 0)])

    ts = parse_thread_stats(payload)

    assert ts.threads[0].cpu_jiffies == 42


def test_delta_computes_per_thread_cpu_percent():
    """60 jiffies over a 1 s window at 100 Hz is 60% of one core."""
    prev = parse_thread_stats(
        _payload(1_000_000, 0, 0, 0, [_thread_blob('tx-stream', 7, 0, 0, 0, 0)]))
    curr = parse_thread_stats(
        _payload(2_000_000, 0, 0, 0, [_thread_blob('tx-stream', 7, 50, 10, 0, 0)]))

    d = curr.delta(prev)

    assert d.window_sec == pytest.approx(1.0)
    assert len(d.threads) == 1
    assert d.threads[0].name == 'tx-stream'
    assert d.threads[0].cpu_pct == pytest.approx(60.0)


def test_delta_scales_by_window_length():
    """The same jiffy delta over 5 s is a fifth of the percentage."""
    prev = parse_thread_stats(
        _payload(0, 0, 0, 0, [_thread_blob('w', 1, 0, 0, 0, 0)]))
    curr = parse_thread_stats(
        _payload(5_000_000, 0, 0, 0, [_thread_blob('w', 1, 60, 0, 0, 0)]))

    d = curr.delta(prev)

    assert d.window_sec == pytest.approx(5.0)
    assert d.threads[0].cpu_pct == pytest.approx(12.0)


def test_delta_reports_context_switch_deltas_not_totals():
    """Preemption is a rate; cumulative counters would hide a change."""
    prev = parse_thread_stats(
        _payload(0, 0, 0, 0, [_thread_blob('w', 1, 0, 0, 1000, 2000)]))
    curr = parse_thread_stats(
        _payload(1_000_000, 0, 0, 0, [_thread_blob('w', 1, 0, 0, 1005, 2050)]))

    d = curr.delta(prev)

    assert d.threads[0].voluntary_switches == 5
    assert d.threads[0].involuntary_switches == 50


def test_delta_daemon_pct_sums_threads():
    """The daemon's own share is the sum over its threads."""
    prev = parse_thread_stats(_payload(0, 0, 0, 0, [
        _thread_blob('a', 1, 0, 0, 0, 0),
        _thread_blob('b', 2, 0, 0, 0, 0),
    ]))
    curr = parse_thread_stats(_payload(1_000_000, 0, 0, 0, [
        _thread_blob('a', 1, 60, 0, 0, 0),
        _thread_blob('b', 2, 38, 0, 0, 0),
    ]))

    d = curr.delta(prev)

    assert d.daemon_pct == pytest.approx(98.0)


def test_delta_system_busy_excludes_idle():
    """System busy is non-idle jiffies over total, from /proc/stat."""
    prev = parse_thread_stats(_payload(0, 1000, 800, 0, []))
    curr = parse_thread_stats(_payload(1_000_000, 1100, 830, 0, []))

    d = curr.delta(prev)

    # 100 total jiffies elapsed, 30 of them idle -> 70% busy
    assert d.system_busy_pct == pytest.approx(70.0)


def test_delta_other_pct_reveals_processes_outside_the_daemon():
    """iiod and the kernel are invisible in per-thread data alone."""
    prev = parse_thread_stats(_payload(0, 1000, 800, 0, [
        _thread_blob('a', 1, 0, 0, 0, 0),
    ]))
    curr = parse_thread_stats(_payload(1_000_000, 1100, 830, 0, [
        _thread_blob('a', 1, 50, 0, 0, 0),
    ]))

    d = curr.delta(prev)

    assert d.system_busy_pct == pytest.approx(70.0)
    assert d.daemon_pct == pytest.approx(50.0)
    assert d.other_pct == pytest.approx(20.0)


def test_delta_counts_full_cost_of_a_thread_started_mid_window():
    """A thread absent from the previous sample began inside the window,
    so its cumulative counter already is its delta."""
    prev = parse_thread_stats(_payload(0, 0, 0, 0, [
        _thread_blob('poll', 1, 0, 0, 0, 0),
    ]))
    curr = parse_thread_stats(_payload(1_000_000, 0, 0, 0, [
        _thread_blob('poll', 1, 5, 0, 0, 0),
        _thread_blob('tx-stream', 9, 60, 0, 0, 0),
    ]))

    d = curr.delta(prev)

    by_name = {t.name: t for t in d.threads}
    assert by_name['tx-stream'].cpu_pct == pytest.approx(60.0)
    assert by_name['poll'].cpu_pct == pytest.approx(5.0)


def test_delta_omits_threads_that_exited():
    """An exited producer has no current counter to difference against."""
    prev = parse_thread_stats(_payload(0, 0, 0, 0, [
        _thread_blob('poll', 1, 0, 0, 0, 0),
        _thread_blob('waterfall', 2, 0, 0, 0, 0),
    ]))
    curr = parse_thread_stats(_payload(1_000_000, 0, 0, 0, [
        _thread_blob('poll', 1, 10, 0, 0, 0),
    ]))

    d = curr.delta(prev)

    assert [t.name for t in d.threads] == ['poll']


def test_delta_matches_threads_by_tid_not_by_name():
    """Producer threads are recreated on every mode switch and can share a
    name; only the tid identifies a lifetime."""
    prev = parse_thread_stats(_payload(0, 0, 0, 0, [
        _thread_blob('fm-rx', 100, 500, 0, 0, 0),
    ]))
    curr = parse_thread_stats(_payload(1_000_000, 0, 0, 0, [
        _thread_blob('fm-rx', 200, 40, 0, 0, 0),
    ]))

    d = curr.delta(prev)

    # tid 200 is a new thread, so 40 jiffies is its whole cost — not
    # 40 - 500, which would go negative against the old thread's total.
    assert d.threads[0].tid == 200
    assert d.threads[0].cpu_pct == pytest.approx(40.0)


def test_delta_over_zero_window_does_not_divide_by_zero():
    """Two samples from the same poll must not raise."""
    prev = parse_thread_stats(_payload(5_000, 100, 50, 0, [
        _thread_blob('w', 1, 3, 0, 0, 0),
    ]))
    curr = parse_thread_stats(_payload(5_000, 100, 50, 0, [
        _thread_blob('w', 1, 3, 0, 0, 0),
    ]))

    d = curr.delta(prev)

    assert d.window_sec == 0.0
    assert d.threads[0].cpu_pct == 0.0
    assert d.system_busy_pct == 0.0
    assert d.other_pct == 0.0


def test_parse_rejects_truncated_thread_record():
    """A short frame must fail loudly rather than yield silent garbage."""
    payload = _payload(0, 0, 0, 0, [_thread_blob('w', 1, 0, 0, 0, 0)])

    with pytest.raises(ValueError):
        parse_thread_stats(payload[:-8])


def test_parse_rejects_truncated_header():
    payload = _payload(0, 0, 0, 0, [])

    with pytest.raises(ValueError):
        parse_thread_stats(payload[:20])


def test_system_busy_is_a_share_of_one_core_scaled_by_core_count():
    """Per-thread percentages are shares of ONE core, so system busy must
    use the same unit or the two cannot be compared.  Two cores each half
    busy is one core's worth of work: 100%, not 50%."""
    prev = parse_thread_stats(_payload(0, 1000, 500, 0, [], n_cpus=2))
    curr = parse_thread_stats(_payload(1_000_000, 1400, 700, 0, [], n_cpus=2))

    d = curr.delta(prev)

    # 400 total jiffies elapsed across 2 cores, 200 idle -> 50% of capacity
    assert d.n_cpus == 2
    assert d.system_busy_pct == pytest.approx(100.0)


def test_system_busy_on_one_core_is_unscaled():
    prev = parse_thread_stats(_payload(0, 1000, 800, 0, [], n_cpus=1))
    curr = parse_thread_stats(_payload(1_000_000, 1100, 830, 0, [], n_cpus=1))

    d = curr.delta(prev)

    assert d.n_cpus == 1
    assert d.system_busy_pct == pytest.approx(70.0)


def test_daemon_pct_can_exceed_100_only_with_multiple_cores():
    """The sum over threads is bounded by n_cpus * 100, and the reader needs
    n_cpus to know which ceiling applies."""
    prev = parse_thread_stats(_payload(0, 0, 0, 0, [
        _thread_blob('a', 1, 0, 0, 0, 0),
        _thread_blob('b', 2, 0, 0, 0, 0),
    ], n_cpus=2))
    curr = parse_thread_stats(_payload(1_000_000, 0, 0, 0, [
        _thread_blob('a', 1, 95, 0, 0, 0),
        _thread_blob('b', 2, 90, 0, 0, 0),
    ], n_cpus=2))

    d = curr.delta(prev)

    assert d.daemon_pct == pytest.approx(185.0)
    assert d.n_cpus == 2
