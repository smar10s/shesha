"""Tests for the always-visible WiFi TX status bar."""
import pytest

from shesha.frames import parse_mac
from shesha.tui.pytui import StyledWindow, Text, display_width
from shesha.tui.theme import DEFAULT_PALETTE
from shesha.tui.wifi import (
    ScanView, TrafficView, WifiTxBar, WifiTxInfo, _mac_forms,
    format_wifi_tx_bar,
)

DA = parse_mac('aa:bb:cc:dd:ee:ff')
BSSID = parse_mac('11:22:33:44:55:66')
IDLE = {'tx_busy': '0'}
ACTIVE = {
    'tx_busy': '1', 'tx_proto': '0', 'tx_flags': '0',
    'tx_repeat': '10', 'tx_interval_us': '5000',
    'freq': '2437000000', 'tx_atten': '-10.0',
}
DEAUTH = WifiTxInfo(
    frame_type='deauth', rate_mbps=6, psdu_len=26,
    da=DA, bssid=BSSID, reason=7)


def _plain(info, state, width):
    return Text(format_wifi_tx_bar(info, state, width, DEFAULT_PALETTE)).strip_ansi()


def _fg_code(color):
    return f'\x1b[38;2;{color[0]};{color[1]};{color[2]}m'


def test_mac_ladder():
    assert _mac_forms(DA) == [
        'aa:bb:cc:dd:ee:ff', 'aa:bb:…:ee:ff', 'aa:…:ff', '…:ff']


def test_idle_bar_is_dim():
    raw = format_wifi_tx_bar(None, IDLE, 80, DEFAULT_PALETTE)
    plain = Text(raw).strip_ansi()
    assert 'tx' in plain
    assert '--' in plain
    assert _fg_code(DEFAULT_PALETTE['dim']) in raw
    assert _fg_code(DEFAULT_PALETTE['warn']) not in raw


def test_active_wifi_bar_lights_tx_warn():
    raw = format_wifi_tx_bar(DEAUTH, ACTIVE, 80, DEFAULT_PALETTE)
    assert _fg_code(DEFAULT_PALETTE['warn']) in raw
    assert 'deauth' in Text(raw).strip_ansi()


def test_other_protocol_leaves_wifi_bar_idle():
    state = dict(ACTIVE, tx_proto='3')
    plain = _plain(DEAUTH, state, 80)
    assert 'deauth' not in plain
    assert '--' in plain


def test_full_macs_fit_at_80():
    plain = _plain(DEAUTH, ACTIVE, 80)
    assert 'aa:bb:cc:dd:ee:ff' in plain
    assert '11:22:33:44:55:66' in plain
    assert '10x@5ms' in plain
    assert 'ch6' in plain
    assert '-10dB' in plain


def test_da_stays_full_while_bssid_abbreviates_at_60():
    plain = _plain(DEAUTH, ACTIVE, 60)
    assert 'aa:bb:cc:dd:ee:ff' in plain
    assert '11:22:33:44:55:66' not in plain
    assert '11:…:66' in plain


def test_both_macs_abbreviate_at_40():
    plain = _plain(DEAUTH, ACTIVE, 40)
    assert 'aa:bb:cc:dd:ee:ff' not in plain
    assert '…:ff' in plain
    assert '…:66' in plain


@pytest.mark.parametrize('width', [20, 30, 40, 50, 60, 70, 80, 100, 120])
def test_bar_never_exceeds_width(width):
    assert display_width(_plain(DEAUTH, ACTIVE, width)) <= width


def test_beacon_shows_ssid_bssid_and_length():
    info = WifiTxInfo(
        frame_type='beacon', rate_mbps=6, psdu_len=61,
        ssid='SHESHA', bssid=BSSID)
    plain = _plain(info, dict(ACTIVE, tx_flags='1'), 100)
    assert 'ssid "SHESHA"' in plain
    assert 'bssid 11:22:33:44:55:66' in plain
    assert '61B' in plain
    assert 'cyclic' in plain


def test_raw_shows_length():
    info = WifiTxInfo(frame_type='raw', rate_mbps=6, psdu_len=61)
    plain = _plain(info, dict(ACTIVE, tx_flags='1'), 80)
    assert 'raw' in plain
    assert '61B' in plain


@pytest.mark.parametrize('state, expected', [
    (dict(tx_flags='1'), 'cyclic'),
    (dict(tx_flags='0', tx_repeat='65535', tx_interval_us='0'), '∞ blast'),
    (dict(tx_flags='0', tx_repeat='1', tx_interval_us='0'), '1x blast'),
    (dict(tx_flags='0', tx_repeat='3', tx_interval_us='500'), '3x@500us'),
    (dict(tx_flags='0', tx_repeat='3', tx_interval_us='2000'), '3x@2ms'),
])
def test_schedule_variants(state, expected):
    assert expected in _plain(DEAUTH, dict(ACTIVE, **state), 100)


def test_bar_clears_last_frame_after_tx_ends():
    bar = WifiTxBar()
    win = StyledWindow(0, 0, 80, 1, {})
    bar.info = DEAUTH

    bar.render(win, DEFAULT_PALETTE, IDLE)
    assert bar.info is DEAUTH

    bar.render(win, DEFAULT_PALETTE, ACTIVE)
    assert bar.info is DEAUTH

    bar.render(win, DEFAULT_PALETTE, IDLE)
    assert bar.info is None


class _Daemon:
    def __init__(self, state):
        self._state = state

    def get_state(self):
        return dict(self._state)


def test_scan_view_does_not_invent_a_channel_before_state_arrives():
    view = ScanView()
    view.layout(StyledWindow(0, 0, 80, 24, {}))
    first = Text(view.table.columns[0].buffer[0]).strip_ansi().strip()
    assert first == 'Scanning...'


def test_scan_view_names_the_channel_once_known():
    view = ScanView()
    view.layout(StyledWindow(0, 0, 80, 24, {}))
    view._freq_mhz = 2457.0
    view._rebuild()
    first = Text(view.table.columns[0].buffer[0]).strip_ansi().strip()
    assert first.startswith('Scanning ch10')


@pytest.mark.parametrize('view_cls', [ScanView, TrafficView])
def test_view_always_reserves_the_bar(view_cls):
    view = view_cls()
    root = StyledWindow(0, 0, 80, 24, {})
    windows = view.layout(root)

    assert view._bar_win in windows
    assert view._bar_win.height == 1
    idle = Text(view._bar_win.buffer[0]).strip_ansi()
    assert 'tx' in idle and '--' in idle

    view.daemon = _Daemon(ACTIVE)
    view._tx_bar.info = DEAUTH
    view.tick(0.0)

    active = Text(view._bar_win.buffer[0]).strip_ansi()
    assert 'deauth' in active
    assert '10x@5ms' in active
