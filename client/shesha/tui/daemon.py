"""Daemon abstraction: ABC, Daemon base class, SheshaDaemon.

The daemon is the source of truth.  Client state is populated only from
RESP_STATUS — never from client intent.  apply() puts bytes on the wire
and emits a transient `_sent` notification for UI feedback (pulse
animations); the hardware answers with an unsolicited RESP_STATUS push
(the firmware sends one to the issuing client after every successful
state mutation), which is what updates the displayed values.

Config is a write-only store: it seeds a freshly started daemon
(freq/gain/bw/fft + the saved app's mode) and is otherwise never pushed
into a running daemon.  Reconnecting adopts whatever the daemon has.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Callable
import queue
import struct
import subprocess
import threading
import time

from ..client import DaemonClient as _DaemonClient
from ..protocol import (
    MsgType,
    SUBSCRIBE_ALL,
    RadioPolicy,
    parse_psd, parse_wifi, parse_status, parse_fm, parse_tx_stats,
    parse_caps, parse_iq,
    cmd_set_freq, cmd_set_gain, cmd_set_bw,
    cmd_set_fft_size, cmd_set_window, cmd_set_fps,
    cmd_set_agc, cmd_set_sample_rate, cmd_set_accum, cmd_set_workers,
    cmd_set_mode, cmd_set_fpga, cmd_set_tx_atten, cmd_tx_wifi, cmd_tx_stop,
    cmd_set_iq_bits, cmd_status, cmd_caps, cmd_subscribe, cmd_tx_stats,
    cmd_tx_fm_stream_start, cmd_tx_stream_data, cmd_tx_stream_end,
    cmd_tx_stream_flush, cmd_set_rds,
)
from .fmt import parse_freq

# ─── AGC mode constants ──────────────────────────────────────────────────

AGC_MANUAL = 0
AGC_FAST = 1
AGC_SLOW = 2

_AGC_MODE_NAMES = {AGC_MANUAL: 'manual', AGC_FAST: 'fast', AGC_SLOW: 'slow'}


class Daemon(ABC):
    """Abstract radio daemon interface.

    Accepts key-value config changes and notifies listeners on state
    transitions.  get_state() returns only daemon-confirmed values
    (RESP_STATUS payloads).
    """

    _RADIO_CONFIG_MAP: dict[str, str] = {
        'freq':                      'freq',
        'gain':                      'gain',
        'bw':                        'bw',
        'sample_rate':               'sample_rate',
        'fps':                       'fps',
        'tx_gain':                   'tx_gain',
        'workers':                   'workers',
        'viz.waterfall.fft_size':    'fft_size',
        'viz.waterfall.window':      'window',
        'viz.waterfall.accum':       'accum',
    }

    @classmethod
    def radio_config_keys(cls) -> list[str]:
        """Config keys the daemon accepts, in seed order.

        Derived from ``_RADIO_CONFIG_MAP`` so a new mapping is enough.
        """
        return list(cls._RADIO_CONFIG_MAP)

    def __init__(self) -> None:
        self._callbacks: list[Callable[[str], None]] = []
        self._state: dict[str, str] = {}
        self._last_sent: list[str] = []
        self._last_acked: str | None = None
        self._last_error: str = ''
        self._host_display: str = ''
        self.connected = False
        self._ui_thread: int | None = None
        self._pending_notifications: queue.SimpleQueue[str] = queue.SimpleQueue()
        # Name of the daemon/connection operation in flight, or None when
        # idle.  The UI's only two questions are "is a link up?" and "is
        # something happening?"; everything else is noise.  See docs/tui.md.
        self._activity: str | None = None

    @abstractmethod
    def apply(self, key: str, value: str) -> None:
        """Send a config change to hardware."""

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def host_display(self) -> str:
        """Endpoint the daemon talks to, ``host:port``."""
        return self._host_display

    @property
    def last_sent(self) -> list[str]:
        """Keys most recently sent (for pulse feedback)."""
        return list(self._last_sent)

    @property
    def last_acked(self) -> str | None:
        """Pulse key settled by the most recent command ack/error."""
        return self._last_acked

    def get_state(self) -> dict[str, str]:
        return dict(self._state)

    def radio_policy(self) -> RadioPolicy | None:
        """Active producer's bw/rate policy, or None until a status arrives."""
        return None

    def apply_config(self, key: str, value: str, client=None) -> None:
        """Public entry point for config-key changes (e.g. `:set freq ...`)."""
        base_key = self._RADIO_CONFIG_MAP.get(key)
        if base_key is not None:
            if client is None:
                self.apply(base_key, value)
            else:
                self.apply(base_key, value, client)

    def _sent(self, *keys: str) -> None:
        self._last_sent = list(keys)
        self._notify('_sent')

    def fileno(self) -> int | None:
        return None

    def poll(self) -> list[dict]:
        return []

    def on_change(self, callback: Callable[[str], None]) -> None:
        self._callbacks.append(callback)

    def check_alive(self, timeout: float = 5.0) -> bool:
        """Return True if connection is healthy. Close and return False if stale."""
        return True

    def wifi_tx(self, rate_mbps: int, psdu: bytes,
                repeat: int = 1, interval_us: int = 0,
                cyclic: bool = False) -> None:
        """Send a TX command. Override in subclasses."""

    def tx_stop(self) -> None:
        """Stop any active TX. Override in subclasses."""

    def fm_tx_stream_start(self, audio: list[float], *,
                           audio_rate: int = 44100,
                           deviation: int = 75_000,
                           dac_rate: int = 2_100_000,
                           rds=None) -> bool:
        """Begin streaming FM TX with an initial audio chunk."""
        return False

    def set_rds(self, rds) -> bool:
        """Update the RDS fields of an active FM stream."""
        return False

    def fm_tx_stream_push(self, audio: list[float]) -> 'PushResult':
        """Push the next chunk of audio into an active FM TX stream."""
        from ..client import PushResult
        return PushResult.ERROR

    def fm_tx_stream_end(self) -> bool:
        """Signal end-of-stream for FM TX."""
        return False

    def fm_tx_stream_flush(self) -> bool:
        """Discard the queued, not-yet-modulated audio of an FM stream."""
        return False

    def tx_stats(self) -> 'TxStats | None':
        """Query current TX telemetry from the daemon."""
        return None

    def set_iq_bits(self, bits: int) -> bool:
        """Set the IQ wire sample width (8 or 12). Override in subclasses."""
        return False

    @property
    def busy(self) -> bool:
        """True while a daemon/connection operation is in flight."""
        return self._activity is not None

    @property
    def activity(self) -> str | None:
        """Label of the in-flight operation, or None (diagnostics only)."""
        return self._activity

    @contextmanager
    def _doing(self, what: str):
        """Mark a daemon/connection operation as in flight for its duration.

        Wrap the *whole* operation — including pre-flight SSH probes — so
        the glyph flashes from the first moment of work rather than sitting
        at rest.  Nested calls restore the outer label, so an operation can
        delegate to another (smart_connect -> sync -> stop) without the
        inner completion clearing the outer activity.
        """
        prev = self._activity
        self._activity = what
        self._notify('connection')
        try:
            yield
        finally:
            self._activity = prev
            self._notify('connection')

    def set_ui_thread(self, ident: int | None = None) -> None:
        """Name the thread that owns UI callbacks."""
        self._ui_thread = threading.get_ident() if ident is None else ident

    def _notify(self, key: str) -> None:
        """Queue a change for delivery on the UI thread.

        Everything goes through one queue once a UI thread is set.  Keeping
        command and response notifications in order matters: a reply that
        lands before the next frame must not settle a pulse that has not
        started yet.
        """
        if self._ui_thread is None:
            self._deliver(key)
        else:
            self._pending_notifications.put(key)

    def _deliver(self, key: str) -> None:
        for cb in self._callbacks:
            cb(key)

    def drain_notifications(self) -> None:
        """Deliver queued changes. Call from the UI thread."""
        while True:
            try:
                key = self._pending_notifications.get_nowait()
            except queue.Empty:
                return
            self._deliver(key)

    def _set_connected(self, value: bool) -> None:
        """Publish a link up/down transition."""
        if value == self.connected:
            return
        self.connected = value
        self._notify('connection')


# Map config key -> protocol command builder
_CMD_BUILDERS: dict[str, Callable] = {
    'freq': lambda v: cmd_set_freq(int(v)),
    'gain': lambda v: cmd_set_gain(float(v)),
    'bw': lambda v: cmd_set_bw(int(v)),
    'fft_size': lambda v: cmd_set_fft_size(int(v)),
    'sample_rate': lambda v: cmd_set_sample_rate(int(v)),
    'fps': lambda v: cmd_set_fps(int(v)),
    'window': lambda v: cmd_set_window(int(v)),
    'accum': lambda v: cmd_set_accum(int(v)),
    'tx_gain': lambda v: cmd_set_tx_atten(float(v)),
    'workers': lambda v: cmd_set_workers(int(v)),
}

# Command ack/error -> pulse key to settle.  Purely animation feedback:
# the pulse started when the command was sent must stop when the daemon
# answers, even if the confirmed state value didn't change (e.g. an AGC
# switch leaves gain_db alone, so no 'gain' notify ever fires).
_ACK_SETTLE: dict[int, str] = {
    MsgType.CMD_SET_FREQ: 'freq',
    MsgType.CMD_SET_GAIN: 'gain',
    MsgType.CMD_SET_AGC: 'gain',
    MsgType.CMD_SET_BW: 'bw',
}

class SheshaDaemon(Daemon):
    """Connects to PlutoSDR running shesha-daemon over TCP.

    Lifecycle managed via make targets (deploy, run, stop).
    Wraps DaemonClient for protocol framing.  State is only ever taken
    from RESP_STATUS (requested at connect, pushed by the firmware after
    mutations, and polled via heartbeat ping).
    """

    def __init__(self, config, host: str = '192.168.2.1', port: int = 5555,
                 project_dir: str | None = None, *,
                 fresh_start: bool = False,
                 desired_mode: str | None = None) -> None:
        super().__init__()
        self._config = config
        self._host = host
        self._port = port
        self._host_display = f'{host}:{port}'
        self._project_dir = project_dir
        self._client: _DaemonClient | None = None
        self._fresh_start = fresh_start       # daemon was launched by us
        self._desired_mode = desired_mode     # saved app mode, pre-connect
        self._radio_policy: RadioPolicy | None = None  # active producer's rules
        self._tx_stats = None           # cached RESP_TX_STATS
        self._tx_push_backpressure = False  # set by RESP_RETRY, cleared by push
        self._pending_mutations = 0     # commands sent, not yet answered

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def fresh_start(self) -> bool:
        return self._fresh_start

    @property
    def desired_mode(self) -> str | None:
        return self._desired_mode

    def _sent(self, *keys: str) -> None:
        if self._client is not None:
            self._pending_mutations += 1
        super()._sent(*keys)

    def _command_answered(self) -> None:
        if self._pending_mutations:
            self._pending_mutations -= 1

    def apply(self, key: str, value: str, client=None) -> None:
        if key == 'freq':
            try:
                freq_hz = parse_freq(value)
            except (ValueError, TypeError):
                return
            self._sent('freq')
            self._send(cmd_set_freq(freq_hz), client)
            return
        if key == 'bw':
            try:
                bw_hz = parse_freq(value)
            except (ValueError, TypeError):
                return
            self._sent('bw')
            self._send(cmd_set_bw(bw_hz), client)
            return
        if key == 'sample_rate':
            try:
                rate_hz = parse_freq(value)
            except (ValueError, TypeError):
                return
            self._sent('sample_rate')
            self._send(cmd_set_sample_rate(rate_hz), client)
            return
        if key == 'gain':
            if value in ('auto', 'fast', 'fast_attack'):
                self._sent('gain', 'agc_mode')
                self._send(cmd_set_agc(AGC_FAST), client)
                return
            if value in ('slow', 'slow_attack'):
                self._sent('gain', 'agc_mode')
                self._send(cmd_set_agc(AGC_SLOW), client)
                return
            try:
                gain_db = float(value)
            except (ValueError, TypeError):
                return
            self._sent('gain', 'agc_mode')
            self._send(cmd_set_agc(AGC_MANUAL), client)
            self._send(cmd_set_gain(gain_db), client)
            return
        builder = _CMD_BUILDERS.get(key)
        if not builder:
            return
        self._sent(key)
        self._send(builder(value), client)

    def _send(self, data: bytes, client=None) -> None:
        """Send raw bytes via the client. Close on error."""
        target = client or self._client
        if not target:
            return
        try:
            target.send_raw(data)
        except (OSError, ConnectionError):
            if client is not None:
                target.disconnect()
            else:
                self._close()

    def _active_client(self) -> _DaemonClient | None:
        """Return the connected client, or None. Bind once per operation."""
        client = self._client
        if not client or not client.connected:
            return None
        return client

    def fileno(self) -> int | None:
        client = self._active_client()
        if not client:
            return None
        try:
            return client.fileno()
        except ValueError:
            return None

    def poll(self) -> list[dict]:
        client = self._client
        if not client or not client.connected:
            return []
        try:
            raw_frames = client.recv_raw(timeout=0)
        except (OSError, ConnectionError):
            self._close()
            return []
        if not client.connected:
            # recv_raw disconnected us (EOF or error)
            self._close()
            return []
        frames: list[dict] = []
        for msg_type, payload in raw_frames:
            self._dispatch(msg_type, payload, frames)
        return frames

    def _dispatch(self, msg_type: int, payload: bytes, frames: list) -> None:
        if msg_type == MsgType.DATA_PSD:
            psd = parse_psd(payload)
            frames.append({
                'ts': psd.timestamp,
                'center_freq_mhz': psd.center_freq_mhz,
                'db_floor': psd.db_floor,
                'db_ceil': psd.db_ceil,
                'bins': psd.bins,
                'type': 'psd',
            })
        elif msg_type == MsgType.DATA_WIFI:
            wf = parse_wifi(payload)
            frames.append({
                'ts': wf.timestamp,
                'rate_code': wf.rate_code,
                'rate_mbps': wf.rate_mbps,
                'fcs_ok': wf.fcs_ok,
                'length': wf.length,
                'frame_class': wf.frame_class,
                'frame_class_name': wf.frame_class_name,
                'rx_gain': wf.rx_gain,
                'rx_freq_hz': wf.rx_freq_hz,
                'psdu': wf.psdu,
                'meta': wf.meta,
                'ie': wf.ie,
                'type': 'wifi',
            })
        elif msg_type == MsgType.DATA_FM:
            fm = parse_fm(payload)
            frames.append({
                'ts': fm.timestamp,
                'audio_rate': fm.audio_rate_hz,
                'audio': fm.audio,
                'rds': fm.rds,
                'type': 'fm',
            })
        elif msg_type == MsgType.DATA_IQ:
            iq = parse_iq(payload)
            frames.append({
                'ts': iq.timestamp,
                'freq_mhz': iq.freq_mhz,
                'sample_rate_hz': iq.sample_rate_hz,
                'bits': iq.bits,
                'n_samples': iq.n_samples,
                'samples': iq.samples,
                'type': 'iq',
            })
        elif msg_type == MsgType.RESP_OK:
            # Bare ack.  The firmware pushes an unsolicited RESP_STATUS to
            # the issuer after every successful mutation — that push, not
            # the ack, is what updates state.  The ack does settle the
            # send pulse (see _ACK_SETTLE).
            self._command_answered()
            if payload:
                settle_key = _ACK_SETTLE.get(payload[0])
                if settle_key:
                    self._last_acked = settle_key
                    self._notify('_acked')
        elif msg_type == MsgType.RESP_ERROR:
            self._command_answered()
            err_msg = ''
            if len(payload) > 1:
                err_msg = payload[1:].decode('ascii', errors='replace')
            self._last_error = err_msg
            if payload:
                settle_key = _ACK_SETTLE.get(payload[0])
                if settle_key:
                    self._last_acked = settle_key
                    self._notify('_acked')
            self._notify('_error')
        elif msg_type == MsgType.RESP_STATUS:
            status = parse_status(payload)
            self._radio_policy = status.radio
            old_state = self._state
            state = dict(old_state)
            state['freq'] = str(status.freq_hz)
            state['bw'] = str(status.bw_hz)
            state['gain'] = str(status.gain_db)
            state['fft_size'] = str(status.fft_size)
            state['agc_mode'] = _AGC_MODE_NAMES.get(status.agc_mode, 'fast')
            state['iq_bits'] = str(status.iq_bits)
            state['tx_atten'] = str(status.tx_atten_db)
            state['mode'] = str(status.daemon_mode)
            state['target_fps'] = str(status.target_fps)
            state['psd_coverage'] = str(status.psd_coverage)
            state['workers'] = str(status.workers)
            state['clip_count'] = str(status.clip_count)
            state['sample_count'] = str(status.sample_count)
            state['fpga_active'] = '1' if status.fpga_active else '0'
            prev_busy = old_state.get('tx_busy', '0')
            old_sample_rate = old_state.get('sample_rate')
            old_bw = old_state.get('bw')
            state['tx_busy'] = '1' if status.tx_busy else '0'
            state['tx_proto'] = str(status.tx_proto)
            state['tx_flags'] = str(status.tx_flags)
            state['tx_repeat'] = str(status.tx_repeat)
            state['tx_interval_us'] = str(status.tx_interval_us)
            new_sample_rate = str(status.sample_rate_hz)
            state['sample_rate'] = new_sample_rate
            self._state = state
            self._notify('_status')
            if state['tx_busy'] != prev_busy:
                self._notify('tx_busy')
            for key in ('mode', 'fpga_active', 'freq', 'bw', 'gain',
                        'fft_size', 'agc_mode', 'iq_bits', 'target_fps',
                        'tx_atten', 'workers'):
                if state.get(key) != old_state.get(key):
                    self._notify(key)
            # Coverage drifts continuously rather than changing on command,
            # so an exact-inequality check would fire on nearly every poll.
            # Notify only on a visible move.
            try:
                old_cov = float(old_state.get('psd_coverage') or -1.0)
            except ValueError:
                old_cov = -1.0
            if abs(status.psd_coverage - old_cov) >= 0.005:
                self._notify('psd_coverage')
            if new_sample_rate != old_sample_rate and old_sample_rate is not None:
                self._notify('sample_rate')
        elif msg_type == MsgType.RESP_CAPS:
            caps = parse_caps(payload)
            state = dict(self._state)
            state['fpga_build_id'] = str(caps.fpga_build_id)
            state['features'] = str(caps.features)
            state['fw_build_id'] = str(caps.fw_build_id)
            self._state = state
            self._notify('fpga_build_id')
            self._notify('features')
            self._notify('fw_build_id')
        elif msg_type == MsgType.RESP_TX_STATS:
            self._tx_stats = parse_tx_stats(payload)
            self._notify('_tx_stats')
        elif msg_type == MsgType.RESP_RETRY:
            self._tx_push_backpressure = True

    # --- Lifecycle (via make) ---

    def _make(self, target: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['make', target],
            capture_output=True, text=True, timeout=timeout,
            cwd=self._project_dir,
        )

    def sync(self) -> bool:
        """Deploy: stop daemon, build firmware + scp to Pluto."""
        with self._doing('deploy'):
            self.stop()
            try:
                result = self._make('deploy', timeout=120)
            except subprocess.TimeoutExpired:
                raise RuntimeError('sync timed out (build too slow?)')
            if result.returncode != 0:
                stderr = result.stderr.strip()
                stdout = result.stdout.strip()
                detail = stderr.split('\n')[-1] if stderr else stdout.split('\n')[-1] if stdout else 'unknown'
                raise RuntimeError(f'sync failed: {detail}')
            return True

    def start(self, port: int | None = None) -> bool:
        """Start daemon on Pluto.

        If port is given and differs from current, stop and restart.
        If daemon is already running on current port, do nothing.
        A daemon we actually launched is marked fresh: the next connect
        seeds it with config.
        """
        with self._doing('start'):
            restart = port is not None and port != self._port
            if port is not None:
                self._port = port
                self._host_display = f'{self._host}:{self._port}'
            try:
                result = self._ssh('pidof shesha-daemon')
                already_running = result.returncode == 0 and result.stdout.strip()

                if already_running and not restart:
                    return True

                if already_running and restart:
                    self.stop()

                cmd = (
                    f'killall shesha-daemon 2>/dev/null;'
                    f' nohup shesha-daemon {self._port}'
                    f' > /tmp/shesha.log 2>&1 &'
                )
                result = self._ssh(cmd, timeout=15)
                if result.returncode == 0:
                    time.sleep(1.0)
                    self._fresh_start = True
                    return True
                return False
            except Exception:
                return False

    def stop(self) -> bool:
        """Stop daemon on Pluto via make stop."""
        with self._doing('stop'):
            self._close()
            try:
                result = self._make('stop', timeout=10)
                return result.returncode == 0
            except Exception:
                return False

    def connect(self, host: str | None = None, port: int | None = None) -> bool:
        """TCP connect to an already-running daemon.

        Adopts the daemon's state: subscribe + one status request.  No
        config is pushed — unless we just started the daemon ourselves
        (_fresh_start), in which case it gets the config seed first.
        """
        with self._doing('connect'):
            if host:
                self._host = host
            if port:
                self._port = port
            self._host_display = f'{self._host}:{self._port}'
            if self._client is not None:
                self._close()
            client = _DaemonClient(self._host, self._port, timeout=5.0)
            try:
                client.connect()
                # Seed through the local client.  Publishing it first would
                # let the event loop read while the seed waits for status,
                # and two readers corrupt the shared buffer.
                if self._fresh_start:
                    self._seed_fresh(client)
                    if not client.connected:
                        raise ConnectionError('seed failed')
            except (OSError, TimeoutError, ConnectionError):
                client.disconnect()
                self._set_connected(False)
                return False
            self._client = client
            self._set_connected(True)
            self._desired_mode = None
            self._send(cmd_subscribe(SUBSCRIBE_ALL))
            self._send(cmd_status())
            self._send(cmd_caps())
            return True

    def radio_policy(self) -> RadioPolicy | None:
        return self._radio_policy

    def _seed_fresh(self, client) -> None:
        """Configure a daemon we just started.

        Mode first: the daemon validates bw/sample_rate against the active
        producer.  Then read that producer's advertised policy and replay
        only the keys it treats as user inputs.  A value that belongs to
        another mode (or is out of range) is dropped, leaving the mode's
        own entry-time settlement in place.

        All traffic goes through the local client.  The event loop must not
        see it until the seed's status read is finished.
        """
        if self._desired_mode is not None:
            try:
                self._send(cmd_set_mode(int(self._desired_mode)), client)
            except ValueError:
                pass
        policy = self._await_status_policy(client)
        for key in self.radio_config_keys():
            value = self._config.get_normalized(key)
            if value is None or not self._seedable(key, value, policy):
                continue
            self.apply_config(key, value, client)
        self._fresh_start = False

    def _seedable(self, key: str, value: str,
                  policy: RadioPolicy | None) -> bool:
        """May this saved key be replayed as an input for the active mode?

        A mode-pinned AGC drops a saved gain; a fresh daemon never starts on
        the FPGA producer, so an unread policy leaves gain seedable.
        """
        if key == 'gain':
            return policy is None or not policy.agc_locked
        if key not in ('bw', 'sample_rate'):
            return True
        if policy is None:
            return False
        try:
            hz = int(float(value))
        except (ValueError, TypeError):
            return False
        if key == 'bw':
            return policy.bw_is_user_input and policy.allows_bw(hz)
        return policy.sr_is_user_input and policy.allows_sr(hz)

    def _await_status_policy(self, client,
                             timeout: float = 2.0) -> RadioPolicy | None:
        """Ask for status and block until the reply reveals the policy.

        Runs inside connect() before the client is published, so the seed
        is decided from the daemon's own rules rather than a client-side
        copy of them.  recv_raw reports a dead link by disconnecting, not
        raising, so a dropped connection returns at once instead of waiting
        out the timeout.
        """
        if not client:
            return None
        try:
            client.send_raw(cmd_status())
        except (OSError, ConnectionError):
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not client.connected:
                return None
            frames = client.recv_raw(timeout=0.05)
            policy = None
            for msg_type, payload in frames:
                self._dispatch(msg_type, payload, [])
                if msg_type == MsgType.RESP_STATUS:
                    policy = self._radio_policy
            if policy is not None:
                return policy
        return None

    def _ssh(self, cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
        ssh_cmd = [
            'sshpass', '-p', 'analog', 'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            f'root@{self._host}', cmd,
        ]
        return subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)

    def smart_connect(self, host: str | None = None, port: int | None = None) -> str:
        """Smart connect: skip steps that are already satisfied.

        - If already connected to same host:port, do nothing.
        - Only sync if binary is missing on Pluto.
        - Only start if daemon is not running.
        - Then TCP connect.

        The whole sequence, probes included, runs as one activity so the
        glyph flashes throughout instead of idling red between steps.
        """
        with self._doing('connect'):
            target_host = host or self._host
            target_port = port or self._port

            # Already connected to same target?  Do nothing.
            if (self.connected and self._client and
                    target_host == self._host and target_port == self._port):
                return f'already connected {self._host_display}'

            # Update target
            if host:
                self._host = host
            if port:
                self._port = port
            self._host_display = f'{self._host}:{self._port}'

            # Close existing connection if switching targets
            self._close()

            # Check if binary is deployed
            result = self._ssh('test -f /usr/bin/shesha-daemon')
            if result.returncode != 0:
                if not self.sync():
                    return 'sync failed: could not upload binary'

            # Check if daemon is running
            result = self._ssh('pidof shesha-daemon')
            if result.returncode != 0 or not result.stdout.strip():
                if not self.start(self._port):
                    return 'start failed: daemon did not start'

            # TCP connect
            if self.connect():
                return f'connected {self._host_display}'
            return 'connect failed: daemon not responding'

    def ping(self) -> None:
        """Send status request as heartbeat."""
        self._send(cmd_status())

    def set_mode(self, mode: int) -> None:
        if self._client and self._client.connected:
            self._sent('mode')
            # A different producer means the advertised policy is about to
            # change.  Drop it now so callers defer to the firmware until
            # the new status lands, rather than judging commands against
            # rules that no longer apply.
            if self._state.get('mode') != str(mode):
                self._radio_policy = None
            self._send(cmd_set_mode(mode))
        else:
            # Pre-connect: remember for the fresh-daemon seed.  A daemon
            # that is already running keeps its own mode.
            self._desired_mode = str(mode)

    def set_fpga(self, enable: bool) -> None:
        if self._client and self._client.connected:
            self._sent('fpga_active')
            self._send(cmd_set_fpga(enable))

    def set_iq_bits(self, bits: int) -> bool:
        """Ask for a wire width.  The status push reports the result.

        TUI key handlers run outside the event loop, so they must never read
        the socket: a synchronous reply competes with the loop's drain and
        corrupts the shared receive buffer.
        """
        if not self._active_client():
            return False
        self._sent('iq_bits')
        self._send(cmd_set_iq_bits(bits))
        return True

    def wifi_tx(self, rate_mbps: int, psdu: bytes,
                repeat: int = 1, interval_us: int = 0,
                cyclic: bool = False) -> None:
        client = self._active_client()
        if not client:
            raise RuntimeError('not connected')
        try:
            client.send_raw(cmd_tx_wifi(rate_mbps, psdu,
                                        repeat, interval_us, cyclic))
        except (OSError, ConnectionError):
            self._close()
            raise

    def tx_stop(self) -> None:
        client = self._active_client()
        if not client:
            raise RuntimeError('not connected')
        try:
            client.send_raw(cmd_tx_stop())
        except (OSError, ConnectionError):
            self._close()
            raise

    def fm_tx_stream_start(self, audio, *, audio_rate=44100,
                           deviation=75_000, dac_rate=2_100_000, rds=None):
        client = self._active_client()
        if not client:
            raise RuntimeError('not connected')
        self._tx_stats = None
        try:
            client.send_raw(cmd_tx_fm_stream_start(
                audio, audio_rate=audio_rate,
                deviation=deviation, dac_rate=dac_rate, rds=rds))
            return True
        except (OSError, ConnectionError):
            self._close()
            raise

    def set_rds(self, rds):
        client = self._active_client()
        if not client:
            raise RuntimeError('not connected')
        try:
            client.send_raw(cmd_set_rds(rds))
            return True
        except (OSError, ConnectionError):
            self._close()
            raise

    def fm_tx_stream_push(self, audio):
        from ..client import PushResult
        client = self._active_client()
        if not client:
            return PushResult.ERROR
        if self._tx_push_backpressure:
            self._tx_push_backpressure = False
            return PushResult.RETRY
        try:
            client.send_raw(cmd_tx_stream_data(audio))
            return PushResult.OK
        except (OSError, ConnectionError):
            self._close()
            return PushResult.ERROR

    def fm_tx_stream_end(self):
        client = self._active_client()
        if not client:
            return False
        try:
            client.send_raw(cmd_tx_stream_end())
            return True
        except (OSError, ConnectionError):
            self._close()
            return False

    def fm_tx_stream_flush(self):
        client = self._active_client()
        if not client:
            return False
        try:
            client.send_raw(cmd_tx_stream_flush())
            return True
        except (OSError, ConnectionError):
            self._close()
            return False

    def tx_stats(self):
        """Return the most recently cached TX stats.

        Sends a stats request so the next poll() picks up a fresh
        RESP_TX_STATS.  Returns the cached value immediately (may be
        one frame stale).
        """
        client = self._active_client()
        if not client:
            return None
        # Request a fresh stats push for the next poll cycle
        try:
            client.send_raw(cmd_tx_stats())
        except (OSError, ConnectionError):
            self._close()
            return None
        return self._tx_stats

    def check_alive(self, timeout: float = 1.0, grace: float = 5.0) -> bool:
        """Close the link if no bytes have arrived for *timeout*.

        Heartbeats are answered sub-second, so a short timeout is enough;
        this is what notices an unplugged Pluto before the kernel's own TCP
        timeout (many seconds) does.  Liveness is tracked from real bytes
        only — see DaemonClient.last_rx.

        A mutating command can block the daemon past that: switching from
        FM to WiFi holds the main loop for about a second before it answers.
        While an answer is outstanding the link gets *grace* instead, so the
        watchdog does not kill a healthy connection.
        """
        client = self._client
        if not self.connected or not client:
            return False
        limit = max(timeout, grace) if self._pending_mutations else timeout
        if time.time() - client.last_rx > limit:
            self._close()
            return False
        return True

    def _close(self) -> None:
        """Close TCP connection.

        The advertised policy belongs to the link that is going away, so
        drop it too: a later status from a new link must be what re-arms
        client-side enforcement.
        """
        if self._client:
            self._client.disconnect()
        self._client = None
        self._radio_policy = None
        self._pending_mutations = 0
        self._set_connected(False)
