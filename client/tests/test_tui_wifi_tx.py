"""Tests for the TUI WiFi transmit commands.

The TUI shares its frame builders with the CLI. These tests pin the
command parsing and the arguments handed to the daemon.
"""
import pytest

from shesha.frames import build_beacon, build_deauth, parse_mac
from shesha.tui.wifi import WifiApp


class _TxDaemon:
    def __init__(self, freq_hz: int = 2_437_000_000) -> None:
        self.calls: list[tuple] = []
        self.events: list[tuple] = []
        self._state = {'freq': str(freq_hz)}

    def get_state(self) -> dict:
        return dict(self._state)

    def apply(self, key: str, value: str) -> None:
        self.events.append(('apply', key, value))
        if key == 'freq':
            self._state['freq'] = value

    def wifi_tx(self, rate_mbps: int, psdu: bytes, repeat: int = 1,
                interval_us: int = 0, cyclic: bool = False) -> None:
        self.events.append(('wifi_tx',))
        self.calls.append((rate_mbps, psdu, repeat, interval_us, cyclic))

    def tx_stop(self) -> None:
        pass


def _tx(daemon: _TxDaemon) -> tuple:
    assert len(daemon.calls) == 1
    return daemon.calls[0]


def _rebuild_beacon(psdu: bytes, **kwargs) -> bytes:
    """Rebuild a beacon with the timestamp from the transmitted PSDU."""
    timestamp = int.from_bytes(psdu[24:32], 'little')
    return build_beacon(timestamp=timestamp, **kwargs)


def test_tx_usage_lists_frame():
    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command([], daemon)
    assert err is not None
    assert 'tx frame' in err
    assert not daemon.calls


def test_tx_unknown_op():
    err, _detail = WifiApp()._tx_command(['nope'], _TxDaemon())
    assert err == 'unknown tx op: nope'


def test_beacon_shorthand_is_cyclic_by_default():
    daemon = _TxDaemon()
    err, detail = WifiApp()._tx_command(['beacon', 'TEST'], daemon)
    assert err is None
    assert detail == 'TEST'
    rate, psdu, repeat, interval, cyclic = _tx(daemon)
    assert rate == 6
    assert psdu == _rebuild_beacon(psdu, ssid='TEST')
    assert (repeat, interval, cyclic) == (1, 0, True)


def test_beacon_count_switches_to_finite_burst():
    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command(
        ['beacon', 'TEST', '--count', '5', '--interval', '1000'], daemon)
    assert err is None
    rate, psdu, repeat, interval, cyclic = _tx(daemon)
    assert (rate, repeat, interval, cyclic) == (6, 5, 1000, False)
    assert psdu[24:32]  # still a full beacon


def test_beacon_rejects_count_with_cyclic():
    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command(
        ['beacon', 'TEST', '--count', '5', '--cyclic'], daemon)
    assert err == 'count/interval cannot be combined with --cyclic'
    assert not daemon.calls


def test_frame_beacon_uses_shared_builder():
    daemon = _TxDaemon()
    err, detail = WifiApp()._tx_command(
        ['frame', 'beacon', 'TEST',
         '--bssid', 'aa:bb:cc:dd:ee:ff',
         '--da', '11:22:33:44:55:66',
         '--sa', 'aa:bb:cc:dd:ee:00',
         '--channel', '11', '--seq', '7', '--capability', '0x0411',
         '--beacon-interval', '200', '--ie', '221:0102',
         '--rate', '12', '--count', '3'], daemon)
    assert err is None
    assert detail == 'TEST'
    rate, psdu, repeat, interval, cyclic = _tx(daemon)
    timestamp = int.from_bytes(psdu[24:32], 'little')
    assert psdu == build_beacon(
        ssid='TEST', bssid=parse_mac('aa:bb:cc:dd:ee:ff'),
        da=parse_mac('11:22:33:44:55:66'),
        sa=parse_mac('aa:bb:cc:dd:ee:00'),
        channel=11, seq=7, capability=0x0411, beacon_interval=200,
        ies=[bytes.fromhex('dd020102')], timestamp=timestamp)
    assert (rate, repeat, interval, cyclic) == (12, 3, 0, False)


def test_frame_beacon_ssid_option():
    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command(
        ['frame', 'beacon', '--ssid', 'OPT'], daemon)
    assert err is None
    psdu = _tx(daemon)[1]
    assert psdu == _rebuild_beacon(psdu, ssid='OPT')


def test_beacon_derives_channel_from_radio():
    daemon = _TxDaemon(5_180_000_000)
    err, _detail = WifiApp()._tx_command(
        ['frame', 'beacon', 'TEST'], daemon)
    assert err is None
    psdu = _tx(daemon)[1]
    assert psdu == _rebuild_beacon(psdu, ssid='TEST', channel=36)
    assert daemon.events == [('wifi_tx',)]


def test_beacon_shorthand_uses_current_channel():
    daemon = _TxDaemon(5_180_000_000)
    err, _detail = WifiApp()._tx_command(['beacon', 'TEST'], daemon)
    assert err is None
    psdu = _tx(daemon)[1]
    assert psdu == _rebuild_beacon(psdu, ssid='TEST', channel=36)


def test_beacon_channel_option_tunes_before_tx():
    daemon = _TxDaemon(5_180_000_000)
    err, _detail = WifiApp()._tx_command(
        ['frame', 'beacon', 'TEST', '--channel', '6'], daemon)
    assert err is None
    psdu = _tx(daemon)[1]
    assert psdu == _rebuild_beacon(psdu, ssid='TEST', channel=6)
    assert daemon.events == [
        ('apply', 'freq', '2437000000'), ('wifi_tx',)]


def test_beacon_requires_a_wifi_channel():
    daemon = _TxDaemon(100_100_000)
    err, _detail = WifiApp()._tx_command(
        ['frame', 'beacon', 'TEST'], daemon)
    assert err == 'radio is not tuned to a WiFi channel; pass --channel N'
    assert daemon.events == []


def test_beacon_channel_must_be_known():
    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command(
        ['frame', 'beacon', 'TEST', '--channel', '15'], daemon)
    assert err == 'unknown WiFi channel: 15'
    assert daemon.events == []


def test_deauth_channel_option_tunes_before_tx():
    daemon = _TxDaemon(100_100_000)
    err, _detail = WifiApp()._tx_command(
        ['frame', 'deauth', '11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff',
         '--channel', '6'], daemon)
    assert err is None
    assert daemon.events == [
        ('apply', 'freq', '2437000000'), ('wifi_tx',)]


def test_deauth_without_channel_uses_the_current_frequency():
    daemon = _TxDaemon(100_100_000)
    err, _detail = WifiApp()._tx_command(
        ['frame', 'deauth', '11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff'],
        daemon)
    assert err is None
    assert daemon.events == [('wifi_tx',)]


def test_raw_channel_option_tunes_before_tx():
    daemon = _TxDaemon(100_100_000)
    err, _detail = WifiApp()._tx_command(
        ['frame', 'raw', '8000', '--channel', '6'], daemon)
    assert err is None
    assert daemon.events == [
        ('apply', 'freq', '2437000000'), ('wifi_tx',)]


def test_tx_stops_scan_hopping():
    app = WifiApp()
    scan = app.views['scan']
    scan._scanning = True
    daemon = _TxDaemon()
    err, _detail = app._tx_command(['frame', 'beacon', 'TEST'], daemon)
    assert err is None
    assert scan._scanning is False


def test_invalid_tx_does_not_stop_scan_hopping():
    app = WifiApp()
    scan = app.views['scan']
    scan._scanning = True
    daemon = _TxDaemon()
    err, _detail = app._tx_command(
        ['frame', 'beacon', 'TEST', '--rate', '5'], daemon)
    assert err is not None
    assert scan._scanning is True
    assert daemon.events == []


def test_frame_beacon_rejects_two_ssids():
    err, _detail = WifiApp()._tx_command(
        ['frame', 'beacon', 'POS', '--ssid', 'OPT'], _TxDaemon())
    assert err == 'specify the SSID as an argument or with --ssid, not both'


def test_frame_deauth_positionals():
    daemon = _TxDaemon()
    err, detail = WifiApp()._tx_command(
        ['frame', 'deauth', '11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff',
         '--reason', '3', '--seq', '9', '--count', '2'], daemon)
    assert err is None
    assert detail == 'deauth'
    rate, psdu, repeat, interval, cyclic = _tx(daemon)
    assert psdu == build_deauth(
        parse_mac('11:22:33:44:55:66'), parse_mac('aa:bb:cc:dd:ee:ff'),
        reason=3, seq=9)
    assert (rate, repeat, interval, cyclic) == (6, 2, 0, False)


def test_frame_deauth_flags():
    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command(
        ['frame', 'deauth',
         '--da', '11:22:33:44:55:66', '--bssid', 'aa:bb:cc:dd:ee:ff',
         '--sa', 'aa:bb:cc:dd:ee:00'], daemon)
    assert err is None
    assert _tx(daemon)[1] == build_deauth(
        parse_mac('11:22:33:44:55:66'), parse_mac('aa:bb:cc:dd:ee:ff'),
        sa=parse_mac('aa:bb:cc:dd:ee:00'))


def test_frame_deauth_requires_both_addresses():
    err, _detail = WifiApp()._tx_command(
        ['frame', 'deauth', '11:22:33:44:55:66'], _TxDaemon())
    assert err is not None
    assert 'bssid' in err


def test_frame_raw_hex_positional():
    daemon = _TxDaemon()
    err, detail = WifiApp()._tx_command(
        ['frame', 'raw', '80:00 aa-bb'], daemon)
    assert err is None
    assert detail == 'raw'
    assert _tx(daemon)[1] == bytes.fromhex('8000aabb')


def test_frame_raw_flag_and_file(tmp_path):
    path = tmp_path / 'frame.bin'
    path.write_bytes(bytes.fromhex('8000cafe'))
    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command(
        ['frame', 'raw', '--psdu-file', str(path)], daemon)
    assert err is None
    assert _tx(daemon)[1] == bytes.fromhex('8000cafe')

    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command(
        ['frame', 'raw', '--psdu-hex', '80000000'], daemon)
    assert err is None
    assert _tx(daemon)[1] == bytes.fromhex('80000000')


def test_frame_raw_psdu_file_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'frame.bin').write_bytes(bytes.fromhex('8000cafe'))
    daemon = _TxDaemon()
    err, _detail = WifiApp()._tx_command(
        ['frame', 'raw', '--psdu-file', '~/frame.bin'], daemon)
    assert err is None
    assert _tx(daemon)[1] == bytes.fromhex('8000cafe')


def test_frame_raw_rejects_two_sources():
    err, _detail = WifiApp()._tx_command(
        ['frame', 'raw', '8000', '--psdu-hex', '8000'], _TxDaemon())
    assert err == 'specify the PSDU once (argument, --psdu-hex, or --psdu-file)'


def test_tx_command_remembers_the_frame_for_the_status_bar():
    app = WifiApp()
    err, _detail = app._tx_command(
        ['frame', 'deauth',
         '11:22:33:44:55:66', 'aa:bb:cc:dd:ee:ff', '--reason', '3'],
        _TxDaemon())
    assert err is None
    info = app._tx_bar.info
    assert info is not None
    assert info.frame_type == 'deauth'
    assert info.da == parse_mac('11:22:33:44:55:66')
    assert info.bssid == parse_mac('aa:bb:cc:dd:ee:ff')
    assert info.reason == 3
    assert info.rate_mbps == 6


def test_frame_rejects_unknown_type():
    err, _detail = WifiApp()._tx_command(
        ['frame', 'nope'], _TxDaemon())
    assert err == 'unknown frame type: nope'


def test_frame_rejects_option_for_other_type():
    err, _detail = WifiApp()._tx_command(
        ['frame', 'deauth', '--da', '11:22:33:44:55:66',
         '--bssid', 'aa:bb:cc:dd:ee:ff', '--ssid', 'X'], _TxDaemon())
    assert err == 'unknown option for deauth: --ssid'


@pytest.mark.parametrize('command, message', [
    (['beacon', 'TEST', '--rate', '5'], 'rate must be 6-54 Mbps'),
    (['beacon', 'TEST', '--count', '-1'], 'count must be >= 0'),
    (['beacon', 'TEST', '--interval', '-1'], 'interval must be >= 0'),
    (['frame', 'beacon', '--bssid', 'nope'], 'invalid --bssid: nope'),
    (['frame', 'beacon', '--ie', '221'], 'invalid --ie (expected TAG:HEX): 221'),
    (['frame', 'beacon', '--ie', '999:00'], '--ie tag out of range: 999'),
    (['frame', 'raw', ''], 'invalid PSDU'),
    (['frame', 'raw', '--psdu-file', '/does/not/exist'],
     'cannot read /does/not/exist'),
])
def test_frame_rejects_bad_values(command, message):
    err, _detail = WifiApp()._tx_command(command, _TxDaemon())
    assert err is not None
    assert message in err
