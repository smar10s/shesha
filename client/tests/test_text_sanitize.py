"""Untrusted text must never reach the terminal as a command.

SSIDs, probed SSIDs, ID3 tags, and daemon error strings are attacker
influenced.  A terminal control sequence in any of them can retitle the
window, write the clipboard, or repaint the UI.
"""
import threading

from shesha.mp3 import TrackTags
from shesha.scan import ApRecord, StaRecord
from shesha.tui.fm import Track
from shesha.tui.pytui import (
    StyledWindow, Text, Window, sanitize_styled, sanitize_text,
)
from shesha.tui.shell import Shell
from shesha.tui.table import Table
from shesha.tui.wifi import ScanView

OSC_TITLE = "\x1b]0;pwned\x07"
OSC_CLIP = "\x1b]52;c;ZGF0YQ==\x07"
CSI_ERASE = "\x1b[2J"
SGR_RED = "\x1b[31m"


def test_sanitize_text_strips_osc_payloads():
    assert sanitize_text(f"a{OSC_TITLE}b{OSC_CLIP}c") == "abc"


def test_sanitize_text_strips_csi_c1_and_del():
    raw = f"a{SGR_RED}b{CSI_ERASE}c\x01\x7f\x9cd"
    assert sanitize_text(raw) == "abcd"


def test_sanitize_text_keeps_printable_unicode():
    text = "caf\u00e9 \u4e2d\u6587 \U0001f4f6"
    assert sanitize_text(text) == text


def test_sanitize_styled_keeps_sgr_only():
    raw = f"\x1b[38;2;1;2;3mok{OSC_TITLE}{CSI_ERASE}x\x01"
    assert sanitize_styled(raw) == "\x1b[38;2;1;2;3mokx"


def test_strip_ansi_removes_all_terminal_commands():
    raw = f"a{OSC_TITLE}b{CSI_ERASE}c\x01"
    assert Text(raw).strip_ansi() == "abc"


def test_window_append_line_strips_terminal_commands():
    win = Window(0, 0, 40, 1)
    win.append_line(f"safe{OSC_TITLE} text{CSI_ERASE}!")
    line = win.buffer[0]
    assert "\x1b]" not in line
    assert CSI_ERASE not in line
    assert Text(line).strip_ansi().strip() == "safe text!"


def test_window_update_buffer_strips_terminal_commands():
    win = Window(0, 0, 40, 1)
    win.update_buffer([f"safe{OSC_CLIP} text{CSI_ERASE}"])
    line = win.buffer[0]
    assert "\x1b]" not in line
    assert CSI_ERASE not in line
    assert Text(line).strip_ansi().strip() == "safe text"


def test_table_cell_strips_terminal_commands_from_ssid():
    root = StyledWindow(0, 0, 20, 2)
    table = Table(root, [10])
    out = table.format_cell(f"AP{OSC_TITLE}", Table.DEFAULT_FORMAT,
                            table.columns[0])
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "AP" in out


def test_ap_detail_sanitizes_ssid():
    view = ScanView()
    win = StyledWindow(0, 0, 40, 20)
    view._detail_win = win
    ap = ApRecord(bssid="aa:bb:cc:00:00:01",
                  ssid=f"EVIL{SGR_RED}{OSC_TITLE}",
                  security="open", cipher="none", pmf="none",
                  phy="HT", country="US", max_rate=100.0, n_ss=1)
    view._render_ap_detail(ap)
    blob = "\n".join(win.buffer)
    assert "EVIL" in blob
    assert SGR_RED not in blob
    assert "\x1b]" not in blob


def test_station_detail_sanitizes_probed_ssids():
    view = ScanView()
    win = StyledWindow(0, 0, 40, 20)
    view._detail_win = win
    sta = StaRecord(mac="aa:bb:cc:dd:ee:ff",
                    probed_ssids=[f"PROBE{SGR_RED}{OSC_TITLE}"],
                    frame_count=1)
    view._render_sta_detail(sta)
    blob = "\n".join(win.buffer)
    assert "PROBE" in blob
    assert SGR_RED not in blob
    assert "\x1b]" not in blob


def test_fm_displays_id3_title_as_plain_text():
    track = Track("song.mp3",
                  tags=TrackTags(title=f"TITLE{SGR_RED}{OSC_TITLE}"))
    assert track.display_title == "TITLE"


def test_shell_sanitizes_command_problem():
    shell = object.__new__(Shell)
    shell._ui_thread = threading.get_ident()
    shell._input_bar = None
    shell._problem(f"bad{SGR_RED}{OSC_TITLE}")
    assert shell._result_message == "bad"
