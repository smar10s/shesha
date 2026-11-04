"""DummyDaemon — in-memory fake daemon for offline TUI testing."""
from __future__ import annotations
from dataclasses import dataclass
import math
import random
import time

from ..channels import CHANNEL_TO_FREQ
from ..frames import (
    build_ack, build_beacon, build_data, build_probe_req, build_probe_resp,
)
from ..protocol import RdsState, RadioPolicy, pack_iq
from .daemon import Daemon


# 16-QAM symbol map: gray-ish index -> (I, Q) level in {0,1,2,3}.
_QAM16 = [(i & 3, i >> 2) for i in range(16)]


# ─── Fake frame types for wifi generation ─────────────────────────────────

@dataclass
class _FakeMeta:
    bssid: str = ""
    sa: str = ""
    da: str = ""
    ssid: str = ""
    is_eapol: bool = False


@dataclass
class _FakeIe:
    channel: int = 0
    security: str = "unknown"
    cipher: str = "unknown"
    phy_flags: int = 0
    phy: str = ""
    max_rate: float = 0.0
    n_ss: int = 0
    chan_width: int = 0
    country: str = ""
    pmf: str = "none"


class DummyDaemon(Daemon):
    """In-memory daemon for offline testing.

    State is set directly (it IS the daemon): apply() writes state and
    notifies, mirroring the real firmware's status push after a command.
    Generates minimal fake PSD with one drifting signal, or fake wifi
    frames when in wifi mode.
    """

    _N_BINS = 1024
    _NOISE_FLOOR = -90.0

    def __init__(self, config) -> None:
        super().__init__()
        self._config = config
        self._t = 0.0
        self.connected = True
        self._host_display = 'dummy'
        self._state['tx_busy'] = '0'
        self._state['tx_proto'] = '0'
        self._state['tx_repeat'] = '0'
        self._state['tx_interval_us'] = '0'
        self._state['fpga_active'] = '0'
        # Device identity.  The build id is a deliberate placeholder so it
        # can never be mistaken for a real bitstream fingerprint.
        self._state['fpga_build_id'] = str(0xDEADBEEF)
        self._state['fw_build_id'] = str(0xCAFEBABE)
        self._state['features'] = str(0x03 | (1 << 2) | (1 << 3))
        # Seed radio state from config exactly like a freshly started real
        # daemon: same derived key set, same normalized values.
        for key in self.radio_config_keys():
            value = config.get_normalized(key)
            if value is not None:
                self.apply_config(key, value)
        # Wifi mode state
        self._wifi_mode = False
        self._wifi_t = 0.0
        # FM mode state
        self._fm_mode = False
        self._fm_t = 0.0
        # FM TX playout model: audio is fed ahead of the DAC, so telemetry
        # advances with wall time rather than with the push pointer.
        self._clock = time.monotonic
        self._tx_fed = 0
        self._tx_started_at = 0.0
        self._tx_dac_rate = 2_100_000
        self._tx_audio_rate = 44100
        # IQ mode state
        self._iq_mode = False
        self._iq_t = 0.0
        self._iq_bits = 8
        self._fake_aps = [
            {'bssid': 'aa:bb:cc:00:01:00', 'ssid': 'CoffeeShop-5G', 'channel': 36,
             'security': 'wpa2-psk', 'cipher': 'ccmp', 'pmf': 'capable',
             'phy': 'VHT-80', 'country': 'US', 'max_rate': 866.0,
             'n_ss': 2, 'chan_width': 2},
            {'bssid': 'aa:bb:cc:00:01:01', 'ssid': 'CoffeeShop-5G', 'channel': 36,
             'security': 'wpa2-psk', 'cipher': 'ccmp', 'pmf': 'capable',
             'phy': 'VHT-80', 'country': 'US', 'max_rate': 866.0,
             'n_ss': 2, 'chan_width': 2},
            {'bssid': 'dd:ee:ff:10:20:30', 'ssid': 'HomeNet', 'channel': 44,
             'security': 'wpa3-sae', 'cipher': 'gcmp-256', 'pmf': 'required',
             'phy': 'HE/VHT-80', 'country': 'CA', 'max_rate': 1201.0,
             'n_ss': 2, 'chan_width': 2},
            {'bssid': '11:22:33:44:55:60', 'ssid': 'CorpWifi', 'channel': 36,
             'security': 'wpa2-eap', 'cipher': 'ccmp', 'pmf': 'required',
             'phy': 'HT-40', 'country': 'US', 'max_rate': 300.0,
             'n_ss': 2, 'chan_width': 1},
            {'bssid': '11:22:33:44:55:61', 'ssid': 'CorpWifi-Guest', 'channel': 36,
             'security': 'open', 'cipher': 'none', 'pmf': 'none',
             'phy': 'HT', 'country': 'US', 'max_rate': 150.0,
             'n_ss': 1, 'chan_width': 0},
            {'bssid': '77:88:99:aa:bb:cc', 'ssid': '', 'channel': 40,
             'security': 'wpa2-psk', 'cipher': 'ccmp', 'pmf': 'none',
             'phy': 'VHT-80', 'country': '', 'max_rate': 433.0,
             'n_ss': 1, 'chan_width': 2},
        ]
        self._fake_stas = [
            {'mac': 'c0:ff:ee:00:00:01', 'ap_idx': 0},
            {'mac': 'c0:ff:ee:00:00:02', 'ap_idx': 0},
            {'mac': 'de:ad:be:ef:00:01', 'ap_idx': 2},
            {'mac': 'de:ad:be:ef:00:02', 'ap_idx': 3},
            {'mac': 'de:ad:be:ef:00:03', 'ap_idx': 3},
            {'mac': 'ff:ff:00:11:22:33', 'ap_idx': -1},  # unassociated prober
            {'mac': 'ba:dc:af:e0:00:01', 'ap_idx': -1},  # unassociated prober
            {'mac': 'ba:dc:af:e0:00:02', 'ap_idx': -1},  # unassociated prober
        ]

    def apply(self, key: str, value: str) -> None:
        if key == 'gain':
            if value in ('auto', 'fast', 'fast_attack'):
                self._state['gain'] = 'auto'
                self._state['agc_mode'] = 'fast'
            elif value in ('slow', 'slow_attack'):
                self._state['gain'] = 'auto'
                self._state['agc_mode'] = 'slow'
            else:
                try:
                    float(value)
                except (ValueError, TypeError):
                    return
                self._state['gain'] = value
                self._state['agc_mode'] = 'manual'
            self._sent('gain', 'agc_mode')
            self._notify('gain')
            return
        if key == 'fps':
            self._state['target_fps'] = value
        elif key == 'tx_gain':
            self._state['tx_atten'] = value
        else:
            self._state[key] = value
        self._sent(key)
        self._notify(key)

    def poll(self) -> list[dict]:
        if self._fm_mode:
            return self._poll_fm()

        if self._iq_mode:
            return self._poll_iq()

        if self._wifi_mode:
            return self._poll_wifi()

        t = self._t
        self._t += 0.02
        bins = [self._NOISE_FLOOR + random.gauss(0, 2.0)
                for _ in range(self._N_BINS)]
        # One drifting signal
        center = 512 + 50 * math.sin(t * 0.2)
        for i in range(max(0, int(center) - 40), min(self._N_BINS, int(center) + 40)):
            dist = (i - center) / 20.0
            signal_db = -30.0 - 10.0 * dist * dist
            if signal_db > bins[i]:
                bins[i] = signal_db
        try:
            freq_hz = int(float(self._state.get('freq', '5180000000')))
        except (ValueError, TypeError):
            freq_hz = 5180000000
        return [{
            'ts': t,
            'type': 'psd',
            'center_freq_mhz': freq_hz / 1e6,
            'db_floor': min(bins),
            'db_ceil': max(bins),
            'bins': bins,
        }]

    def _poll_fm(self) -> list[dict]:
        """Generate a synthetic FM audio frame (1 kHz tone + noise).

        The RDS state is synthesized directly rather than modulated onto the
        audio: the decode runs in firmware, so there is nothing for the
        client to exercise, and a fixed state keeps snapshots deterministic.
        """
        self._fm_t += 0.02
        t = self._fm_t
        rate = 43750
        n = 1024
        audio = []
        for i in range(n):
            sample_time = self._fm_t * rate + i
            phase = 2 * math.pi * 1000 * (sample_time / rate)
            audio.append(0.4 * math.sin(phase) + random.gauss(0, 0.02))

        messages = [
            'Shesha test transmission',
            'Hack The Planet',
        ]
        rt = messages[int(t / 3.0) % len(messages)]
        rds = RdsState(
            synced=True, pi_valid=True, pi=0x8203, pty=10,
            tp=True, ta=False, ms=True, di=0,
            ps='SHESHA  ', ps_mask=0xFF,
            rt=rt.ljust(64), rt_len=len(rt), rt_mask=(1 << len(rt)) - 1,
            rt_ab=False, rt_valid=True,
            ptyn='TEST', ptyn_mask=0xFF, ptyn_ab=False,
            af=[126, 92], ct_mjd=61000, ct_minutes=12 * 60 + 34,
            ct_offset_half_hours=-10, ct_valid=True,
            oda=[], groups=int(t * 11), blocks=int(t * 46),
            block_errors=0, sync_losses=0,
        )
        return [{'ts': t, 'audio_rate': rate, 'audio': audio,
                 'rds': rds, 'type': 'fm'}]

    def _poll_iq(self) -> list[dict]:
        """Synthesize a 16-QAM constellation, packed like the wire."""
        self._iq_t += 0.02
        t = self._iq_t
        pts = []
        for k in range(1024):
            a, b = _QAM16[(k + int(t * 50)) % 16]
            noise = random.gauss(0, 0.03)
            pts.append(complex((a - 1.5) / 1.5 + noise,
                               (b - 1.5) / 1.5 + noise))
        bits = self._iq_bits
        return [{
            'ts': t, 'type': 'iq', 'bits': bits, 'n_samples': len(pts),
            'samples': pack_iq(pts, bits), 'sample_rate_hz': 2_100_000,
            'freq_mhz': 915.0,
        }]

    def _poll_wifi(self) -> list[dict]:
        """Generate fake wifi frames per poll cycle."""
        frames = []
        self._wifi_t += 0.02
        t = self._wifi_t

        # Emit a beacon from a random AP (~35% of polls)
        if random.random() < 0.35:
            ap = random.choice(self._fake_aps)
            frames.append(self._make_beacon_frame(ap, t))

        # Emit a data frame from a random STA (~20% of polls)
        if random.random() < 0.2:
            sta = random.choice(self._fake_stas)
            if sta['ap_idx'] >= 0:
                ap = self._fake_aps[sta['ap_idx']]
                frames.append(self._make_data_frame(sta, ap, t))
            else:
                frames.append(self._make_probe_frame(sta, t))

        # Emit an ACK frame (~15% of polls)
        if random.random() < 0.15:
            frames.append(self._make_ack_frame(t))

        # Emit a probe response (~10% of polls)
        if random.random() < 0.10:
            ap = random.choice(self._fake_aps)
            frames.append(self._make_probe_resp_frame(ap, t))

        # Emit an unknown/he frame (~5% of polls) with always-failed FCS
        if random.random() < 0.05:
            frames.append(self._make_unknown_frame(t))

        return frames

    def _make_beacon_frame(self, ap: dict, t: float) -> dict:
        bssid_bytes = bytes.fromhex(ap['bssid'].replace(':', ''))
        ssid = ap['ssid']
        psdu = build_beacon(
            ssid=ssid, bssid=bssid_bytes, channel=ap['channel'])
        return {
            'ts': t,
            'rate_code': 0,
            'rate_mbps': 6,
            'fcs_ok': True,
            'length': len(psdu),
            'frame_class': 3,
            'frame_class_name': 'beacon',
            'rx_gain': 40.0 + random.gauss(0, 3.0),
            'rx_freq_hz': CHANNEL_TO_FREQ.get(ap['channel'], 0) * 1_000_000,
            'psdu': psdu,
            'meta': _FakeMeta(
                bssid=ap['bssid'], sa=ap['bssid'], da='ff:ff:ff:ff:ff:ff',
                ssid=ap['ssid'], is_eapol=False),
            'ie': _FakeIe(
                channel=ap['channel'], security=ap['security'],
                cipher=ap['cipher'], phy_flags=0, phy=ap['phy'],
                max_rate=ap['max_rate'], n_ss=ap.get('n_ss', 0),
                chan_width=ap.get('chan_width', 0),
                country=ap['country'], pmf=ap['pmf']),
            'type': 'wifi',
        }

    def _make_data_frame(self, sta: dict, ap: dict, t: float) -> dict:
        sta_bytes = bytes.fromhex(sta['mac'].replace(':', ''))
        ap_bytes = bytes.fromhex(ap['bssid'].replace(':', ''))
        psdu = build_data(da=ap_bytes, sa=sta_bytes, bssid=ap_bytes)
        return {
            'ts': t,
            'rate_code': 0,
            'rate_mbps': random.choice([24, 36, 48, 54]),
            'fcs_ok': random.random() > 0.10,
            'length': len(psdu),
            'frame_class': 7,
            'frame_class_name': 'data',
            'rx_gain': 35.0 + random.gauss(0, 5.0),
            'rx_freq_hz': CHANNEL_TO_FREQ.get(ap['channel'], 0) * 1_000_000,
            'psdu': psdu,
            'meta': _FakeMeta(
                bssid=ap['bssid'], sa=sta['mac'], da=ap['bssid'],
                ssid='', is_eapol=False),
            'ie': None,
            'type': 'wifi',
        }

    def _make_probe_frame(self, sta: dict, t: float) -> dict:
        probe_ssids = ['HomeNet', 'airport-wifi', 'Starbucks', '']
        ssid = random.choice(probe_ssids)
        sta_bytes = bytes.fromhex(sta['mac'].replace(':', ''))
        psdu = build_probe_req(ssid=ssid, sa=sta_bytes)
        return {
            'ts': t,
            'rate_code': 0,
            'rate_mbps': 6,
            'fcs_ok': True,
            'length': len(psdu),
            'frame_class': 5,
            'frame_class_name': 'probe_req',
            'rx_gain': 30.0 + random.gauss(0, 4.0),
            'rx_freq_hz': random.choice(
                list(CHANNEL_TO_FREQ.values())) * 1_000_000,
            'psdu': psdu,
            'meta': _FakeMeta(
                bssid='ff:ff:ff:ff:ff:ff', sa=sta['mac'],
                da='ff:ff:ff:ff:ff:ff', ssid=ssid, is_eapol=False),
            'ie': None,
            'type': 'wifi',
        }

    def _make_ack_frame(self, t: float) -> dict:
        psdu = build_ack()
        return {
            'ts': t,
            'rate_code': 0,
            'rate_mbps': 24,
            'fcs_ok': True,
            'length': len(psdu),
            'frame_class': 0,
            'frame_class_name': 'ack',
            'rx_gain': 38.0 + random.gauss(0, 2.0),
            'psdu': psdu,
            'meta': None,
            'ie': None,
            'type': 'wifi',
        }

    def _make_probe_resp_frame(self, ap: dict, t: float) -> dict:
        bssid_bytes = bytes.fromhex(ap['bssid'].replace(':', ''))
        ssid = ap['ssid']
        psdu = build_probe_resp(
            ssid=ssid, bssid=bssid_bytes, channel=ap['channel'])
        return {
            'ts': t,
            'rate_code': 0,
            'rate_mbps': 6,
            'fcs_ok': True,
            'length': len(psdu),
            'frame_class': 6,
            'frame_class_name': 'probe_resp',
            'rx_gain': 40.0 + random.gauss(0, 3.0),
            'rx_freq_hz': CHANNEL_TO_FREQ.get(ap['channel'], 0) * 1_000_000,
            'psdu': psdu,
            'meta': _FakeMeta(
                bssid=ap['bssid'], sa=ap['bssid'], da='ff:ff:ff:ff:ff:ff',
                ssid=ap['ssid'], is_eapol=False),
            'ie': None,
            'type': 'wifi',
        }

    def _make_unknown_frame(self, t: float) -> dict:
        psdu = build_ack()  # minimal stub, FCS will fail anyway
        return {
            'ts': t,
            'rate_code': 0,
            'rate_mbps': 6,
            'fcs_ok': False,
            'length': len(psdu),
            'frame_class': 9,
            'frame_class_name': 'unknown',
            'rx_gain': 30.0 + random.gauss(0, 5.0),
            'psdu': psdu,
            'meta': None,
            'ie': None,
            'type': 'wifi',
        }

    def set_mode(self, mode: int) -> None:
        self._wifi_mode = (mode == 1)  # WIFI
        self._fm_mode = (mode == 3)    # FM
        self._iq_mode = (mode == 2)    # IQ
        self._state['mode'] = str(mode)
        self._sent('mode')
        self._notify('mode')

    def radio_policy(self) -> RadioPolicy:
        """Mirror the firmware's per-mode radio descriptors."""
        if self._iq_mode:
            return RadioPolicy(200_000, 8_000_000, 0,
                               2_100_000, 4_000_000, 0, True, 0)
        if self._fm_mode:
            return RadioPolicy(0, 0, 200_000, 0, 0, 2_100_000, False, 0)
        if self._wifi_mode:
            fpga = self._state.get('fpga_active') == '1'
            bw = 28_000_000 if fpga else 20_000_000
            return RadioPolicy(0, 0, bw, 0, 0, 20_000_000, False,
                               1 if fpga else 0)
        return RadioPolicy(200_000, 56_000_000, 0, 0, 0, 0, False, 0)

    def set_iq_bits(self, bits: int) -> bool:
        self._iq_bits = bits
        self._state['iq_bits'] = str(bits)
        self._sent('iq_bits')
        self._notify('iq_bits')
        return True

    def set_fpga(self, enable: bool) -> None:
        self._state['fpga_active'] = '1' if enable else '0'
        self._sent('fpga_active')
        self._notify('fpga_active')

    def wifi_tx(self, rate_mbps: int, psdu: bytes,
                repeat: int = 1, interval_us: int = 0,
                cyclic: bool = False) -> None:
        self._state['tx_busy'] = '1'
        self._notify('tx_busy')

    def tx_stop(self) -> None:
        self._state['tx_busy'] = '0'
        self._notify('tx_busy')

    def fm_tx_stream_start(self, audio, *, audio_rate=44100,
                           deviation=75_000, dac_rate=2_100_000, rds=None):
        self._tx_fed = len(audio)
        self._tx_started_at = self._clock()
        self._tx_dac_rate = dac_rate
        self._tx_audio_rate = audio_rate
        self._state['tx_busy'] = '1'
        self._state['tx_proto'] = '3'  # FM
        self._notify('tx_busy')
        return True

    def set_rds(self, rds):
        self._state['tx_rds'] = '1' if rds is not None and rds.enabled else '0'
        self._notify('tx_rds')
        return True

    def fm_tx_stream_push(self, audio):
        from shesha.client import PushResult
        self._tx_fed += len(audio)
        return PushResult.OK

    def fm_tx_stream_end(self):
        self._state['tx_busy'] = '0'
        self._notify('tx_busy')
        return True

    def fm_tx_stream_flush(self):
        return True

    def tx_stats(self):
        from shesha.protocol import TxStats
        active = self._state.get('tx_busy') == '1'
        dac_rate = self._tx_dac_rate
        fed = int(self._tx_fed * dac_rate / self._tx_audio_rate)
        elapsed_us = (int((self._clock() - self._tx_started_at) * 1e6)
                      if active else 0)
        emitted = min(fed, int(elapsed_us * dac_rate / 1e6)) if active else 0
        return TxStats(
            tx_active=active,
            fm_active=self._state.get('tx_proto') == '3',
            end_of_stream=False, dac_rate=dac_rate,
            fed=fed, emitted=emitted, elapsed_us=elapsed_us, depth=0,
            max_depth=0, high_water=0, underruns=0,
            ring_fill=0, ring_size=262144, audio_rate=self._tx_audio_rate,
            audio_consumed=int(emitted * self._tx_audio_rate / dac_rate),
            starved_samples=0, push_rejects=0,
            iq_active=False, iq_ring_fill=0, iq_push_rejects=0,
            iq_starved=0)
