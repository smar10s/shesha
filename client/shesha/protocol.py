"""Binary TLV protocol — mirrors firmware/src/protocol.h"""
import array
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum


class FrameDecodeError(ValueError):
    """A data frame's declared lengths do not fit its payload."""


class MsgType(IntEnum):
    CMD_SET_FREQ        = 0x01
    CMD_SET_GAIN        = 0x02
    CMD_SET_BW          = 0x03
    CMD_SET_FFT_SIZE    = 0x04
    CMD_SET_WINDOW      = 0x05
    CMD_SET_FPS         = 0x06
    CMD_SET_AGC         = 0x07
    CMD_SET_SAMPLE_RATE = 0x08
    CMD_SET_ACCUM       = 0x09
    CMD_SET_MODE        = 0x0A
    CMD_SET_TX_ATTEN    = 0x0B
    CMD_WIFI_DIAG       = 0x0C
    CMD_TX              = 0x0D
    CMD_TX_STOP         = 0x0E
    CMD_SET_FPGA        = 0x0F
    CMD_TX_STREAM_DATA  = 0x15
    CMD_TX_STREAM_END   = 0x16
    CMD_TX_STATS        = 0x17
    CMD_SET_IQ_BITS     = 0x1C
    CMD_IQ_TX_STREAM_DATA = 0x1D
    CMD_IQ_TX_STREAM_END  = 0x1E
    CMD_SET_WORKERS     = 0x1F
    CMD_SET_RDS         = 0x24
    CMD_TX_STREAM_FLUSH = 0x25
    RESP_OK             = 0x10
    RESP_ERROR          = 0x11
    RESP_STATUS         = 0x12
    RESP_CAPS           = 0x13
    RESP_WIFI_DIAG      = 0x14
    RESP_TX_STATS       = 0x18
    # Backpressure, not failure: the daemon's TX audio ring is bounded, so
    # a client running ahead of realtime is told to retry rather than
    # handed an error it would abandon a healthy stream over.
    RESP_RETRY          = 0x19
    RESP_THREAD_STATS   = 0x1A
    DATA_PSD            = 0x20
    DATA_WIFI           = 0x21
    DATA_FM             = 0x22
    DATA_IQ             = 0x23
    CMD_STATUS          = 0x30
    CMD_CAPS            = 0x31
    CMD_THREAD_STATS    = 0x32
    CMD_SUBSCRIBE       = 0x33


class WindowType(IntEnum):
    RECT            = 0
    HANN            = 1
    BLACKMAN_HARRIS = 2
    FLAT_TOP        = 3


WINDOW_NAMES: dict[int, str] = {
    WindowType.RECT: "rect",
    WindowType.HANN: "hann",
    WindowType.BLACKMAN_HARRIS: "blackman-harris",
    WindowType.FLAT_TOP: "flat-top",
}
WINDOW_CODES: dict[str, WindowType] = {
    name: WindowType(code) for code, name in WINDOW_NAMES.items()
}


class AccumMode(IntEnum):
    NONE = 0
    MAX  = 1
    AVG  = 2


ACCUM_NAMES: dict[int, str] = {
    AccumMode.NONE: "none",
    AccumMode.MAX: "max",
    AccumMode.AVG: "avg",
}
ACCUM_CODES: dict[str, AccumMode] = {
    name: AccumMode(code) for code, name in ACCUM_NAMES.items()
}


class DaemonMode(IntEnum):
    WATERFALL  = 0
    WIFI       = 1
    IQ         = 2
    FM         = 3


# Subscription bitmask bits for CMD_SUBSCRIBE.  A client sends a single byte
# with the OR of the data types it wants.  Default after connect is 0 (nothing).
SUBSCRIBE_PSD  = 1 << 0
SUBSCRIBE_WIFI = 1 << 1
SUBSCRIBE_FM   = 1 << 2
SUBSCRIBE_IQ   = 1 << 3
SUBSCRIBE_ALL  = SUBSCRIBE_PSD | SUBSCRIBE_WIFI | SUBSCRIBE_FM | SUBSCRIBE_IQ


FRAME_HEADER = struct.Struct('<BII')  # type(u8) + length(u32) + seq(u32)

# Mirrors PROTO_MAX_PAYLOAD in firmware/src/protocol.h.  A frame larger
# than this is rejected before it is built, so a client cannot leave the
# daemon reassembling an unbounded payload.
MAX_PAYLOAD = 1 << 20

# The daemon's largest FFT is 8192 bins; anything bigger cannot be a PSD
# frame and would make the client allocate on the sender's word alone.
MAX_FFT_BINS = 8192


@dataclass
class PsdFrame:
    timestamp: float
    center_freq_mhz: float
    db_floor: float
    db_ceil: float
    bins: list[float]


@dataclass(frozen=True)
class RadioPolicy:
    """The active producer's radio constraints, as the firmware enforces them.

    bw_fixed/sr_fixed are 0 when the key is a range.  sr_independent marks
    a mode that takes a user rate; otherwise the rate is derived from bw
    (or fixed outright) and is not an input.  agc_fixed is 0 when the mode
    lets the user choose the gain/AGC mode, or the AGC code the mode pins.
    """
    bw_min: int
    bw_max: int
    bw_fixed: int
    sr_min: int
    sr_max: int
    sr_fixed: int
    sr_independent: bool
    agc_fixed: int

    @property
    def bw_is_user_input(self) -> bool:
        return self.bw_fixed == 0

    @property
    def sr_is_user_input(self) -> bool:
        return self.sr_independent

    @property
    def agc_locked(self) -> bool:
        return self.agc_fixed != 0

    def allows_bw(self, hz: int) -> bool:
        if self.bw_fixed:
            return hz == self.bw_fixed
        return self.bw_min <= hz <= self.bw_max

    def allows_sr(self, hz: int) -> bool:
        if self.sr_fixed:
            return hz == self.sr_fixed
        if self.sr_independent:
            return self.sr_min <= hz <= self.sr_max
        return False


@dataclass
class Status:
    freq_hz: int
    sample_rate_hz: int
    bw_hz: int
    fft_size: int
    gain_db: float
    agc_mode: int
    window_type: int
    target_fps: int
    daemon_mode: int
    tx_atten_db: float
    tx_busy: int
    tx_proto: int
    tx_flags: int
    tx_repeat: int
    tx_interval_us: int
    fpga_active: bool
    rx_active: bool   # is daemon_mode's producer actually running now?
    iq_bits: int      # IQ wire sample width (8 or 12), both directions
    # Fraction of the sample stream that reached an FFT, 0.0-1.0.  Below 1.0
    # the spectrum is gappy in time: some signal was never transformed and a
    # burst landing in a gap is not displayed.  Not a resolution loss -- the
    # frames that are computed are exact.
    psd_coverage: float
    # PSD producer workers actually running (0 while the producer is
    # stopped).  The fs_max clamp and the request both affect this.
    workers: int
    # bw/rate constraints of the producer that is running right now.
    radio: RadioPolicy
    # Lifetime ADC rail components and total components seen.  Deltas between
    # status samples give the clipping rate.
    clip_count: int
    sample_count: int
    # PSD accumulation mode (0=none, 1=max, 2=avg).
    accum: int
    # The configured FPGA producer is actually running right now.
    fpga_running: bool


@dataclass
class Capabilities:
    fpga_build_id: int
    features: int
    max_sample_rate: int
    max_fft_size: int
    min_freq_mhz: int
    max_freq_mhz: int
    fw_build_id: int


@dataclass
class WifiMeta:
    bssid: str          # "aa:bb:cc:dd:ee:ff" or ""
    sa: str             # source address or ""
    da: str             # destination address or ""
    ssid: str           # SSID string or ""
    is_eapol: bool


@dataclass
class WifiIeSummary:
    channel: int            # Primary channel (DS Param or HT Operation)
    security: str           # "open", "wep", "wpa2-psk", "wpa2-eap", "wpa3-sae", "mixed", "wpa1"
    cipher: str             # "none", "wep", "tkip", "ccmp", "gcmp-256"
    phy_flags: int          # raw bitfield
    phy: str                # human-readable: "HE", "VHT-160", "VHT-80", "HT-40", "HT"
    max_rate: float         # Mbps (MCS-derived or legacy fallback)
    n_ss: int               # spatial streams (0=unknown)
    chan_width: int          # 0=20MHz, 1=40, 2=80, 3=160
    center_chan: int         # center freq channel (VHT/HE)
    secondary_offset: int   # 0=none, 1=above, 3=below
    country: str            # "US", "CA", etc. or ""
    pmf: str                # "none", "capable", "required"


@dataclass
class WifiFrame:
    timestamp: float
    rate_code: int
    rate_mbps: int
    fcs_ok: bool
    length: int
    frame_class: int
    frame_class_name: str
    rx_gain: float
    rx_freq_hz: int
    psdu: bytes
    meta: WifiMeta | None
    ie: WifiIeSummary | None


@dataclass
class WifiDiag:
    captures: int
    decode_ok: int
    fcs_ok: int
    fcs_bad: int
    iq_rms: float
    iq_peak: float


@dataclass
class TxStats:
    """Streaming-TX ground truth, straight off the FPGA counters.

    `emitted` is the only trustworthy output-rate measurement: the drain
    FSM advances RD_PTR solely when it hands a sample to the DAC, so
    emitted/elapsed is the real rate and any deficit is exactly the
    time-stretch a receiver hears as the audio dragging.
    """
    tx_active: bool
    fm_active: bool
    end_of_stream: bool
    dac_rate: int
    fed: int              # samples written into the DDR ring
    emitted: int          # samples the DAC has actually output
    elapsed_us: int       # since the stream engine was triggered
    depth: int            # signed DDR depth; negative would be an underrun
    max_depth: int
    high_water: int
    underruns: int
    ring_fill: int        # audio samples buffered in the daemon
    ring_size: int
    audio_rate: int
    audio_consumed: int
    starved_samples: int  # carrier emitted for want of audio
    push_rejects: int
    iq_active: bool       # IQ TX stream running
    iq_ring_fill: int     # packed IQ bytes buffered in the daemon
    iq_push_rejects: int
    iq_starved: int       # times the producer found the IQ ring empty
    dma_start_failed: bool = False  # one-shot TX: DMA engine refused to start

    @property
    def rate_frac(self) -> float:
        """Actual output rate as a fraction of nominal.  1.0 is perfect."""
        if not self.elapsed_us or not self.dac_rate:
            return 0.0
        return self.emitted / (self.elapsed_us * 1e-6) / self.dac_rate

    @property
    def depth_ms(self) -> float:
        return self.depth * 1000.0 / self.dac_rate if self.dac_rate else 0.0

    @property
    def ring_fill_ms(self) -> float:
        return (self.ring_fill * 1000.0 / self.audio_rate
                if self.audio_rate else 0.0)


@dataclass
class RdsState:
    """Decoded RDS station state, straight from the firmware decoder.

    The wire block is fixed-size and sits between the DATA_FM header and
    the audio, so every field has a constant offset. ``ps``/``rt``/``ptyn``
    are decoded from raw bytes; ``*_mask`` says which characters have
    actually been received, because a station rotates its groups.
    """
    synced: bool
    pi_valid: bool
    pi: int
    pty: int
    tp: bool
    ta: bool
    ms: bool
    di: int
    ps: str
    ps_mask: int
    rt: str
    rt_len: int
    rt_mask: int
    rt_ab: bool
    rt_valid: bool
    ptyn: str
    ptyn_mask: int
    ptyn_ab: bool
    af: list[int]
    ct_mjd: int
    ct_minutes: int
    ct_offset_half_hours: int
    ct_valid: bool
    oda: list[tuple[int, int, bytes]]
    groups: int
    blocks: int
    block_errors: int
    sync_losses: int

    @property
    def ps_complete(self) -> bool:
        return self.ps_mask == 0xFF

    @property
    def ptyn_complete(self) -> bool:
        return self.ptyn_mask == 0xFF

    @property
    def pi_text(self) -> str:
        return f'{self.pi:04X}'


@dataclass
class FmFrame:
    timestamp: float
    audio_rate_hz: int
    audio: list[float]
    rds: RdsState | None = None


IQ_BITS_8  = 8
IQ_BITS_12 = 12

_IQ_BYTES_PER_SAMPLE = {IQ_BITS_8: 2, IQ_BITS_12: 3}
MAX_IQ_SAMPLES = 1 << 16


@dataclass
class IqfFrame:
    timestamp: float
    freq_mhz: float
    sample_rate_hz: int
    bits: int
    n_samples: int
    samples: bytes


@dataclass
class ThreadStat:
    """One thread's cumulative counters, straight out of /proc/self/task.

    Cumulative on purpose.  The daemon does no averaging, so the client
    owns the measurement window and two samples can be differenced over
    any interval — which is what keeps this a diagnostic rather than a
    control input.
    """
    name: str
    tid: int
    utime_jiffies: int
    stime_jiffies: int
    voluntary_switches: int
    involuntary_switches: int

    @property
    def cpu_jiffies(self) -> int:
        """Total CPU consumed.  System time counts: a thread blocked in a
        DMA copy is occupying the core just as much as one doing FP math."""
        return self.utime_jiffies + self.stime_jiffies


@dataclass
class ThreadCpu:
    """One thread's share of the core over a measurement window."""
    name: str
    tid: int
    cpu_pct: float
    voluntary_switches: int    # delta over the window, not a total
    involuntary_switches: int


@dataclass
class ThreadStatsDelta:
    """Differenced telemetry — the form a human actually reads.

    `other_pct` exists because per-thread data cannot see outside the
    process.  iiod runs three threads of its own on the same single core,
    and without a system-wide reference its cost is invisible.
    """
    window_sec: float
    threads: list[ThreadCpu]
    system_busy_pct: float
    vm_rss_kb: int
    n_cpus: int      # ceiling on daemon_pct is n_cpus * 100

    @property
    def daemon_pct(self) -> float:
        return sum(t.cpu_pct for t in self.threads)

    @property
    def other_pct(self) -> float:
        """Everything on the core that is not this daemon."""
        return self.system_busy_pct - self.daemon_pct


@dataclass
class ThreadStats:
    wall_us: int
    cpu_total_jiffies: int     # system-wide, all /proc/stat cpu fields
    cpu_idle_jiffies: int      # idle + iowait
    clk_tck: int
    vm_rss_kb: int
    n_cpus: int                # ONLINE cores only
    threads: list[ThreadStat]

    def delta(self, prev: 'ThreadStats') -> ThreadStatsDelta:
        """Difference against an earlier sample.

        Threads are matched by tid, never by name.  Producer threads are
        destroyed and recreated on every mode switch and reuse the same
        name, so name matching would difference one thread's total against
        an unrelated predecessor's and go negative.  A tid absent from
        `prev` started inside the window, so its cumulative counter
        already *is* its delta and baselines at zero correctly.
        """
        window_sec = (self.wall_us - prev.wall_us) / 1e6
        if window_sec <= 0.0:
            return ThreadStatsDelta(0.0, [ThreadCpu(t.name, t.tid, 0.0, 0, 0)
                                          for t in self.threads],
                                    0.0, self.vm_rss_kb, self.n_cpus)

        before = {t.tid: t for t in prev.threads}
        scale = 100.0 / (self.clk_tck * window_sec) if self.clk_tck else 0.0

        threads = []
        for t in self.threads:
            p = before.get(t.tid)
            threads.append(ThreadCpu(
                name=t.name,
                tid=t.tid,
                cpu_pct=(t.cpu_jiffies - (p.cpu_jiffies if p else 0)) * scale,
                voluntary_switches=t.voluntary_switches
                    - (p.voluntary_switches if p else 0),
                involuntary_switches=t.involuntary_switches
                    - (p.involuntary_switches if p else 0),
            ))

        # Scaled by core count so this shares a unit with the per-thread
        # figures, which are each a share of ONE core.  Two cores half busy
        # is one core's worth of work: 100%, not 50%.
        d_total = self.cpu_total_jiffies - prev.cpu_total_jiffies
        d_idle = self.cpu_idle_jiffies - prev.cpu_idle_jiffies
        busy = (100.0 * (d_total - d_idle) / d_total * self.n_cpus
                if d_total > 0 else 0.0)

        return ThreadStatsDelta(window_sec, threads, busy, self.vm_rss_kb,
                                self.n_cpus)



_PSD_HEADER = struct.Struct('<dfffI')  # 8+4+4+4+4 = 24 bytes
_WIFI_HEADER = struct.Struct('<dBBBHBfHQ')  # 8+1+1+1+2+1+4+2+8 = 28 bytes


def parse_psd(payload: bytes) -> PsdFrame:
    if len(payload) < _PSD_HEADER.size:
        raise FrameDecodeError(
            f"psd frame truncated: header needs {_PSD_HEADER.size} bytes, "
            f"got {len(payload)}")
    ts, center, floor_db, ceil_db, n_bins = _PSD_HEADER.unpack_from(payload, 0)
    if n_bins > MAX_FFT_BINS:
        raise FrameDecodeError(
            f"psd bin count {n_bins} exceeds {MAX_FFT_BINS}")
    need = _PSD_HEADER.size + 4 * n_bins
    if need > len(payload):
        raise FrameDecodeError(
            f"psd bins truncated: {len(payload) - _PSD_HEADER.size} of "
            f"{4 * n_bins} bytes")
    bins = list(struct.unpack_from(f'<{n_bins}f', payload, _PSD_HEADER.size))
    return PsdFrame(ts, center, floor_db, ceil_db, bins)


_FRAME_CLASS_NAMES = [
    "ack", "block_ack", "ht_vht", "beacon", "mgmt",
    "probe_req", "probe_resp", "data", "eapol", "deauth", "disassoc",
    "unknown",
]

_SECURITY_NAMES = ["open", "wep", "wpa2-psk", "wpa2-eap", "wpa3-sae", "mixed", "wpa1"]
_CIPHER_NAMES = ["none", "wep", "tkip", "ccmp", "gcmp-256"]
_PMF_NAMES = ["none", "capable", "required"]


def _mac_str(b: bytes) -> str:
    return ':'.join(f'{x:02x}' for x in b)


def _phy_str(flags: int) -> str:
    parts = []
    if flags & 0x20:
        parts.append('HE')
    if flags & 0x10:
        parts.append('VHT-160')
    elif flags & 0x08:
        parts.append('VHT-80')
    elif flags & 0x04:
        parts.append('VHT')
    if flags & 0x02:
        parts.append('HT-40')
    elif flags & 0x01:
        parts.append('HT')
    return '/'.join(parts) if parts else ''


def parse_wifi(payload: bytes) -> WifiFrame:
    if len(payload) < _WIFI_HEADER.size:
        raise FrameDecodeError(
            f"wifi frame truncated: header needs {_WIFI_HEADER.size} bytes, "
            f"got {len(payload)}")
    (ts, rate_code, rate_mbps, fcs, length, fclass, rx_gain, psdu_len,
     rx_freq_hz) = _WIFI_HEADER.unpack_from(payload, 0)
    psdu_end = _WIFI_HEADER.size + psdu_len
    if psdu_end > len(payload):
        raise FrameDecodeError(
            f"wifi psdu truncated: {psdu_len} bytes declared, "
            f"{len(payload) - _WIFI_HEADER.size} available")
    psdu = payload[_WIFI_HEADER.size:psdu_end]

    fclass_name = _FRAME_CLASS_NAMES[fclass] if fclass < len(_FRAME_CLASS_NAMES) else "unknown"

    # Parse meta block (starts after PSDU)
    meta_offset = psdu_end
    if meta_offset + 20 > len(payload):
        raise FrameDecodeError(
            f"wifi meta block truncated: needs 20 bytes, "
            f"got {len(payload) - meta_offset}")

    meta_flags = payload[meta_offset]
    bssid = _mac_str(payload[meta_offset + 1:meta_offset + 7]) if (meta_flags & 0x01) else ""
    sa = _mac_str(payload[meta_offset + 7:meta_offset + 13]) if (meta_flags & 0x02) else ""
    da = _mac_str(payload[meta_offset + 13:meta_offset + 19]) if (meta_flags & 0x04) else ""
    ssid_len = payload[meta_offset + 19]
    ssid_end = meta_offset + 20 + ssid_len
    if ssid_end > len(payload):
        raise FrameDecodeError(
            f"wifi ssid truncated: {ssid_len} bytes declared, "
            f"{len(payload) - (meta_offset + 20)} available")
    ssid = ""
    if (meta_flags & 0x08) and ssid_len > 0:
        ssid = payload[meta_offset + 20:ssid_end].decode('utf-8',
                                                         errors='replace')
    is_eapol = bool(meta_flags & 0x10)
    meta = WifiMeta(bssid=bssid, sa=sa, da=da, ssid=ssid, is_eapol=is_eapol)

    # Parse IE summary block if present
    ie_offset = ssid_end
    ie = None
    if meta_flags & 0x20:
        if ie_offset + 14 > len(payload):
            raise FrameDecodeError(
                f"wifi ie block truncated: needs 14 bytes, "
                f"got {max(0, len(payload) - ie_offset)}")
        ie_data = payload[ie_offset:ie_offset + 14]
        channel = ie_data[0]
        sec_idx = ie_data[1]
        cip_idx = ie_data[2]
        phy_flags = ie_data[3]
        max_rate_mcs = struct.unpack_from('<H', ie_data, 4)[0]  # MCS-derived Mbps
        n_ss = ie_data[6]
        chan_width = ie_data[7]
        center_chan = ie_data[8]
        secondary_offset = ie_data[9]
        country = ie_data[10:12].decode('ascii', errors='replace').rstrip('\x00')
        pmf_idx = ie_data[12]
        legacy_rate_raw = ie_data[13]

        # Use MCS rate if available, otherwise fall back to legacy
        max_rate = float(max_rate_mcs) if max_rate_mcs > 0 else legacy_rate_raw * 0.5

        ie = WifiIeSummary(
            channel=channel,
            security=_SECURITY_NAMES[sec_idx] if sec_idx < len(_SECURITY_NAMES) else "unknown",
            cipher=_CIPHER_NAMES[cip_idx] if cip_idx < len(_CIPHER_NAMES) else "unknown",
            phy_flags=phy_flags,
            phy=_phy_str(phy_flags),
            max_rate=max_rate,
            n_ss=n_ss,
            chan_width=chan_width,
            center_chan=center_chan,
            secondary_offset=secondary_offset,
            country=country,
            pmf=_PMF_NAMES[pmf_idx] if pmf_idx < len(_PMF_NAMES) else "none",
        )

    return WifiFrame(
        timestamp=ts,
        rate_code=rate_code,
        rate_mbps=rate_mbps,
        fcs_ok=bool(fcs),
        length=length,
        frame_class=fclass,
        frame_class_name=fclass_name,
        rx_gain=rx_gain,
        rx_freq_hz=rx_freq_hz,
        psdu=psdu,
        meta=meta,
        ie=ie,
    )


_STATUS_STRUCT = struct.Struct('<QIIIfBBBBfBBBHIBBBHBIIIIIIBBQQBB')  # 91 bytes
_IQ_HEADER = struct.Struct('<dfIBBH')  # 8+4+4+1+1+2 = 20 bytes


def parse_status(payload: bytes) -> Status:
    freq, rate, bw, fft_size, gain, agc, win, fps, dmode, tx_atten, \
        tx_busy, tx_proto, tx_flags, tx_repeat, tx_interval, fpga, \
        rx_active, iq_bits, cover_permille, workers, \
        bw_min, bw_max, bw_fixed, sr_min, sr_max, sr_fixed, sr_indep, agc_fixed, \
        clip_count, sample_count, accum, fpga_running = \
        _STATUS_STRUCT.unpack_from(payload, 0)
    return Status(freq, rate, bw, fft_size, gain, agc, win, fps,
                  dmode, tx_atten, tx_busy, tx_proto, tx_flags,
                  tx_repeat, tx_interval, bool(fpga), bool(rx_active),
                  iq_bits, cover_permille / 1000.0, workers,
                  RadioPolicy(bw_min, bw_max, bw_fixed,
                              sr_min, sr_max, sr_fixed, bool(sr_indep),
                              agc_fixed),
                  clip_count, sample_count, accum, bool(fpga_running))


_CAPS_STRUCT = struct.Struct('<IIIIHHI')  # 4+4+4+4+2+2+4 = 24
_WIFI_DIAG_STRUCT = struct.Struct('<IIIIff')  # 4+4+4+4+4+4 = 24
_FM_HEADER = struct.Struct('<dII')  # 8+4+4 = 16 bytes

# DATA_FM: 16-byte header, fixed 190-byte RDS state, then f32 audio.
# Mirrors PROTO_FM_HEADER_SIZE / PROTO_RDS_WIRE_SIZE in firmware/src/protocol.h
# and is pinned on both sides by test_protocol_fm.
FM_HEADER_SIZE = 16
RDS_WIRE_SIZE = 190

# One FM frame carries a decimated audio chunk, never a whole capture.
MAX_FM_SAMPLES = 1 << 16

# '<' suppresses alignment padding, matching the firmware's byte-wise memcpy.
_RDS_STRUCT = struct.Struct('<BBHB8sBB64sQB8sB25sIHbB44sIIII')


def parse_caps(payload: bytes) -> Capabilities:
    fpga_id, features, max_rate, max_fft, min_freq, max_freq, fw_build_id = \
        _CAPS_STRUCT.unpack_from(payload, 0)
    return Capabilities(fpga_id, features, max_rate, max_fft, min_freq,
                        max_freq, fw_build_id)


def parse_wifi_diag(payload: bytes) -> WifiDiag:
    captures, decode_ok, fcs_ok, fcs_bad, iq_rms, iq_peak = \
        _WIFI_DIAG_STRUCT.unpack_from(payload, 0)
    return WifiDiag(captures, decode_ok, fcs_ok, fcs_bad, iq_rms, iq_peak)


def _rds_text(raw: bytes) -> str:
    """Render an RDS G0 string: printable ASCII kept, other bytes spaced.

    The basic character set is mostly ASCII plus a handful of accents that
    mean little on a terminal; control bytes (including the 0x0D terminator
    that stays in the raw buffer) become spaces rather than escaping the
    line.
    """
    return ''.join(chr(b) if 0x20 <= b <= 0x7E else ' '
                   for b in raw)


def parse_rds_state(payload: bytes) -> RdsState:
    (f0, f1, pi, pty, ps, ps_mask, rt_len, rt, rt_mask, ptyn_mask, ptyn,
     af_count, af, ct_mjd, ct_minutes, ct_off, oda_count,
     oda_raw, groups, blocks, block_errors, sync_losses) = \
        _RDS_STRUCT.unpack_from(payload, FM_HEADER_SIZE)

    oda = []
    for i in range(oda_count):
        entry = oda_raw[i * 11:(i + 1) * 11]
        aid = struct.unpack_from('<H', entry, 0)[0]
        n = entry[2]
        oda.append((aid, n, entry[3:3 + n]))

    return RdsState(
        synced=bool(f0 & 0x01),
        pi_valid=bool(f0 & 0x02),
        pty=pty,
        tp=bool(f0 & 0x40),
        ta=bool(f0 & 0x80),
        ms=bool(f1 & 0x01),
        di=(f1 >> 3) & 0x7,
        pi=pi,
        ps=_rds_text(ps),
        ps_mask=ps_mask,
        rt=_rds_text(rt),
        rt_len=rt_len,
        rt_mask=rt_mask,
        rt_ab=bool(f1 & 0x02),
        rt_valid=bool(f0 & 0x08),
        ptyn=_rds_text(ptyn),
        ptyn_mask=ptyn_mask,
        ptyn_ab=bool(f1 & 0x04),
        af=list(af[:af_count]),
        ct_mjd=ct_mjd,
        ct_minutes=ct_minutes,
        ct_offset_half_hours=ct_off,
        ct_valid=bool(f0 & 0x20),
        oda=oda,
        groups=groups,
        blocks=blocks,
        block_errors=block_errors,
        sync_losses=sync_losses,
    )


def parse_fm(payload: bytes) -> FmFrame:
    minimum = FM_HEADER_SIZE + RDS_WIRE_SIZE
    if len(payload) < minimum:
        raise FrameDecodeError(
            f"fm frame truncated: header and rds need {minimum} bytes, "
            f"got {len(payload)}")
    ts, rate, n_samples = _FM_HEADER.unpack_from(payload, 0)
    if n_samples > MAX_FM_SAMPLES:
        raise FrameDecodeError(
            f"fm sample count {n_samples} exceeds max {MAX_FM_SAMPLES} samples")
    audio_offset = FM_HEADER_SIZE + RDS_WIRE_SIZE
    if audio_offset + 4 * n_samples > len(payload):
        raise FrameDecodeError(
            f"fm audio truncated: {len(payload) - audio_offset} of "
            f"{4 * n_samples} bytes")
    rds = parse_rds_state(payload)
    audio = list(struct.unpack_from(f'<{n_samples}f', payload, audio_offset))
    return FmFrame(ts, rate, audio, rds)


def parse_iq(payload: bytes) -> IqfFrame:
    if len(payload) < _IQ_HEADER.size:
        raise FrameDecodeError(
            f"iq frame truncated: header needs {_IQ_HEADER.size} bytes, "
            f"got {len(payload)}")
    ts, freq, rate, bits, _pad, n = _IQ_HEADER.unpack_from(payload, 0)
    if bits not in _IQ_BYTES_PER_SAMPLE:
        raise FrameDecodeError(f"iq frame has unknown sample width {bits}")
    if n > MAX_IQ_SAMPLES:
        raise FrameDecodeError(
            f"iq sample count {n} exceeds max {MAX_IQ_SAMPLES}")
    expect = n * _IQ_BYTES_PER_SAMPLE[bits]
    if len(payload) < _IQ_HEADER.size + expect:
        raise FrameDecodeError(
            f"iq samples truncated: {len(payload) - _IQ_HEADER.size} of "
            f"{expect} bytes")
    return IqfFrame(ts, freq, rate, bits, n,
                    bytes(payload[_IQ_HEADER.size:_IQ_HEADER.size + expect]))


def unpack_iq(samples: bytes, bits: int) -> list[complex]:
    """Decode packed wire samples to complex values in [-1, 1).

    Mirrors firmware/src/iq_pack.c: I is the first component, each is a
    signed 12-bit value (8-bit carries the top 8 bits).
    """
    out: list[complex] = []
    if bits == IQ_BITS_8:
        for i in range(0, len(samples) - 1, 2):
            re = struct.unpack('<b', samples[i:i + 1])[0]
            im = struct.unpack('<b', samples[i + 1:i + 2])[0]
            out.append(complex(re / 128.0, im / 128.0))
    elif bits == IQ_BITS_12:
        for i in range(0, len(samples) - 2, 3):
            v = samples[i] | (samples[i + 1] << 8) | (samples[i + 2] << 16)
            re = v & 0xFFF
            if re & 0x800:
                re -= 0x1000
            im = (v >> 12) & 0xFFF
            if im & 0x800:
                im -= 0x1000
            out.append(complex(re / 2048.0, im / 2048.0))
    else:
        raise ValueError(f'unknown iq bits {bits}')
    return out


def pack_iq(points: list[complex], bits: int) -> bytes:
    """Encode complex values in [-1, 1) to the packed wire format.

    The inverse of ``unpack_iq``.  Used to synthesize frames offline; the
    daemon never packs.
    """
    out = bytearray()
    if bits == IQ_BITS_8:
        for z in points:
            re = max(-128, min(127, int(round(z.real * 128.0))))
            im = max(-128, min(127, int(round(z.imag * 128.0))))
            out += struct.pack('<bb', re, im)
    elif bits == IQ_BITS_12:
        for z in points:
            re = max(-2048, min(2047, int(round(z.real * 2048.0)))) & 0xFFF
            im = max(-2048, min(2047, int(round(z.imag * 2048.0)))) & 0xFFF
            v = re | (im << 12)
            out += bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))
    else:
        raise ValueError(f'unknown iq bits {bits}')
    return bytes(out)


# 5 x u64, 4 x u32, 1 x i32, 4 x u32, 4 x u8 = 40 + 36 + 4 = 80 bytes,
# plus 3 x u32 + 4 x u8 = 16 bytes of IQ stream fields: 96 bytes total.
# The trailing four u8 are iq_active, dma_start_failed, and two reserved
# bytes.
# '<' keeps struct from inserting alignment padding, matching the daemon's
# byte-wise memcpy.  depth (index 6) is int32_t — negative means underrun.
_TX_STATS_STRUCT = struct.Struct('<5QIi3I4I4B3I4B')    # 96 bytes


def parse_tx_stats(payload: bytes) -> TxStats:
    (fed, emitted, elapsed_us, audio_consumed, starved,
     dac_rate, depth, max_depth, high_water, underruns,
     ring_fill, ring_size, audio_rate, push_rejects,
     tx_active, fm_active, eos, _pad,
     iq_ring_fill, iq_push_rejects, iq_starved, iq_active,
     dma_start_failed, _pad3, _pad4) = \
        _TX_STATS_STRUCT.unpack_from(payload, 0)
    return TxStats(
        tx_active=bool(tx_active), fm_active=bool(fm_active),
        end_of_stream=bool(eos), dac_rate=dac_rate, fed=fed,
        emitted=emitted, elapsed_us=elapsed_us, depth=depth,
        max_depth=max_depth, high_water=high_water, underruns=underruns,
        ring_fill=ring_fill, ring_size=ring_size, audio_rate=audio_rate,
        audio_consumed=audio_consumed, starved_samples=starved,
        push_rejects=push_rejects,
        iq_active=bool(iq_active), iq_ring_fill=iq_ring_fill,
        iq_push_rejects=iq_push_rejects, iq_starved=iq_starved,
        dma_start_failed=bool(dma_start_failed))


# 34-byte header, then a 52-byte record per thread.  '<' suppresses
# alignment padding to match the daemon's byte-wise memcpy.
_THREAD_STATS_HEADER = struct.Struct('<QQQIIHH')  # 8+8+8+4+4+2+2 = 36
_THREAD_STAT_RECORD = struct.Struct('<16sIQQQQ')  # 16+4+32       = 52
_THREAD_NAME_LEN = 16


def parse_thread_stats(payload: bytes) -> ThreadStats:
    if len(payload) < _THREAD_STATS_HEADER.size:
        raise ValueError(
            f'thread stats header truncated: {len(payload)} bytes, '
            f'need {_THREAD_STATS_HEADER.size}')

    wall_us, cpu_total, cpu_idle, clk_tck, vm_rss_kb, n_cpus, n_threads = \
        _THREAD_STATS_HEADER.unpack_from(payload, 0)

    expect = _THREAD_STATS_HEADER.size + n_threads * _THREAD_STAT_RECORD.size
    if len(payload) < expect:
        raise ValueError(
            f'thread stats truncated: {len(payload)} bytes for '
            f'{n_threads} threads, need {expect}')

    threads = []
    off = _THREAD_STATS_HEADER.size
    for _ in range(n_threads):
        name, tid, utime, stime, nvcsw, nivcsw = \
            _THREAD_STAT_RECORD.unpack_from(payload, off)
        threads.append(ThreadStat(
            name=name.split(b'\x00', 1)[0].decode('ascii', 'replace'),
            tid=tid,
            utime_jiffies=utime,
            stime_jiffies=stime,
            voluntary_switches=nvcsw,
            involuntary_switches=nivcsw,
        ))
        off += _THREAD_STAT_RECORD.size

    return ThreadStats(wall_us, cpu_total, cpu_idle, clk_tck, vm_rss_kb,
                       n_cpus, threads)


def build_frame(msg_type: MsgType, payload: bytes = b'') -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(
            f"payload of {len(payload)} bytes exceeds MAX_PAYLOAD "
            f"({MAX_PAYLOAD})")
    return FRAME_HEADER.pack(msg_type, len(payload), 0) + payload


def cmd_set_freq(freq_hz: int) -> bytes:
    return build_frame(MsgType.CMD_SET_FREQ, struct.pack('<Q', freq_hz))


def cmd_set_gain(gain_db: float) -> bytes:
    return build_frame(MsgType.CMD_SET_GAIN, struct.pack('<f', gain_db))


def cmd_set_bw(bw_hz: int) -> bytes:
    return build_frame(MsgType.CMD_SET_BW, struct.pack('<I', bw_hz))


def cmd_set_fft_size(n: int) -> bytes:
    return build_frame(MsgType.CMD_SET_FFT_SIZE, struct.pack('<I', n))


def cmd_set_window(win: WindowType) -> bytes:
    return build_frame(MsgType.CMD_SET_WINDOW, struct.pack('<B', win))


def cmd_set_fps(fps: int) -> bytes:
    return build_frame(MsgType.CMD_SET_FPS, struct.pack('<B', fps))


def cmd_set_agc(mode: int) -> bytes:
    return build_frame(MsgType.CMD_SET_AGC, struct.pack('<B', mode))


def cmd_set_sample_rate(rate_hz: int) -> bytes:
    return build_frame(MsgType.CMD_SET_SAMPLE_RATE, struct.pack('<I', rate_hz))


def cmd_set_tx_atten(db: float) -> bytes:
    return build_frame(MsgType.CMD_SET_TX_ATTEN, struct.pack('<f', db))


def cmd_set_accum(mode: AccumMode) -> bytes:
    return build_frame(MsgType.CMD_SET_ACCUM, struct.pack('<B', mode))


def cmd_set_workers(n: int) -> bytes:
    return build_frame(MsgType.CMD_SET_WORKERS, struct.pack('<B', n))


def cmd_set_mode(mode: DaemonMode) -> bytes:
    return build_frame(MsgType.CMD_SET_MODE, struct.pack('<B', mode))


def cmd_set_fpga(enable: bool) -> bytes:
    return build_frame(MsgType.CMD_SET_FPGA, struct.pack('<B', 1 if enable else 0))


TX_PROTO_WIFI = 0
TX_PROTO_LORA = 1
TX_PROTO_IQ = 2
TX_PROTO_FM = 3
TX_PROTO_NONE = 0xFE
TX_STOP_ALL = 0xFF


class TxProtocol(IntEnum):
    WIFI = TX_PROTO_WIFI
    LORA = TX_PROTO_LORA
    IQ   = TX_PROTO_IQ
    FM   = TX_PROTO_FM


class TxFlags(IntEnum):
    NONE   = 0x00
    CYCLIC = 0x01
    STREAM = 0x02


TX_REPEAT_FOREVER = 0xFFFF

TX_PROTO_NAMES: dict[int, str] = {
    TX_PROTO_WIFI: 'wifi',
    TX_PROTO_LORA: 'lora',
    TX_PROTO_IQ: 'iq',
    TX_PROTO_FM: 'fm',
    TX_PROTO_NONE: 'none',
}

# Fixed RDS transmit block appended to the FM stream-start sub-payload and
# carried alone by CMD_SET_RDS.  Layout pinned by
# firmware/src/protocol.h (PROTO_RDS_TX_SIZE) and test_protocol_fm on both
# ends.
PROTO_RDS_TX_SIZE = 76
_RDS_TX_STRUCT = struct.Struct('<BHB8s64s')  # flags, pi, pty, ps, rt

# The FM stream-start command carries one second of audio, so this rate
# keeps that first chunk inside one payload.  cmd_tx adds 8 bytes, the FM
# header adds 16, and the RDS block adds PROTO_RDS_TX_SIZE.
_FM_START_OVERHEAD = 8 + 16 + PROTO_RDS_TX_SIZE
MAX_FM_AUDIO_RATE_HZ = (MAX_PAYLOAD - _FM_START_OVERHEAD) // 4


@dataclass
class RdsTxConfig:
    """Firmware RDS transmit fields (groups 0A + 2A)."""
    enabled: bool = False
    tp: bool = False
    pi: int = 0
    pty: int = 0
    ps: str = ' ' * 8
    rt: str = ' ' * 64


def pack_f32le(samples: Sequence[float]) -> bytes:
    """Pack float samples little-endian regardless of host byte order."""
    data = array.array('f', samples)
    if sys.byteorder != 'little':
        data.byteswap()
    return data.tobytes()


def _pack_rds_block(rds: RdsTxConfig | None) -> bytes:
    if rds is None:
        return b'\x00' * PROTO_RDS_TX_SIZE
    flags = (1 if rds.enabled else 0) | (2 if rds.tp else 0)
    ps = rds.ps.encode('ascii', 'replace')[:8].ljust(8, b' ')
    rt = rds.rt.encode('ascii', 'replace')[:64].ljust(64, b' ')
    return _RDS_TX_STRUCT.pack(flags, rds.pi & 0xFFFF,
                               rds.pty & 0x1F, ps, rt)


def cmd_tx(protocol: TxProtocol, flags: TxFlags,
           repeat: int, interval_us: int,
           sub_payload: bytes) -> bytes:
    payload = struct.pack('<BBHI', protocol, flags, repeat, interval_us) + sub_payload
    return build_frame(MsgType.CMD_TX, payload)


def cmd_tx_stop(target: int = TX_STOP_ALL) -> bytes:
    return build_frame(MsgType.CMD_TX_STOP, struct.pack('<B', target))


def cmd_tx_wifi(rate_mbps: int, psdu: bytes,
                repeat: int = 1, interval_us: int = 0,
                cyclic: bool = False) -> bytes:
    flags = TxFlags.CYCLIC if cyclic else TxFlags.NONE
    sub = struct.pack('<BH', rate_mbps, len(psdu)) + psdu
    return cmd_tx(TxProtocol.WIFI, flags, repeat, interval_us, sub)


def cmd_tx_fm(audio: list[float], *,
              audio_rate: int = 44100,
              deviation: int = 75000,
              dac_rate: int = 2_100_000,
              rds: RdsTxConfig | None = None) -> bytes:
    sub = struct.pack('<IIII', audio_rate, deviation, dac_rate, len(audio))
    sub += _pack_rds_block(rds)
    sub += pack_f32le(audio)
    return cmd_tx(TxProtocol.FM, TxFlags.NONE, 1, 0, sub)


def cmd_tx_fm_stream_start(audio: Sequence[float], *,
                           audio_rate: int = 44100,
                           deviation: int = 75000,
                           dac_rate: int = 2_100_000,
                           rds: RdsTxConfig | None = None) -> bytes:
    sub = struct.pack('<IIII', audio_rate, deviation, dac_rate, len(audio))
    sub += _pack_rds_block(rds)
    sub += pack_f32le(audio)
    return cmd_tx(TxProtocol.FM, TxFlags.STREAM, 1, 0, sub)


def cmd_set_rds(rds: RdsTxConfig) -> bytes:
    return build_frame(MsgType.CMD_SET_RDS, _pack_rds_block(rds))


def cmd_tx_stream_data(audio: Sequence[float]) -> bytes:
    payload = struct.pack('<I', len(audio))
    payload += pack_f32le(audio)
    return build_frame(MsgType.CMD_TX_STREAM_DATA, payload)


def cmd_tx_stream_end() -> bytes:
    return build_frame(MsgType.CMD_TX_STREAM_END)


def cmd_tx_stream_flush() -> bytes:
    """Discard an FM stream's queued (not yet modulated) audio."""
    return build_frame(MsgType.CMD_TX_STREAM_FLUSH)


def cmd_set_iq_bits(bits: int) -> bytes:
    return build_frame(MsgType.CMD_SET_IQ_BITS, struct.pack('<B', bits))


def cmd_iq_tx_stream_data(data: bytes) -> bytes:
    payload = struct.pack('<I', len(data)) + data
    return build_frame(MsgType.CMD_IQ_TX_STREAM_DATA, payload)


def cmd_iq_tx_stream_end() -> bytes:
    return build_frame(MsgType.CMD_IQ_TX_STREAM_END)


def cmd_tx_stats() -> bytes:
    return build_frame(MsgType.CMD_TX_STATS)


def cmd_thread_stats() -> bytes:
    return build_frame(MsgType.CMD_THREAD_STATS)


def cmd_status() -> bytes:
    return build_frame(MsgType.CMD_STATUS)


def cmd_caps() -> bytes:
    return build_frame(MsgType.CMD_CAPS)


def cmd_wifi_diag() -> bytes:
    return build_frame(MsgType.CMD_WIFI_DIAG)


def cmd_subscribe(mask: int = SUBSCRIBE_ALL) -> bytes:
    """Subscribe to data streams.  mask is OR of SUBSCRIBE_* bits."""
    return build_frame(MsgType.CMD_SUBSCRIBE, struct.pack('<B', mask & 0xFF))
