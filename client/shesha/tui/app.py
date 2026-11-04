"""App and View base classes for the vim-modal TUI.

Shell > App > View hierarchy. Apps group related Views.
Views own a screen region and implement keybinds/commands/rendering.
"""
from __future__ import annotations
from typing import Callable, TYPE_CHECKING

from .command_model import Command
from ..protocol import DaemonMode

if TYPE_CHECKING:
    from .pytui import StyledWindow, Window
    from .daemon import Daemon
    from .config import Config
    from .animate import Animator
    from .config import KeySpec


class View:
    """A screen section with keybinds, commands, and per-frame updates."""

    name: str = ''
    hints: str = ''
    mode: DaemonMode | None = None

    def __init__(self) -> None:
        self.config: Config | None = None
        self.daemon: Daemon | None = None
        self.animator: Animator | None = None
        self.active = False

    def apply_config(self, key: str, value: str) -> None:
        """View-internal config change: persist and forward radio keys.

        Config is write-only — radio keys must be pushed to the daemon
        explicitly (the old Config observer is gone).  What is forwarded is
        the canonical form, not the typed text: symbolic settings such as
        window ``hann`` or accum ``max`` must reach the daemon as their wire
        numbers, or the daemon's ``int()`` raises on the name.
        """
        if self.config:
            self.config.set(key, value)
            wire = self.config.get_normalized(key)
            if wire is not None:
                value = wire
        if self.daemon is not None:
            self.daemon.apply_config(key, value)

    def layout(self, container: 'StyledWindow') -> list['Window']:
        """Build window tree inside container. Return list for Screen."""
        return [container]

    def _key_defs(self) -> list[tuple[str, str, Callable[[int], None]]]:
        """Return (key, description, handler) triples. Override in subclasses.

        keybinds() and key_hints() derive from this — one source of truth.
        """
        return []

    def keybinds(self) -> dict[str, Callable[[int], None]]:
        """Normal-mode keybinds derived from _key_defs()."""
        return {k: h for k, _, h in self._key_defs()}

    def key_hints(self) -> dict[str, str]:
        """Key descriptions for the help page, derived from _key_defs()."""
        return {k: d for k, d, _ in self._key_defs()}

    def key_groups(self) -> list[tuple[tuple[str, ...], str]]:
        """Keys that trigger one action, grouped by that action.

        Aliases such as ``j``/``down`` declare the same description, so the
        help page shows them on one row.
        """
        groups: list[list] = []
        index: dict[str, int] = {}
        for key, description, _ in self._key_defs():
            if description in index:
                groups[index[description]][0].append(key)
            else:
                index[description] = len(groups)
                groups.append([[key], description])
        return [(tuple(keys), description) for keys, description in groups]

    def command_hints(self) -> list[tuple[str, str]]:
        """Argument commands this view accepts, as (syntax, description)."""
        return []

    def command(self, args: list[str]) -> tuple[str | None, str]:
        """Handle a command delegated from Shell. Returns (error, detail)."""
        return (None, '')

    def word_completions(self) -> list[str]:
        """Fixed candidates for this view's single argument slot."""
        return []

    def command_completions(self, text: str) -> list[str]:
        """Candidates for this view's argument text."""
        if ' ' in text:
            return []
        return [word for word in self.word_completions()
                if word.startswith(text)]

    def update(self, frame: dict) -> None:
        """Receive a data frame from daemon."""
        pass

    def tick(self, dt: float) -> None:
        """Per-frame update (animations, redraws). dt in seconds."""
        if self.animator:
            self.animator.tick(dt)

    def config_changed(self, key: str, daemon: 'Daemon') -> None:
        """Config/daemon state changed. Update display if relevant."""
        pass

    def app_registry_changed(self, apps: dict[str, 'App']) -> None:
        """The shell's app registry is available (overview screens)."""
        pass

    def config_updated(self, key: str, value: str) -> None:
        """A config key was set via the shell (view-local display prefs)."""
        pass

    def theme_changed(self, style: dict) -> None:
        """Theme switched. Update colors."""
        pass

    def on_enter(self) -> None:
        """Called when this view becomes active."""
        pass

    def on_exit(self) -> None:
        """Called when leaving this view."""
        pass


class App:
    """Groups related views. Each app declares one default view.
    
    commands: dict of name → handler(args: list[str], daemon: Daemon).
    Command names must not overlap with view names. When Shell resolves
    ':app_name command_name args', if command_name is in commands it
    executes the handler WITHOUT switching views. This supports app-level
    operations like TX that run alongside any view.
    """

    name: str = ''
    description: str = ''
    default_view: str = ''
    commands: dict[str, Callable]
    command_descriptions: dict[str, str] = {}
    command_usage: dict[str, str] = {}

    def __init__(self) -> None:
        self.views: dict[str, View] = {}
        self.commands: dict[str, Callable] = {}
        self._active_view: str | None = None

    def validate(self) -> None:
        """Reject an app that cannot be entered.

        Subclasses populate views after construction, so callers invoke
        this once the app is built rather than from __init__.
        """
        if not self.views:
            raise ValueError(f'{self.name}: app has no views')
        if self.default_view not in self.views:
            raise ValueError(
                f'{self.name}: default_view {self.default_view!r} '
                f'is not one of {sorted(self.views)}')
        overlap = set(self.views) & set(self.commands)
        if overlap:
            raise ValueError(
                f'{self.name}: view and command names overlap: '
                f'{sorted(overlap)}')

    @classmethod
    def config_keys(cls) -> dict[str, 'KeySpec']:
        """Return config keys owned by this app (dot-prefixed with app name)."""
        return {}

    @property
    def active_view(self) -> View | None:
        if self._active_view:
            return self.views.get(self._active_view)
        return None

    def switch_view(self, name: str) -> None:
        current = self.active_view
        if current:
            current.active = False
            current.on_exit()
        self._active_view = name
        new = self.active_view
        if new:
            new.active = True
            new.on_enter()

    def command_nodes(self, run, completions) -> list[Command]:
        """Build app-level command nodes. Override to add subcommands."""
        return [
            Command(name,
                    run=lambda args, n=name: run(n, args),
                    description=self.command_descriptions.get(name, ''),
                    usage=self.command_usage.get(name, ''),
                    complete=lambda text, n=name: completions(n, text))
            for name in self.commands
        ]

    def command_completions(self, name: str, text: str) -> list[str]:
        return []

    def execute_command(self, name: str, args: list[str],
                        daemon: 'Daemon') -> tuple[str | None, str]:
        """Execute an app-level command. Returns (error, detail)."""
        handler = self.commands.get(name)
        if handler:
            try:
                return handler(args, daemon)
            except Exception as e:
                return (str(e), '')
        return (f"unknown command: {name}", '')
