# Shesha TUI

The TUI is a vim-modal terminal client for the SDR daemon. It is one client of
many (see `ARCHITECTURE.md`). This document records the design decisions that
are not obvious from the code. Signatures and module layout belong in the code
itself.

## Ambient state, not text

Persistent state is carried by styling and animating glyphs and chrome, not
by words. The interface is meant to feel like an instrument you are
operating rather than a log you are reading: `:fpga on` shifting the app/view
pane's colour reads like pressing the accelerator, and the daemon glyph
pulsing faster while a connect is in flight reads as effort, then relief as
the "heart rate" settles.

- **Daemon glyph** (`glyph_daemon`, the skull) is the connection indicator.
  Only two facts matter: is a link up, and is anything happening. Busy -- any
  daemon or connection operation in flight, including the pre-flight SSH
  probes and a deploy -- pulses fast. Idle and connected throbs slowly. Idle
  and disconnected sits at steady `warn`, no animation. A link with no bytes
  received for ~1 s is treated as dead. The `host:port` text that used to sit
  beside it now lives on the status splash.
- **TX glyph** (`glyph_tx`, the lightning bolt) sits next to the daemon
  glyph: `dim` when idle, bright (`fg`) while the radio is transmitting. It
  is deliberately the only generic TX indicator -- per-app detail (what is
  being sent, how far along) belongs to the app.
- **Colour as state** elsewhere: the status-bar app/view pane shifts on
  `fpga_active`, and the gain field fades from `fg` to `warn` when the ADC is
  clipping. The daemon's rail counters trip a latch above 1%; it holds for
  2.5 s so a duty-cycled burst stays visible, then fades back. A fade always
  completes before the next one starts.
  App views carry their own cues (RDS lock, scan tables, FM stream progress).

## The command line is a result line, not an error line

The single line under the `:` prompt shows the result of the last command. It
carries two things:

- **Answers** to queries (`:get`, `:fpga`) -- normal foreground.
- **Problems** -- anything that failed or was refused -- `warn`.

It is transient: cleared on the next keypress. Success acknowledgements for
ambient state changes (connect, sync, start, stop, fpga on/off) emit no text
at all; the glyph or app state is the acknowledgement.

There is intentionally no separate "notice" channel and no toast/notification
system. If something is worth saying and is not a problem, it belongs to the
owning app.

## Apps own their own status

App- and view-specific status lives with the app and is drawn in its own
pane: FPGA on/off, RDS lock state, FM TX streaming and progress, WiFi scan
and traffic. The shell does not broker it. This is why the old global TX
status bar was removed rather than extended. The WiFi views keep a per-view
TX bar so toggling TX never reflows the table.

## Glyphs are semantic theme keys

`glyph_daemon` and `glyph_tx` are roles, not literal characters. A theme may
substitute a different glyph; the role's meaning and usage are fixed. A glyph
must occupy one terminal column (`char_width`).

## State that settles: sent, then confirmed

The client never shows a radio value it did not receive from the daemon.
A command such as `:freq 5200M` writes bytes and pulses the affected status
field. The firmware pushes an unsolicited `RESP_STATUS` after every successful
mutation, and that push is what updates the displayed state and settles the
pulse. A bare command ack also settles the pulse when the value did not
change, as with an AGC mode switch.

The config store is write-only. It seeds a freshly started daemon and is never
pushed into a running one; reconnecting adopts whatever the daemon reports.
This keeps the daemon the single source of truth and avoids a second copy of
radio state in the client.

## Apps, views, and modes

Apps are UI groupings, not daemon modes. A view declares the radio mode it
needs through `View.mode`; `None` leaves the radio alone. The shell applies the
mode on every view transition, and several views may share one mode. An app may
also expose commands that run without switching views; WiFi TX uses this to run
alongside both scan and traffic.

A view owns its layout, keys, and status. `_key_defs()` is the single source
for key bindings and help text, so the two cannot drift apart.

## Commands and completion

One command tree drives resolution and tab completion. The first token is
resolved against global commands, then against the active app. Later tokens
descend inside the chosen branch; the first non-matching token becomes an
argument. `:wifi record` and `:record` (while WiFi is active) therefore reach
the same handler, and `:stop_tx` is always available. If no node matches, the
active view's free-form `command()` still receives the arguments, which is how
`:88.5M` tunes FM and `:floor -40` sets the waterfall range.

Each node may declare an argument completer. Filesystem completion is not a
special case: `:fm tx` attaches the path completer to its node. Tab extends
to the longest common prefix first, then cycles candidates. Command-name
completion replaces only the token under the cursor and leaves the rest of
the line intact. Argument completion replaces from the start of the argument
to the end of the line.
The line editor supports left/right, Home/End, Delete, backspace, and
Ctrl-A/Ctrl-E.

## Radio policy

The daemon advertises the active producer's radio rules in `RESP_STATUS`. The
shell refuses out-of-policy `bw` and `sample_rate` changes, and refuses gain
changes when the producer pins AGC. Views keep no copy of those rules; the
firmware is the source of truth. The fresh-daemon seed uses the same policy to
decide which saved keys to replay.

## FM RX waveform clock

The RX scope follows the player, not the receiver. Whatever sits in the
socket or the player queue is ahead of what the speaker emits. `ffplay`
publishes its audio clock on the status line; the player parses that and
exposes a sample cursor. Players with no clock use a fixed delay from the
accepted-sample frontier.

The view keeps waveform history indexed by accepted-sample count, so a
dropped push closes the gap instead of shifting every later sample. History
is trimmed at the playhead, not at the receive edge: a lagging player still
needs the samples between its cursor and the frontier.

## Threading

The UI thread owns all TUI and published-daemon state. Two other threads
exist: the keyboard reader and short-lived workers for blocking daemon
commands.

- The reader thread only enqueues keys through `Shell._post`.
- Workers only do blocking I/O. They never touch the published daemon,
  the screen, or shell fields.
- The event loop drains the action queue at the top of every iteration,
  before polling the daemon.
- Daemon installs for `:connect` and `:daemon sync/start/stop` are queued
  actions, so `SheshaDaemon.poll()` and `_close()` never overlap.
- `Shell._require_ui_thread()` enforces this in tests; a call from any
  other thread raises `RuntimeError`.

## Off-screen output

Stray writes must never reach the terminal. A subprocess inheriting the tty,
`logging`'s last-resort handler, or a thread traceback would each flash a
line over the alt screen. `Screen` owns fds 1/2 for its lifetime: it
duplicates the real tty for the renderer, points fds 1/2 at
`$TMPDIR/shesha-tui.log`, and restores them on stop. On top of that, the
audio player's stdout is `/dev/null` and stderr is `/dev/null` except for
`ffplay`, whose status line is piped and parsed for its audio clock. A
keyboard-listener exception is logged and routed to the result line instead
of killing the reader thread.

## Where to look

- `client/shesha/tui/shell.py` -- coordinator; wires the loop, input, and
  dispatch.
- `client/shesha/tui/command_model.py` -- one tree for resolution and completion.
- `client/shesha/tui/daemon.py` -- connection, state, and radio policy.
- `client/shesha/tui/app.py` -- App and View contracts.
- `client/shesha/tui/help.py` -- help overlay built from live state.
- `client/shesha/tui/pytui.py` -- terminal rendering primitives.

## Running it

```bash
shesha tui --offline
cd client && python3 -m pytest tests/ -q
```

Offline mode runs against `DummyDaemon`, which generates synthetic frames.
