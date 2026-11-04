"""Status app: device and signal overview.

Default landing screen. Shows the logo, a Status panel with signal
health and device identity, and an overview of the available apps.
"""
from __future__ import annotations
import os

from .pytui import StyledWindow, Window, Text, display_width
from .app import App, View
from .theme import DEFAULT_PALETTE
from ..protocol import DaemonMode

with open(os.path.join(os.path.dirname(__file__), 'status_splash.ans')) as _f:
    _LOGO_LINES = _f.read().splitlines()

# RESP_CAPS feature bit: fabric 802.11 decode present, i.e. the deimos
# bitstream.  Only deimos sets it; base styx leaves it clear.
_FEATURE_FABRIC_WIFI = 1 << 2


class SplashView(View):
    """Device status overview: signal health, bitstream, apps."""
    name = 'splash'
    hints = ''
    mode = DaemonMode.WATERFALL

    # SNR mapped to a full meter, and how the display is smoothed.
    _SNR_FULL_DB = 80.0
    _SNR_SMOOTH = 0.2          # EMA weight for each new PSD reading
    _PEAK_DECAY_DB_S = 4.0     # peak-hold marker fall rate

    def __init__(self) -> None:
        super().__init__()
        self.style = DEFAULT_PALETTE
        self._win: StyledWindow | None = None
        self._width = 0
        self._height = 0
        self._dirty = True

        # Signal health, smoothed so the digits do not flicker.
        self._noise_floor: float = -90.0
        self._peak_signal: float = -90.0
        self._snr: float = 0.0
        self._snr_peak: float = 0.0
        self._have_snr = False

        # Device identity.
        self._bitstream = ''
        self._accelerators: list[tuple[str, bool]] = []

        # App overview: (name, one-line description).
        self._apps: list[tuple[str, str]] = []

    def set_apps(self, apps: list[tuple[str, str]]) -> None:
        """Hand the view the app registry for the overview panel."""
        self._apps = list(apps)
        self._dirty = True

    def app_registry_changed(self, apps: dict[str, 'App']) -> None:
        """Show the shell's apps in the overview panel."""
        self.set_apps([(name, app.description) for name, app in apps.items()])

    def layout(self, container: StyledWindow) -> list[Window]:
        self._win = StyledWindow(
            container.x, container.y, container.width, container.height,
            {'bg': self.style['bg'], 'fg': self.style['fg']})
        self._width = container.width
        self._height = container.height
        self._render()
        return [self._win]

    def update(self, frame: dict) -> None:
        if frame.get('type') != 'psd':
            return
        if frame.get('bins'):
            self._noise_floor = float(
                frame.get('db_floor', self._noise_floor))
            self._peak_signal = float(
                frame.get('db_ceil', self._peak_signal))
            instant = self._peak_signal - self._noise_floor
            if self._have_snr:
                self._snr += self._SNR_SMOOTH * (instant - self._snr)
            else:
                self._snr = instant
                self._have_snr = True
            self._snr_peak = max(self._snr_peak, self._snr)
        self._dirty = True

    def set_device(self, *, features: int) -> None:
        """Set the bitstream identity from RESP_CAPS.

        ``features`` bit2 marks the deimos fabric-wifi bitstream; base
        styx leaves it clear and advertises no accelerator.
        """
        fabric_wifi = bool(features & _FEATURE_FABRIC_WIFI)
        self._bitstream = 'deimos' if fabric_wifi else 'styx'
        self._accelerators = [('wifi', True)] if fabric_wifi else []
        self._dirty = True

    def tick(self, dt: float) -> None:
        if self._snr_peak > self._snr:
            self._snr_peak = max(
                self._snr, self._snr_peak - self._PEAK_DECAY_DB_S * dt)
        if self._dirty:
            self._render()
            self._dirty = False

    def _render(self) -> None:
        if not self._win:
            return
        w = self._width
        h = self._height
        bg = self.style['bg']
        fg = self.style['fg']
        accent = self.style['accent']
        dim = self.style['dim']

        lines: list[str] = []

        def seg(text: str, color=fg, bold: bool = False) -> str:
            opts: dict = {'fg': color, 'bg': bg}
            if bold:
                opts['bold'] = True
            return Text(text).style(opts)

        def blank() -> None:
            lines.append(seg(' ' * w))

        def push(row: str) -> None:
            pad = w - display_width(row)
            if pad > 0:
                row += seg(' ' * pad)
            lines.append(self._win.justify_line(row, 'left'))

        def pad_to(row: str, width: int) -> str:
            pad = width - display_width(row)
            return row + (seg(' ' * pad) if pad > 0 else '')

        def panel_title(text: str, width: int) -> str:
            head = seg(f' \u2500 {text} ', accent, bold=True)
            return self._win.truncate_line(
                head + seg('\u2500' * width, dim), width)

        def field(lbl: str, value: str, color=fg,
                  label_w: int = 10) -> str:
            return seg(f' {lbl:<{label_w}}', dim) + seg(value, color)

        def panel(title: str, rows: list[str], width: int) -> list[str]:
            return [panel_title(title, width)] + [pad_to(r, width)
                                                  for r in rows]

        # ── Logo header ──────────────────────────────────────────────
        blank()
        bg_code = ';48;2;%d;%d;%dm' % bg
        for logo_line in _LOGO_LINES:
            line = logo_line.replace(';40m', bg_code)
            visible = len(Text(line).strip_ansi())
            pad = max(0, (w - visible) // 2)
            lines.append(' ' * pad + line
                         + seg(' ' * max(0, w - pad - visible)))
        blank()

        # ── Device / Signal panels, side by side ─────────────────────
        left_w = max(20, (w - 2) // 2)
        right_w = w - left_w

        bar_w = max(6, left_w - 22)
        frac = max(0.0, min(1.0, self._snr / self._SNR_FULL_DB))
        held = max(0.0, min(1.0, self._snr_peak / self._SNR_FULL_DB))
        filled = int(round(frac * bar_w))
        held_cell = int(round(held * bar_w))
        bar = []
        for i in range(bar_w):
            if i < filled:
                bar.append(seg('\u2588', accent))
            elif i == held_cell and held_cell > filled:
                bar.append(seg('\u2588', fg))       # peak-hold marker
            else:
                bar.append(seg('\u2591', dim))

        signal_rows = [
            seg(f' {"SNR":<11}', dim) + ''.join(bar)
            + seg(f' {self._snr:5.1f} dB', fg),
            field('floor/peak',
                  f'  {self._noise_floor:.1f}/{self._peak_signal:.1f} dB'),
        ]

        daemon = self.daemon
        if daemon is not None:
            connected = bool(daemon.connected)
            host = str(daemon.host_display)
        else:
            connected, host = False, ''
        state = 'connected' if connected else 'offline'
        ready = [name for name, ok in self._accelerators if ok]
        bitstream = (f'{self._bitstream} '
                     f'({", ".join(ready) if ready else "none"})'
                     if self._bitstream else '-')
        device_rows = [
            field(state, f' {host}', fg if connected else dim),
            field('bitstream', f' {bitstream}', fg if self._bitstream else dim),
        ]

        signal_panel = panel('Signal', signal_rows, right_w)
        device_panel = panel('Device', device_rows, left_w)
        for i in range(max(len(signal_panel), len(device_panel))):
            left = device_panel[i] if i < len(device_panel) else seg(' ' * left_w)
            right = signal_panel[i] if i < len(signal_panel) else seg(' ' * right_w)
            push(left + right)

        # ── Apps panel ───────────────────────────────────────────────
        if self._apps:
            blank()
            title = panel_title('Apps', w)
            push(title)
            app_col_w = w // 2 - 2
            half = (len(self._apps) + 1) // 2
            for row in range(half):
                cells = []
                for idx in (row, row + half):
                    if idx >= len(self._apps):
                        continue
                    app_name, desc = self._apps[idx]
                    if idx == row:
                        cell = (seg(f':{app_name:<10}', accent)
                                + seg(desc, fg))
                    else:
                        cell = (seg(f' :{app_name:<9}', accent)
                                + seg(' ' + desc, fg))
                    gap = app_col_w - display_width(cell)
                    cells.append(cell + (seg(' ' * gap) if gap > 0 else ''))
                push(seg(' ') + ''.join(cells).rstrip())

        while len(lines) < h:
            blank()

        self._win.update_buffer(lines[:h])

    def config_changed(self, key: str, daemon) -> None:
        if key == 'connection':
            self._dirty = True
        elif key == 'features':
            state = daemon.get_state()
            self.set_device(
                features=int(float(state.get('features', 0) or 0)))

    def theme_changed(self, style: dict) -> None:
        self.style = style
        if self._win:
            self._win.update_style({'bg': style['bg'], 'fg': style['fg']})


class StatusApp(App):
    """Default app. Status splash screen."""
    name = 'status'
    description = 'device, signal status'
    default_view = 'splash'

    def __init__(self) -> None:
        super().__init__()
        self.views = {'splash': SplashView()}
