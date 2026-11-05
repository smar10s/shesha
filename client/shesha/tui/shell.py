"""Vim-modal TUI shell — main event loop, input routing, chrome."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from typing import Callable
import os
import queue
import struct
import threading
import time

from .pytui import (
    Screen, StyledWindow, Window, Text, Keyboard, sanitize_text,
)
from .animate import Animator
from .theme import STYLES, DEFAULT_STYLE
from .app import App, View
from .config import Config, KeySpec
from .daemon import Daemon, SheshaDaemon
from .dummy import DummyDaemon
from .status_bar import StatusBar
from .clip_heat import HeatDriver
from .command_model import Command
from .commands import CommandDispatcher
from ..channels import CHANNEL_TO_FREQ
from .fmt import parse_freq
from .input import InputHandler
from .event_loop import EventLoop


def command_line(buffer: str, cursor: int, style: dict) -> str:
    """Render the command buffer with a reverse-video cursor cell."""
    before = buffer[:cursor]
    after = buffer[cursor:]
    cursor_char = after[:1] or ' '
    tail = after[1:]
    base = {'fg': style['input_active']}
    return (
        Text(f':{before}').style(base)
        + Text(cursor_char).style({**base, 'negative': True})
        + Text(tail).style(base)
    )


@dataclass(frozen=True)
class GlobalKey:
    """Keys that run one action, plus the text the help page shows."""

    keys: tuple[str, ...]
    description: str
    handler: Callable[[int, bool], None]
    label: str = ''


class Shell:
    """Main TUI shell — owns screen, apps, input routing, status bar."""

    # Heartbeats are answered sub-second, so treat a link with no bytes for
    # this long as dead.  Ethernet-over-USB is fast and lossless; a
    # generous timeout only delays the red.
    _DISCONNECT_TIMEOUT = 1.0

    # The supported terminal floor.  Below it the shell shows a message and
    # keeps running, so shrinking a window never kills the session.
    _MIN_COLS = 80
    _MIN_ROWS = 24

    # name -> (config key, argument syntax, description)
    _CONFIG_SHORTCUTS: dict[str, tuple[str, str, str]] = {
        'freq': ('freq', '<MHz>', 'Set center frequency'),
        'bw': ('bw', '<MHz>', 'Set bandwidth'),
        'gain': ('gain', '<val>', 'Set gain (or fast/slow/auto)'),
        'fft_size': ('viz.waterfall.fft_size', '<n>', 'Set waterfall FFT size'),
        'sample_rate': ('sample_rate', '<MHz>', 'Set sample rate'),
        'tx_gain': ('tx_gain', '<dB>', 'Set TX gain'),
        'workers': ('workers', '<1|2>', 'Set worker threads'),
    }

    def __init__(self, apps: list[App], config: Config, daemon: Daemon,
                 project_dir: str | None = None, clock=None) -> None:
        self.config = config
        self.daemon = daemon
        self._project_dir = project_dir

        # One owner for TUI state.  Other threads post closures here.
        self._ui_thread = threading.get_ident()
        self._actions: queue.SimpleQueue[Callable[[], None]] = \
            queue.SimpleQueue()
        self._lifecycle_active: str | None = None

        # App registry
        self.apps: dict[str, App] = {a.name: a for a in apps}
        for app in self.apps.values():
            app.validate()
        config.register_keys({'app.view': KeySpec('str', default='')})

        # Wire daemon change notifications
        daemon.set_ui_thread(self._ui_thread)
        daemon.on_change(self._on_daemon_change)

        # Theme — a hand-edited config may name a palette that does not
        # exist, and a cosmetic setting must never block startup.
        self.style = STYLES.get(config.get('theme'), STYLES[DEFAULT_STYLE])

        # Push config, daemon, theme, and animators to all pre-existing views
        for app in self.apps.values():
            for view in app.views.values():
                view.config = config
                view.daemon = daemon
                view.theme_changed(self.style)
                view.animator = Animator()
                view.app_registry_changed(self.apps)

        # Screen and animator
        self.screen = Screen()
        self._animator = Animator()
        self._status_bar = StatusBar(animator=self._animator, style=self.style,
                                     clock=clock)
        self._heat = HeatDriver(self._animator)
        self._sync_daemon_anim()

        # Help overlay
        from .help import HelpView
        self._help_active = False
        self._help_view = HelpView(schema=config._schema, apps=self.apps)
        self._global_keys = self._build_global_key_defs()
        self._global_key_bindings = {
            key: binding
            for binding in self._global_keys
            for key in binding.keys
        }

        # Input state
        self._input = InputHandler(keys={
            'escape': Keyboard.ESCAPE,
            'enter': Keyboard.ENTER,
            'tab': Keyboard.TAB,
            'shift_tab': Keyboard.SHIFT_TAB,
            'backspace': Keyboard.BACKSPACE,
            'delete': Keyboard.DELETE,
            'home': Keyboard.HOME,
            'end': Keyboard.END,
            'up': Keyboard.UP,
            'down': Keyboard.DOWN,
            'left': Keyboard.LEFT,
            'right': Keyboard.RIGHT,
        })

        # Command dispatcher (owns resolution, completions, history)
        self._app_commands = {
            name: self._build_app_command(app)
            for name, app in self.apps.items()
        }
        self._commands = CommandDispatcher(
            self._build_global_commands(),
            lambda: self._app_commands.get(self._active_app))

        # Seed history from config
        for item in config.get('cmd_history', []):
            if isinstance(item, str):
                self._commands._command_history.append(item)
        max_len = config.get_int('cmd_history_length', 100)
        if max_len > 0 and len(self._commands._command_history) > max_len:
            config.set_raw('cmd_history', list(self._commands._command_history)[-max_len:])
        if max_len <= 0:
            config.delete_key('cmd_history')

        # Misc state
        self._result_message = ''
        self._result_problem = False
        self._running = False
        self._shutdown_done = False
        self._last_manual_gain = daemon.get_state().get('gain', '50')
        self._last_tx: tuple[str, list[str]] | None = None  # (app_name, args) for toggle replay

        # Recording state
        self._recording = False
        self._rec_file: object | None = None  # opened file handle
        self._rec_filename = ''
        self._rec_eapol = False
        self._rec_frames = 0
        self._rec_label = ''
        self._last_record: tuple[str, list[str]] | None = None

        # Active app — fall back to first in list; overridden below if config has app.view
        self._active_app = apps[0].name if apps else ''

        # Navigation memory (in-memory only): Tab toggles to the previous
        # app, restoring the view last used in each app.
        self._prev_app: str = ''
        self._last_view: dict[str, str] = {}

        # Layout window slots (set during _build_layout)
        self._status_left: StyledWindow | None = None
        self._status_middle: StyledWindow | None = None
        self._status_right: StyledWindow | None = None
        self._rec_left: StyledWindow | None = None
        self._rec_right: StyledWindow | None = None
        self._body: StyledWindow | None = None
        self._input_bar: StyledWindow | None = None

        # Restore saved app/view from config
        saved_view = config.get('app.view')
        if saved_view:
            parts = saved_view.split(':', 1)
            if parts[0] in self.apps:
                self._active_app = parts[0]
                if len(parts) > 1:
                    app = self.active_app
                    if app and parts[1] in app.views:
                        self._sync_view(app.views[parts[1]])
                        app.switch_view(parts[1])
                        self._apply_view_mode(app.active_view)

    # ─── Properties ──────────────────────────────────────────────────────

    @property
    def active_app(self) -> App | None:
        return self.apps.get(self._active_app)

    # ─── Layout ──────────────────────────────────────────────────────────

    def _build_layout(self, root: StyledWindow) -> list[Window]:
        """Build the layout: optional rec bar, status bar, body, input bar."""
        if root.width < self._MIN_COLS or root.height < self._MIN_ROWS:
            return self._build_too_small(root)
        rec_height = 1 if self._recording else 0

        base_parts = [1]  # status
        if rec_height:
            base_parts.append(rec_height)
        base_parts.append(None)  # body
        base_parts.append(1)     # input

        parts = root.hsplit(*base_parts)
        idx = 0
        status = parts[idx]; idx += 1

        if rec_height:
            rec_bar = parts[idx]; idx += 1
            rec_left, rec_right = rec_bar.vsplit(0.50)
            rec_left.update_style({'bg': self.style['header_bg'],
                                   'fg': self.style['header_fg']})
            rec_right.update_style({'bg': self.style['header_bg'],
                                    'fg': self.style['header_fg']})
            self._rec_left = rec_left
            self._rec_right = rec_right
        else:
            self._rec_left = None
            self._rec_right = None

        body = parts[idx]; idx += 1
        input_bar = parts[idx]

        # Split status bar into 3 panes (allocated once, reused every frame)
        left, middle, right = status.vsplit(0.40, None, 0.40)
        left.update_style({'bg': self.style['header_bg'], 'fg': self.style['header_fg']})
        middle.update_style({'bg': self.style['header_bg'], 'fg': self.style['header_fg']})
        right.update_style({'bg': self.style['header_bg'], 'fg': self.style['header_fg']})
        body.update_style({'bg': self.style['bg'], 'fg': self.style['fg']})
        input_bar.update_style({'bg': self.style['input_bg'], 'fg': self.style['input_fg']})

        self._status_left = left
        self._status_middle = middle
        self._status_right = right
        self._body = body
        self._input_bar = input_bar

        # Build body from help overlay or active view
        windows = [left, middle, right, input_bar]
        if self._rec_left and self._rec_right:
            windows.extend([self._rec_left, self._rec_right])

        if self._help_active:
            self._help_view.render(body)
            windows.append(body)
        else:
            app = self.active_app
            if app and app.active_view:
                windows.extend(app.active_view.layout(body))
            else:
                windows.append(body)

        self._update_status_bar()
        self._update_input_bar()

        return windows

    def _build_too_small(self, root: StyledWindow) -> list[Window]:
        """Keep the chrome minimal and report the size instead of raising."""
        self._status_left = None
        self._status_middle = None
        self._status_right = None
        self._rec_left = None
        self._rec_right = None
        self._input_bar = None

        body = root
        windows: list[Window] = []
        if root.height >= 3:
            _, body, input_bar = root.hsplit(1, None, 1)
            input_bar.update_style({'bg': self.style['input_bg'],
                                    'fg': self.style['input_fg']})
            self._input_bar = input_bar
            windows.append(input_bar)

        body.update_style({'bg': self.style['bg'], 'fg': self.style['fg']})
        self._body = body
        message = (f'terminal too small: {root.width}x{root.height} '
                   f'(need {self._MIN_COLS}x{self._MIN_ROWS})')
        middle = body.height // 2
        lines = []
        for row in range(body.height):
            text = (Text(message).style({'fg': self.style['warn']})
                    if row == middle else '')
            lines.append(body.justify_line(text, 'center'))
        body.update_buffer(lines)
        self._update_input_bar()
        return [body, *windows]

    def _relayout(self) -> None:
        """Rebuild the layout tree. Syncs daemon state to active view first."""
        self._require_ui_thread()
        self._sync_active_view()
        self.screen.layout(self._build_layout)

    def _sync_active_view(self) -> None:
        """Push current daemon state to the active view before layout."""
        app = self.active_app
        if app and app.active_view:
            self._sync_view(app.active_view)

    def _sync_view(self, view: View) -> None:
        """Push current daemon state to a view so it has daemon ref before on_enter."""
        state = self.daemon.get_state()
        for key in state:
            view.config_changed(key, self.daemon)
        if view.animator is None:
            view.animator = Animator()

    def _apply_view_mode(self, view: View | None) -> None:
        """Push the view's required daemon mode, if it declares one."""
        if view is not None and view.mode is not None:
            self.daemon.set_mode(view.mode)

    def _install_daemon(self, daemon: Daemon) -> None:
        """Install a daemon prepared off-thread. UI thread only."""
        self._require_ui_thread()
        old = self.daemon
        if isinstance(old, SheshaDaemon):
            old._close()
        self.daemon = daemon
        daemon.set_ui_thread(self._ui_thread)
        daemon.on_change(self._on_daemon_change)
        for app in self.apps.values():
            for view in app.views.values():
                view.daemon = daemon
        app = self.active_app
        if app is not None and app.active_view is not None:
            self._apply_view_mode(app.active_view)
        self._relayout()
        self._sync_daemon_anim()

    def _deactivate_current_view(self) -> None:
        """Call on_exit() on the current app's active view before switching apps."""
        app = self.active_app
        if app and app.active_view:
            app.active_view.active = False
            app.active_view.on_exit()

    # ─── Status Bar ──────────────────────────────────────────────────────

    def _update_status_bar(self) -> None:
        if self._status_left is None:
            return

        app = self.active_app
        if app and app.active_view:
            app_view_text = f'{app.name}:{app.active_view.name}'
        elif app:
            app_view_text = app.name
        else:
            app_view_text = ''

        state = self.daemon.get_state()
        self._status_bar.update(
            left=self._status_left,
            middle=self._status_middle,
            right=self._status_right,
            state=state,
            busy=self.daemon.busy or self._lifecycle_active is not None,
            connected=self.daemon.connected,
            app_view_text=app_view_text,
        )

        if self._rec_left and self._rec_right:
            self._status_bar.update_rec(
                left=self._rec_left, right=self._rec_right,
                style=self.style, label=self._rec_label,
                frames=self._rec_frames)

    # ─── Input Bar ───────────────────────────────────────────────────────

    def _update_input_bar(self) -> None:
        if self._input_bar is None:
            return

        if self._result_message:
            color = self.style['warn'] if self._result_problem else self.style['fg']
            content = Text(f' {self._result_message}').style({'fg': color})
            self._input_bar.update_buffer([self._input_bar.justify_line(content, 'left')])
        elif self._input.command_mode:
            self._input_bar.update_buffer([
                self._input_bar.justify_line(
                    command_line(self._input.command_buf,
                                 self._input.cursor, self.style),
                    'left')])
        else:
            # Normal mode - show count buf or hints
            if self._input.count_buf:
                content = Text(f' {self._input.count_buf}').style({'fg': self.style['dim']})
            else:
                app = self.active_app
                hints = ''
                if app and app.active_view and app.active_view.hints:
                    hints = app.active_view.hints
                content = Text(f' {hints}').style({'fg': self.style['dim']})
            self._input_bar.update_buffer([self._input_bar.justify_line(content, 'left')])

    # ─── Command Result Display ──────────────────────────────────────────
    #
    # The line under the ':' prompt shows the result of the last command:
    # a query answer (normal colour) or a problem (warn).  Ambient-state
    # acknowledgements emit nothing — the glyph or app state says it.  See
    # docs/tui.md.

    def _result(self, msg: str) -> None:
        """Show a command answer (informational, normal colour)."""
        self._require_ui_thread()
        self._result_message = sanitize_text(msg)
        self._result_problem = False
        self._update_input_bar()

    def _problem(self, msg: str) -> None:
        """Show a command problem (failed or refused, warn colour)."""
        self._require_ui_thread()
        self._result_message = sanitize_text(msg)
        self._result_problem = True
        self._update_input_bar()

    def _require_ui_thread(self) -> None:
        if threading.get_ident() != self._ui_thread:
            raise RuntimeError('TUI state mutated outside the UI thread')

    def _post(self, action: Callable[[], None]) -> None:
        """Queue work for the UI thread. Safe from any thread."""
        self._actions.put(action)

    def _drain_actions(self) -> None:
        """Run queued work. UI thread only."""
        self._require_ui_thread()
        while True:
            try:
                action = self._actions.get_nowait()
            except queue.Empty:
                return
            try:
                action()
            except Exception as exc:
                self._problem(f'{type(exc).__name__}: {exc}')

    def _enqueue_key(self, key: str) -> None:
        """Keyboard listener: hand the key to the UI thread."""
        self._post(lambda k=key: self._handle_key(k))

    def _clear_result(self) -> None:
        self._result_message = ''
        self._result_problem = False
        self._update_input_bar()

    # ─── Daemon Change Handler ───────────────────────────────────────────

    def _on_daemon_change(self, key: str) -> None:
        if key == '_error' and self.daemon.last_error:
            self._problem(self.daemon.last_error)
            return

        if key == '_sent':
            for k in self.daemon.last_sent:
                if k in self._RADIO_PULSE:
                    self._animator.pulse(f'status_{k}', frequency=1.0,
                                         min_val=0.4, max_val=1.0, phase_offset=0.25)
            self._update_status_bar()
            return

        if key == '_acked':
            # Command answered (ok or error): stop the send pulse, even if
            # the confirmed value didn't change (agc switches leave gain_db
            # alone — the status push would never settle it).
            settle_key = self.daemon.last_acked
            if settle_key and self._animator.running(f'status_{settle_key}'):
                self._animator.settle_pulse(f'status_{settle_key}')
            return

        if key == '_status':
            state = self.daemon.get_state()
            try:
                clips = int(state.get('clip_count', '0'))
                samples = int(state.get('sample_count', '0'))
            except (TypeError, ValueError):
                clips = samples = 0
            self._heat.update(clips, samples)

        if key in self._RADIO_PULSE and self._animator.running(f'status_{key}'):
            self._animator.settle_pulse(f'status_{key}')

        # Sync FPGA animation with daemon state.  Always retarget — even
        # mid-animation — or a dropped update leaves the pane stuck on
        # the wrong color (the 'fpga off but still red' bug).
        if key == 'fpga_active':
            active = self.daemon.get_state().get('fpga_active', '0') == '1'
            target = 1.0 if active else 0.0
            self._animator.to('fpga_active', target, 0.4, 'ease_out')

        self._update_status_bar()

        # Notify active view
        app = self.active_app
        if app and app.active_view:
            app.active_view.config_changed(key, self.daemon)

        # Connection activity / link animations
        if key == 'connection':
            self._sync_daemon_anim()

    def _sync_daemon_anim(self) -> None:
        """Drive the daemon glyph from (busy, connected).

        Busy (any daemon/connection operation in flight) -> fast pulse.
        Idle and connected -> slow throb.  Idle and disconnected -> no
        animation, the glyph sits at steady warn.  See docs/tui.md.
        """
        if self._lifecycle_active is not None or self.daemon.busy:
            self._animator.pulse('daemon', frequency=2.5, min_val=0.3, max_val=1.0)
        elif self.daemon.connected:
            self._animator.pulse('daemon', frequency=0.5, min_val=0.3, max_val=1.0)
        else:
            self._animator.stop('daemon')

    _AGC_LOCK_NAMES = {1: 'fast_attack', 2: 'slow_attack'}

    def _check_radio(self, key: str, value: str) -> bool:
        """Refuse values the active producer will not accept as inputs.

        The daemon advertises the active mode's policy in RESP_STATUS, so
        the rules live in one place (the firmware) rather than in each view.
        A mode can fix bw, fix or derive sample_rate, and pin the AGC mode;
        each is checked here so an invalid change fails with a clear message
        before it is persisted.
        """
        policy = self.daemon.radio_policy()
        if policy is None:
            return True
        if key == 'gain':
            if not policy.agc_locked:
                return True
            name = self._AGC_LOCK_NAMES.get(policy.agc_fixed, 'fixed')
            self._problem(f'gain locked to {name} by this mode')
            return False
        if key not in ('bw', 'sample_rate'):
            return True
        try:
            hz = int(float(value))
        except (ValueError, TypeError):
            return True          # config validation owns this message
        if key == 'bw':
            if policy.allows_bw(hz):
                return True
            if policy.bw_fixed:
                self._problem(f'bw fixed at {policy.bw_fixed} Hz by this mode')
            else:
                self._problem(
                    f'bw {hz} outside {policy.bw_min}-{policy.bw_max} Hz')
            return False
        if policy.allows_sr(hz):
            return True
        if policy.sr_fixed:
            self._problem(
                f'sample_rate fixed at {policy.sr_fixed} Hz by this mode')
        elif policy.sr_independent:
            self._problem(
                f'sample_rate {hz} outside {policy.sr_min}-{policy.sr_max} Hz')
        else:
            self._problem('sample_rate is derived from bw in this mode')
        return False

    def _apply_config(self, key: str, value: str) -> None:
        """Set config value with policy-checking. Radio keys go to the daemon.

        The daemon gets the canonical form: ``:set viz.waterfall.window hann``
        must send the wire number, not the name.  Views get the value as typed,
        since display prefs key off the symbolic form (e.g. avg_mode).
        """
        if not self._check_radio(key, value):
            return
        self.config.set(key, value)
        wire = self.config.get_normalized(key)
        self.daemon.apply_config(key, wire if wire is not None else value)
        for app in self.apps.values():
            for view in app.views.values():
                view.config_updated(key, value)
        if key == 'cmd_history_length':
            self._on_history_length_changed()

    def _apply_theme(self, name: str) -> None:
        """Switch to a named theme. Persists to config. Notifies all views."""
        if name not in STYLES:
            return
        self.style = STYLES[name]
        self._status_bar.style = self.style
        self.config.set('theme', name)
        for app in self.apps.values():
            for view in app.views.values():
                view.theme_changed(self.style)
        self._relayout()

    def _persist_state(self) -> None:
        """Write daemon-confirmed radio state back to config on exit.

        Config is a write-only seed for the next fresh daemon; the last
        known hardware truth is the best seed there is.  The key set is
        derived from the schema's confirmed keys.
        """
        state = self.daemon.get_state()
        for key in self.config.confirmed_keys():
            if key in state:
                try:
                    self.config.persist(key, state[key])
                except ValueError:
                    pass

    def shutdown(self) -> None:
        """Clean teardown shared by quit and Ctrl-C.

        The active view's on_exit() runs first so view-owned TX streams
        stop through their own state machine; any remaining TX (e.g. a
        beacon) is then stopped directly.  Every step is best effort so a
        failure in one cannot skip the others.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self._record_stop()
        except Exception:
            pass
        try:
            self._deactivate_current_view()
        except Exception:
            pass
        try:
            if self.daemon.get_state().get('tx_busy', '0') == '1':
                self.daemon.tx_stop()
        except Exception:
            pass
        try:
            self._persist_state()
        except Exception:
            pass

    def _quit(self) -> None:
        """Clean exit: stop TX if active, persist state, signal loop stop."""
        self.shutdown()
        self._running = False

    def _handle_key(self, key: str) -> None:
        """Top-level key dispatch via InputHandler. UI thread only."""
        self._require_ui_thread()
        self._clear_result()
        result = self._input.handle_key(key)

        match result.kind:
            case 'enter_command_mode':
                if self._help_active:
                    self._help_active = False
                    self._relayout()
                self._history_index = -1
                self._commands.reset_tab_state()
            case 'exit_command_mode' | 'exit_on_empty_bs':
                self._commands.reset_tab_state()
                self._commands._history_index = -1
                self._commands._saved_input = ''
            case 'execute':
                self._commands.reset_tab_state()
                self._push_history(result.command)
                self._exec_command(result.command)
            case 'backspace' | 'delete' | 'cursor':
                self._commands.reset_tab_state()
            case 'typing':
                self._commands.reset_tab_state()
            case 'tab':
                self._on_tab(reverse=result.reverse)
            case 'history_back':
                self._history_back()
            case 'history_forward':
                self._history_forward()
            case 'normal_action':
                self._dispatch_normal_action(result.key, result.count,
                                             result.had_digits)
            case 'clear':
                # Count cleared by InputHandler. Escape also dismisses help.
                if self._help_active:
                    self._help_active = False
                    self._relayout()
            case 'noop':
                pass

        self._update_input_bar()

    _RADIO_PULSE = {'freq', 'gain', 'bw'}

    def _build_global_key_defs(self) -> list[GlobalKey]:
        """One row per action; aliases share a row in the help page."""
        return [
            GlobalKey(('q',), 'Quit', lambda c, d: self._quit()),
            GlobalKey((Keyboard.TAB,), 'Toggle to previous app',
                      lambda c, d: self._previous_app()),
            GlobalKey(('`',), 'Cycle views in current app',
                      lambda c, d: self._cycle_view(1)),
            GlobalKey(('h', Keyboard.LEFT),
                      'Tune frequency down by 1 MHz',
                      lambda c, d: self._tune_freq(-c)),
            GlobalKey(('l', Keyboard.RIGHT),
                      'Tune frequency up by 1 MHz',
                      lambda c, d: self._tune_freq(c)),
            GlobalKey(('H',), 'Tune frequency down by bandwidth',
                      lambda c, d: self._tune_by_bandwidth(-c)),
            GlobalKey(('L',), 'Tune frequency up by bandwidth',
                      lambda c, d: self._tune_by_bandwidth(c)),
            GlobalKey(('f',), 'Set frequency to <n> MHz',
                      lambda c, d: self._set_from_count('freq', c, d),
                      label='<n> f'),
            GlobalKey(('b',), 'Set bandwidth to <n> MHz',
                      lambda c, d: self._set_from_count('bw', c, d),
                      label='<n> b'),
            GlobalKey(('g',), 'Toggle AGC; <n> sets gain',
                      self._gain_key, label='g / <n> g'),
            GlobalKey(('t',), 'Toggle TX', lambda c, d: self._tx_toggle()),
            GlobalKey(('r',), 'Toggle recording',
                      lambda c, d: self._record_toggle()),
            GlobalKey(('?',), 'Toggle help (this screen)',
                      lambda c, d: self._help_toggle()),
        ]

    def _previous_app(self) -> None:
        if self._prev_app in self.apps and self._prev_app != self._active_app:
            self._switch_app(self._prev_app)
        else:
            self._problem('no previous app')

    def _tune_by_bandwidth(self, steps: int) -> None:
        bw = int(float(self.daemon.get_state().get('bw', '20000000')))
        self._tune_freq(steps * bw // 1_000_000)

    def _set_from_count(self, key: str, count: int, had_digits: bool) -> None:
        if had_digits:
            self._apply_config(key, str(count * 1_000_000))

    def _gain_key(self, count: int, had_digits: bool) -> None:
        if had_digits:
            self._apply_config('gain', str(count))
            self._last_manual_gain = str(count)
            return
        ds = self.daemon.get_state()
        if ds.get('agc_mode', 'fast') == 'manual':
            gain = ds.get('gain', '')
            try:
                float(gain)
                self._last_manual_gain = gain
            except (ValueError, TypeError):
                pass
            self._apply_config('gain', 'fast')
        else:
            self._apply_config('gain', self._last_manual_gain or '50')

    def _help_toggle(self) -> None:
        self._help_active = not self._help_active
        if self._help_active:
            app = self.active_app
            self._help_view.build_content(
                app_name=app.name if app else '',
                view_name=app.active_view.name if app and app.active_view else '',
                global_keys=self._global_keys,
                global_commands=self._commands.root,
                app_commands=self._app_commands)
        self._relayout()

    def _dispatch_normal_action(self, key: str, count: int,
                                had_digits: bool) -> None:
        """Execute a normal-mode action key."""
        binding = self._global_key_bindings.get(key)
        if binding is not None:
            binding.handler(count, had_digits)
            return

        if self._help_active:
            if key == 'j' or key == Keyboard.DOWN:
                self._help_view.scroll(1, self._body.height if self._body else 10,
                                       self._body)
                return
            if key == 'k' or key == Keyboard.UP:
                self._help_view.scroll(-1, self._body.height if self._body else 10,
                                       self._body)
                return
            if key == Keyboard.PAGE_DOWN:
                self._help_view.scroll(
                    self._body.height if self._body else 10,
                    self._body.height if self._body else 10,
                    self._body)
                return
            if key == Keyboard.PAGE_UP:
                self._help_view.scroll(
                    -(self._body.height if self._body else 10),
                    self._body.height if self._body else 10,
                    self._body)
                return
            # Dismiss on any other key
            self._help_active = False
            self._relayout()
            return

        # Fall through to active view's keybinds
        app = self.active_app
        if app and app.active_view:
            binds = app.active_view.keybinds()
            if key in binds:
                binds[key](count)

    # ─── Command Execution ───────────────────────────────────────────────

    def _exec_command(self, cmd: str) -> None:
        """Execute a command string."""
        try:
            self._exec_command_inner(cmd)
        except Exception as e:
            self._problem(str(e))

    def _exec_command_inner(self, cmd: str) -> None:
        """Resolve command via dispatcher and execute the result."""
        parts = cmd.split()
        result = self._commands.resolve(cmd)
        if result is None:
            app = self.active_app
            if app and app.active_view:
                self._run_view_command(app.active_view, parts)
                return
            self._problem(f'unknown: {parts[0] if parts else cmd}')
            return
        if result.command.run is None:
            self._problem(f'unknown: {parts[0] if parts else cmd}')
            return
        result.command.run(result.args)

    _DAEMON_SUBCOMMANDS = ('connect', 'sync', 'start', 'stop')

    def _build_global_commands(self) -> Command:
        root = Command('')
        daemon = Command('daemon', run=self._run_daemon,
                         description='Daemon control',
                         complete=self._complete_daemon)
        daemon.add(
            Command('connect', run=self._connect_command,
                    description='Connect to daemon (or "dummy")',
                    usage='[host] [port]',
                    complete=lambda text: (
                        ['dummy'] if 'dummy'.startswith(text) else [])),
            Command('sync', run=self._sync_command,
                    description='Sync config to daemon'),
            Command('start', run=self._daemon_start,
                    description='Start RX stream', usage='[port]'),
            Command('stop', run=self._daemon_stop,
                    description='Stop daemon'),
        )
        root.add(
            Command('help', run=lambda a: self._show_help(),
                    description='Show this help'),
            Command('quit', run=lambda a: self._quit(), aliases=('q',),
                    description='Exit'),
            Command('connect', run=self._run_connect,
                    description='Alias for :daemon connect',
                    usage='[host] [port]',
                    complete=lambda text: (
                        ['dummy'] if 'dummy'.startswith(text) else [])),
            daemon,
            Command('set', run=self._run_set, complete=self._complete_set,
                    description='Set config value',
                    usage='<key> <value>'),
            Command('get', run=self._run_get, complete=self._complete_get,
                    description='Show config value', usage='<key>'),
            Command('theme', run=self._run_theme,
                    description='Switch theme', usage='<name>',
                    complete=lambda text: [
                        name for name in STYLES if name.startswith(text)]),
            Command('channel', run=self._run_channel,
                    description='Tune to a WiFi channel', usage='<n>',
                    complete=lambda text: [
                        str(ch) for ch in sorted(CHANNEL_TO_FREQ)
                        if str(ch).startswith(text)]),
            Command('fpga', run=self._handle_fpga,
                    description='Control the WiFi FPGA', usage='[on|off]',
                    complete=lambda text: [
                        op for op in ('on', 'off') if op.startswith(text)]),
            Command('stop_tx', run=lambda a: self.daemon.tx_stop(),
                    description='Stop all TX'),
        )
        for name, (key, usage, description) in self._CONFIG_SHORTCUTS.items():
            root.add(Command(name,
                             run=lambda a, k=key, n=name:
                                 self._run_config_shortcut(k, n, a),
                             description=description,
                             usage=usage,
                             complete=self._shortcut_completions(name)))
        for app in self.apps.values():
            root.add(self._app_commands[app.name])
        return root

    def _build_app_command(self, app: App) -> Command:
        node = Command(app.name, run=lambda a: self._enter_app(app.name, a),
                       description=app.description)
        for name, view in app.views.items():
            node.add(Command(
                name,
                run=lambda a, n=name: self._enter_view(app.name, n, a),
                description=f'Open the {name} view',
                complete=lambda text, v=view: v.command_completions(text)))
        node.add(*app.command_nodes(
            run=lambda name, a: self._run_app_command(app, name, a),
            completions=app.command_completions))
        return node

    def _connect_command(self, args: list[str]) -> None:
        self._run_daemon(['connect', *args])

    def _sync_command(self, args: list[str]) -> None:
        self._run_daemon(['sync', *args])

    def _daemon_start(self, args: list[str]) -> None:
        self._run_daemon(['start', *args])

    def _daemon_stop(self, args: list[str]) -> None:
        self._run_daemon(['stop', *args])

    def _show_help(self) -> None:
        self._help_active = True
        app = self.active_app
        self._help_view.build_content(
            app_name=app.name if app else '',
            view_name=app.active_view.name if app and app.active_view else '',
            global_keys=self._global_keys,
            global_commands=self._commands.root,
            app_commands=self._app_commands)
        self._relayout()

    def _begin_lifecycle(self, label: str,
                         work: Callable[[], None]) -> None:
        """Start one blocking daemon operation on a worker thread."""
        self._require_ui_thread()
        if self._lifecycle_active is not None:
            self._problem(f'daemon: {self._lifecycle_active} in progress')
            return
        self._lifecycle_active = label
        self._sync_daemon_anim()
        self._update_status_bar()
        threading.Thread(target=self._run_lifecycle, args=(work,),
                         daemon=True).start()

    def _run_lifecycle(self, work: Callable[[], None]) -> None:
        """Worker body: do the blocking work, then post one completion."""
        try:
            work()
        except Exception as exc:
            message = str(exc)
            self._post(lambda m=message: self._finish_lifecycle(problem=m))

    def _finish_lifecycle(self, daemon: Daemon | None = None,
                          problem: str | None = None) -> None:
        """Apply a lifecycle result. UI thread only."""
        self._require_ui_thread()
        self._lifecycle_active = None
        if daemon is not None:
            self._install_daemon(daemon)
        if problem:
            self._problem(problem)
        self._sync_daemon_anim()
        self._update_status_bar()

    def _run_connect(self, args: list[str]) -> None:
        self._run_daemon(['connect', *args])

    def _run_daemon(self, args: list[str]) -> None:
        sub = args[0] if args else ''
        if sub not in self._DAEMON_SUBCOMMANDS:
            self._problem(f'daemon: unknown subcommand: {sub or "(none)"}')
            return
        target = self.daemon
        real = isinstance(target, SheshaDaemon)
        host = target.host if real else None
        port = target.port if real else None
        fresh = target.fresh_start if real else False
        desired = target.desired_mode if real else None
        if sub == 'connect' and real and target.connected:
            want_host = args[1] if len(args) > 1 else target.host
            want_port = int(args[2]) if len(args) > 2 else target.port
            if want_host == target.host and want_port == target.port:
                self._result(f'already connected {target.host_display}')
                return
        cmd = ' '.join(['daemon', *args])
        self._begin_lifecycle(
            sub,
            lambda h=host, p=port, f=fresh, d=desired, r=real:
                self._exec_command_sync(cmd, h, p, f, d, r))

    def _complete_daemon(self, text: str) -> list[str]:
        if not text:
            return list(self._DAEMON_SUBCOMMANDS)
        if ' ' not in text:
            return [s for s in self._DAEMON_SUBCOMMANDS
                    if s.startswith(text)]
        if text.startswith('connect '):
            return ['connect dummy'] if 'connect dummy'.startswith(text) else []
        return []

    def _run_set(self, args: list[str]) -> None:
        if len(args) >= 2:
            self._run_config(args[0], ' '.join(args[1:]))
        else:
            self._problem('set: usage: set <key> <value>')

    def _complete_set(self, text: str) -> list[str]:
        keys = sorted(self.config._schema.keys())
        if not text:
            return keys
        if ' ' not in text:
            return [key for key in keys if key.startswith(text)]
        key, _, rest = text.partition(' ')
        spec = self.config._schema.get(key)
        if spec and spec.allowed:
            values = spec.allowed
        elif key == 'gain':
            values = ['fast', 'slow', 'auto']
        else:
            values = []
        return [f'{key} {value}' for value in values
                if f'{key} {value}'.startswith(text)]

    def _run_get(self, args: list[str]) -> None:
        if not args:
            self._problem('get: usage: get <key>')
            return
        value = self.config.get(args[0])
        if value:
            self._result(f'{args[0]} = {value}')
        else:
            self._problem(f'{args[0]} not set')

    def _complete_get(self, text: str) -> list[str]:
        if ' ' in text:
            return []
        return [key for key in sorted(self.config._schema.keys())
                if key.startswith(text)]

    def _run_theme(self, args: list[str]) -> None:
        if not args:
            self._problem('theme: usage: theme <name>')
            return
        if args[0] not in STYLES:
            self._problem(f'unknown theme: {args[0]}')
            return
        self._apply_theme(args[0])

    def _run_channel(self, args: list[str]) -> None:
        if not args:
            self._problem('channel: missing channel number')
            return
        try:
            channel = int(args[0])
        except ValueError:
            self._problem(f'invalid channel: {args[0]}')
            return
        freq = CHANNEL_TO_FREQ.get(channel)
        if freq is None:
            self._problem(f'unknown channel: {channel}')
            return
        self._run_config('freq', str(freq * 1_000_000))

    def _shortcut_completions(self, name: str):
        if name == 'gain':
            return lambda text: [
                mode for mode in ('fast', 'slow', 'auto')
                if mode.startswith(text)]
        return lambda text: []

    def _run_config_shortcut(self, key: str, name: str,
                             args: list[str]) -> None:
        if not args:
            self._problem(f'{name}: usage: {name} <value>')
            return
        self._run_config(key, ' '.join(args))

    def _run_config(self, key: str, value: str) -> None:
        if key == 'gain':
            if value in ('auto', 'fast', 'fast_attack', 'slow',
                         'slow_attack'):
                current = self.daemon.get_state().get('gain', '')
                try:
                    float(current)
                    self._last_manual_gain = current
                except (ValueError, TypeError):
                    pass
            else:
                self._last_manual_gain = value
        if key in ('freq', 'bw', 'sample_rate'):
            try:
                value = str(parse_freq(value))
            except (ValueError, TypeError):
                self._problem(f'invalid frequency: {value}')
                return
        self._apply_config(key, value)

    def _run_view_command(self, view: View, args: list[str]) -> None:
        """Execute a view command and show its result on the command line."""
        err, detail = view.command(args)
        if err:
            self._problem(err)
        elif detail:
            self._result(detail)

    def _run_app_command(self, app: App, name: str,
                         args: list[str]) -> None:
        err, detail = app.execute_command(name, args, self.daemon)
        if err:
            self._problem(err)
            return
        if name == 'tx':
            self._last_tx = (app.name, [name, *args])
        elif name == 'record':
            self._last_record = (app.name, [name, *args])
            self._record_start(detail, eapol_only='--eapol' in args)
        elif name == 'record_stop':
            self._record_stop()

    def _enter_app(self, name: str, args: list[str]) -> None:
        app = self._activate_app(name)
        if args:
            view_name = app.default_view
            view = app.views.get(view_name)
            if view:
                self._sync_view(view)
                app.switch_view(view_name)
                self._apply_view_mode(app.active_view)
                self._run_view_command(app.active_view, args)
        self._relayout()
        self._save_app_view()

    def _enter_view(self, app_name: str, view_name: str,
                    args: list[str]) -> None:
        app = self._activate_app(app_name)
        if app.active_view is None or app.active_view.name != view_name:
            view = app.views[view_name]
            self._sync_view(view)
            app.switch_view(view_name)
            self._apply_view_mode(app.active_view)
        if args and app.active_view:
            self._run_view_command(app.active_view, args)
        self._relayout()
        self._save_app_view()

    def _activate_app(self, name: str) -> App:
        if name != self._active_app:
            self._remember_departure()
            self._deactivate_current_view()
            state = self.daemon.get_state()
            if state.get('fpga_active', '0') == '1':
                self.daemon.set_fpga(False)
                self._animator.to('fpga_active', 0.0, 0.4, 'ease_out')
            self._active_app = name
            app = self.active_app
            view_name = self._restore_view_name(app)
            view = app.views.get(view_name)
            if view:
                self._sync_view(view)
            app.switch_view(view_name)
            self._apply_view_mode(app.active_view)
        return self.apps[name]

    def _candidate_daemon(self, host: str | None, port: int | None,
                          fresh_start: bool,
                          desired_mode: str | None) -> SheshaDaemon:
        return SheshaDaemon(
            self.config,
            host=host or '192.168.2.1',
            port=port or 5555,
            project_dir=self._project_dir,
            fresh_start=fresh_start,
            desired_mode=desired_mode,
        )

    def _connect_worker(self, args: list[str], host: str | None,
                        port: int | None, fresh_start: bool,
                        desired_mode: str | None) -> None:
        if args and args[0] == 'dummy':
            candidate = DummyDaemon(self.config)
            self._post(lambda d=candidate: self._finish_lifecycle(daemon=d))
            return
        target_host = args[0] if args else host
        target_port = int(args[1]) if len(args) > 1 else port
        candidate = self._candidate_daemon(
            target_host, target_port, fresh_start, desired_mode)
        result = candidate.smart_connect(target_host, target_port)
        problem = None if candidate.connected else result
        self._post(
            lambda d=candidate, p=problem:
                self._finish_lifecycle(daemon=d, problem=p))

    def _start_worker(self, args: list[str], host: str | None,
                      port: int | None) -> None:
        target_port = int(args[0]) if args else port
        candidate = self._candidate_daemon(host, target_port, False, None)
        if not candidate.start(target_port):
            self._post(lambda: self._finish_lifecycle(
                problem='start failed'))
            return
        if candidate.fresh_start:
            self._post(
                lambda d=candidate: self._finish_lifecycle(daemon=d))
        else:
            self._post(lambda: self._finish_lifecycle())

    def _exec_command_sync(self, cmd: str, host: str | None = None,
                           port: int | None = None,
                           fresh_start: bool = False,
                           desired_mode: str | None = None,
                           target_is_real: bool = False) -> None:
        """Run a blocking daemon command on a worker thread.

        Never touches the published daemon.  Blocking work runs here; the
        resulting daemon and any user-facing message are posted to the UI
        thread.
        """
        parts = cmd.strip().split()
        verb = parts[0]
        args = parts[1:]
        if verb == 'daemon':
            if not args:
                self._post(lambda: self._finish_lifecycle(
                    problem='daemon: missing subcommand'))
                return
            verb, args = args[0], args[1:]

        if verb == 'connect':
            self._connect_worker(args, host, port, fresh_start, desired_mode)
            return
        if verb not in ('sync', 'start', 'stop'):
            message = f'daemon: unknown subcommand: {verb}'
            self._post(lambda m=message: self._finish_lifecycle(problem=m))
            return
        if not target_is_real:
            self._post(lambda: self._finish_lifecycle(
                problem='not a real daemon'))
            return

        if verb == 'sync':
            candidate = self._candidate_daemon(host, port, False, None)
            candidate.sync()
            self._post(
                lambda d=candidate: self._finish_lifecycle(daemon=d))
        elif verb == 'start':
            self._start_worker(args, host, port)
        else:
            candidate = self._candidate_daemon(host, port, False, None)
            if candidate.stop():
                self._post(
                    lambda d=candidate: self._finish_lifecycle(daemon=d))
            else:
                self._post(lambda: self._finish_lifecycle(
                    problem='stop failed'))

    # ─── Tab Completion ──────────────────────────────────────────────────

    def _reset_tab_state(self) -> None:
        self._commands.reset_tab_state()

    def _on_tab(self, reverse: bool = False) -> None:
        """Handle Tab/Shift+Tab in command mode."""
        buf, cursor = self._commands.on_tab(
            self._input.command_buf, self._input.cursor, reverse=reverse)
        self._input.command_buf = buf
        self._input.cursor = cursor

    # ─── History ─────────────────────────────────────────────────────────

    @property
    def _command_history(self) -> deque:
        return self._commands._command_history

    @property
    def _history_index(self) -> int:
        return self._commands._history_index

    @_history_index.setter
    def _history_index(self, val: int) -> None:
        self._commands._history_index = val

    def _on_history_length_changed(self) -> None:
        """Trim or remove persisted cmd_history. Does not affect in-memory deque."""
        max_len = self.config.get_int('cmd_history_length', 100)
        mem = list(self._command_history)
        if max_len <= 0:
            self.config.delete_key('cmd_history')
        else:
            trimmed = mem[-max_len:] if len(mem) > max_len else mem
            if trimmed != self.config.get('cmd_history', []):
                self.config.set_raw('cmd_history', trimmed)

    def _push_history(self, cmd: str) -> None:
        """Add command to history (dedup consecutive). Persists to config."""
        max_len = self.config.get_int('cmd_history_length', 100)
        def persist(history_list):
            self.config.set('cmd_history', history_list)
        self._commands.push_history(cmd, persist=persist, max_len=max_len)

    def _history_back(self) -> None:
        """Navigate history backwards."""
        self._input.command_buf = self._commands.history_back(self._input.command_buf)
        self._input.cursor = len(self._input.command_buf)

    def _history_forward(self) -> None:
        """Navigate history forwards."""
        self._input.command_buf = self._commands.history_forward(self._input.command_buf)
        self._input.cursor = len(self._input.command_buf)

    # ─── App Cycling ─────────────────────────────────────────────────────

    def _save_app_view(self) -> None:
        app = self.active_app
        if app and app.active_view:
            self.config.set('app.view',
                            f'{app.name}:{app.active_view.name}')

    def _remember_departure(self) -> None:
        """Record the current view for its app and set the Tab target."""
        app = self.active_app
        if app is None:
            return
        if app.active_view:
            self._last_view[app.name] = app.active_view.name
        self._prev_app = app.name

    def _restore_view_name(self, app: App) -> str:
        """Enter an app at its remembered view, else its splash view."""
        remembered = self._last_view.get(app.name)
        if remembered in app.views:
            return remembered
        return app.default_view

    def _switch_app(self, name: str) -> None:
        """Switch to an app, restoring the view last used there."""
        if name == self._active_app or name not in self.apps:
            return
        self._remember_departure()
        self._deactivate_current_view()
        self._active_app = name
        app = self.active_app
        view_name = self._restore_view_name(app)
        view = app.views.get(view_name)
        if view:
            self._sync_view(view)
        app.switch_view(view_name)
        self._apply_view_mode(app.active_view)
        self._relayout()
        self._save_app_view()

    def _cycle_view(self, direction: int) -> None:
        """Cycle through the current app's views in list order."""
        app = self.active_app
        if app is None:
            return
        names = list(app.views.keys())
        if len(names) < 2:
            return
        current = app.active_view.name if app.active_view else ''
        idx = names.index(current) if current in names else 0
        idx = (idx + direction) % len(names)
        name = names[idx]
        self._sync_view(app.views[name])
        app.switch_view(name)
        self._apply_view_mode(app.active_view)
        self._relayout()
        self._save_app_view()

    # ─── TX Toggle ───────────────────────────────────────────────────────

    def _tx_toggle(self) -> None:
        """Toggle TX: stop if busy, replay last TX if idle."""
        state = self.daemon.get_state()
        if state.get('tx_busy', '0') == '1':
            self.daemon.tx_stop()
        elif self._last_tx:
            app_name, args = self._last_tx
            app = self.apps.get(app_name)
            if app:
                err, _ = app.execute_command(args[0], args[1:], self.daemon)
                if err:
                    self._problem(err)
        else:
            self._problem('no previous tx — use :wifi tx beacon ... first')

    # ─── FPGA Toggle ─────────────────────────────────────────────────────

    def _handle_fpga(self, args: list[str]) -> None:
        """Handle :fpga on/off/(no args)."""
        if not args:
            # Query: print current fpga state
            state = self.daemon.get_state()
            active = state.get('fpga_active', '0') == '1'
            self._result(f'fpga: {"on" if active else "off"}')
            return

        op = args[0].lower()
        if op == 'on':
            # Validate: current app must be wifi
            if self._active_app != 'wifi':
                self._problem('fpga: not available for this app')
                return
            state = self.daemon.get_state()
            if state.get('fpga_active', '0') == '1':
                self._problem('fpga: already on')
                return
            self.daemon.set_fpga(True)
            self._animator.to('fpga_active', 1.0, 0.8, 'ease_in_out')
        elif op == 'off':
            state = self.daemon.get_state()
            if state.get('fpga_active', '0') == '0':
                self._problem('fpga: already off')
                return
            self.daemon.set_fpga(False)
            self._animator.to('fpga_active', 0.0, 0.4, 'ease_out')
        else:
            self._problem(f'fpga: unknown op "{op}" (use on/off)')

    # ─── Recording ─────────────────────────────────────────────────────

    _PCAP_MAGIC = 0xa1b2c3d4
    _PCAP_VERSION_MAJOR = 2
    _PCAP_VERSION_MINOR = 4
    _PCAP_GLOBAL_HEADER = struct.Struct('<IHHiIII')
    _PCAP_PACKET_HEADER = struct.Struct('<IIII')

    @staticmethod
    def _make_radiotap(ts_usec: int, rate_mbps: int, freq_mhz: int,
                       rx_gain: float, fcs_ok: bool) -> bytes:
        present = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 5) | (1 << 13)
        buf = bytearray(28)
        struct.pack_into('<BBHI', buf, 0, 0, 0, 28, present)  # hdr (8 bytes)
        struct.pack_into('<Q',  buf, 8, ts_usec)               # TSFT
        buf[16] = 0x10 if not fcs_ok else 0                     # Flags
        buf[17] = rate_mbps * 2                                  # Rate (500kHz)
        struct.pack_into('<HH', buf, 20, freq_mhz, 0x0014)      # Channel (5 GHz OFDM)
        buf[24] = int(max(-128, min(127, rx_gain))) & 0xFF      # Antenna signal
        struct.pack_into('<H', buf, 26, 0 if fcs_ok else 1)     # RX flags
        return bytes(buf)

    def _record_start(self, filename: str, eapol_only: bool) -> None:
        """Begin recording wifi frames to a PCAP file."""
        if self._recording:
            self._record_stop()

        expanded = os.path.expanduser(filename)
        if self._project_dir and not os.path.isabs(expanded):
            filepath = os.path.join(self._project_dir, expanded)
        else:
            filepath = expanded

        try:
            f = open(filepath, 'wb')
        except OSError as e:
            self._problem(str(e))
            return

        global_hdr = Shell._PCAP_GLOBAL_HEADER.pack(
            Shell._PCAP_MAGIC,
            Shell._PCAP_VERSION_MAJOR,
            Shell._PCAP_VERSION_MINOR,
            0,       # timezone
            0,       # sigfigs
            65535,   # snaplen
            127,     # DLT_IEEE802_11_RADIOTAP
        )
        f.write(global_hdr)
        f.flush()

        self._recording = True
        self._rec_file = f
        self._rec_filename = filename
        self._rec_eapol = eapol_only
        self._rec_frames = 0
        self._rec_label = filename
        self._relayout()

    def _record_stop(self) -> None:
        """Stop recording and close the PCAP file."""
        if not self._recording:
            return
        self._recording = False
        f = self._rec_file
        self._rec_file = None
        self._rec_filename = ''
        self._rec_label = ''
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        self._relayout()

    def _write_pcap_frame(self, frame: dict) -> None:
        """Write a single wifi frame to the open PCAP file."""
        rec_file = self._rec_file
        if not self._recording or rec_file is None:
            return

        if frame.get('type') != 'wifi':
            return

        if self._rec_eapol:
            meta = frame.get('meta')
            if meta is None or not getattr(meta, 'is_eapol', False):
                return

        ts_abs = time.time()
        ts_sec = int(ts_abs)
        ts_usec = int((ts_abs - ts_sec) * 1_000_000)
        rate = frame.get('rate_mbps', 0) or 0
        psdu = frame.get('psdu', b'') or b''
        fcs_ok = frame.get('fcs_ok', False)

        freq_mhz = 0
        raw_freq = self.daemon.get_state().get('freq')
        if raw_freq is not None:
            try:
                freq_mhz = int(float(raw_freq)) // 1_000_000
            except (ValueError, TypeError):
                pass

        radiotap = Shell._make_radiotap(ts_usec, rate, freq_mhz,
                                         frame.get('rx_gain', 0.0), fcs_ok)
        packet = radiotap + psdu
        pkt_hdr = Shell._PCAP_PACKET_HEADER.pack(
            ts_sec, ts_usec, len(packet), len(packet))

        try:
            rec_file.write(pkt_hdr)
            rec_file.write(packet)
            self._rec_frames += 1
        except (OSError, ValueError):
            if self._rec_file is rec_file:
                self._record_stop()

    def _record_toggle(self) -> None:
        """Toggle recording: stop if active, replay last record command if idle."""
        if self._recording:
            self._record_stop()
        elif self._last_record:
            app_name, args = self._last_record
            app = self.apps.get(app_name)
            if app:
                err, detail = app.execute_command(args[0], args[1:],
                                                       self.daemon)
                if err:
                    self._problem(err)
                elif detail:
                    eapol = '--eapol' in args
                    self._record_start(detail, eapol_only=eapol)
        else:
            self._problem('no previous record — use :wifi record file.pcap first')

    # ─── Frequency Tuning ────────────────────────────────────────────────

    def _tune_freq(self, delta_mhz: int) -> None:
        """Adjust frequency by delta MHz."""
        raw_freq = self.daemon.get_state().get('freq')
        if raw_freq is None:
            self._problem('frequency is not known yet')
            return
        current = int(float(raw_freq))
        new_freq = max(70_000_000, min(6_000_000_000, current + delta_mhz * 1_000_000))
        self._apply_config('freq', str(new_freq))

    # ─── Frame Feeding ───────────────────────────────────────────────────

    def _feed_frames(self, frames: list[dict]) -> None:
        """Send data frames to active view."""
        if not frames:
            return
        app = self.active_app
        if app and app.active_view:
            for frame in frames:
                app.active_view.update(frame)
        if self._recording:
            for frame in frames:
                self._write_pcap_frame(frame)

    # ─── Event Loop ──────────────────────────────────────────────────────

    def run(self) -> None:
        self._ui_thread = threading.get_ident()
        self._running = True
        try:
            self.screen.start()
            self.screen.keyboard.listen(self._enqueue_key,
                                        on_error=self._listener_error)

            # Ensure starting app has active view
            app = self.active_app
            if app and not app.active_view:
                default_view = app.views.get(app.default_view)
                if default_view:
                    self._sync_view(default_view)
                app.switch_view(app.default_view)
                self._apply_view_mode(app.active_view)

            self.screen.layout(self._build_layout)
            self.screen.draw()

            loop = EventLoop(fps=60.0)
            loop.run(self)
        finally:
            try:
                self.shutdown()
            finally:
                self.screen.stop()

    def _listener_error(self, exc: BaseException) -> None:
        """Route a handler failure to the result line via the UI queue."""
        message = f'{type(exc).__name__}: {exc}'
        self._post(lambda m=message: self._problem(m))

    # ─── EventCallbacks protocol ─────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def check_resize(self) -> None:
        self.screen.check_resize()

    def get_daemon_fd(self) -> int | None:
        return self.daemon.fileno()

    def on_actions(self) -> None:
        self._drain_actions()

    def on_data(self) -> None:
        self._feed_frames(self.daemon.poll())

    def on_heartbeat(self) -> None:
        if isinstance(self.daemon, SheshaDaemon) and self.daemon.connected:
            self.daemon.ping()

    def on_check_disconnect(self) -> None:
        if isinstance(self.daemon, SheshaDaemon) and self.daemon.connected:
            self.daemon.check_alive(timeout=self._DISCONNECT_TIMEOUT)

    def on_frame(self, dt: float) -> None:
        self.daemon.drain_notifications()
        self._feed_frames(self.daemon.poll())
        self._animator.tick(dt)
        app = self.active_app
        if app and app.active_view:
            app.active_view.tick(dt)
        self._update_status_bar()
        self.screen.draw()
