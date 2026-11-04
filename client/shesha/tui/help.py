"""Self-documenting help overlay.

Everything here is rendered from live state: key bindings, the command
tree, and the config schema.  No command or key list lives in this file.
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Iterator

from .command_model import Command

if TYPE_CHECKING:
    from .config import KeySpec
    from .app import App, View
    from .pytui import Window
    from .shell import GlobalKey


_KEY_LABELS = {
    'left': '\u2190', 'right': '\u2192', 'up': '\u2191', 'down': '\u2193',
    'page_up': 'PgUp', 'page_down': 'PgDn',
    'home': 'Home', 'end': 'End',
    'escape': 'Esc', 'enter': 'Enter',
    '\x09': 'Tab', '\x0a': 'Enter', '\x7f': 'Backspace',
    ' ': 'Space',
}


def _key_label(key: str) -> str:
    return _KEY_LABELS.get(key, key)


def _key_labels(keys: tuple[str, ...]) -> str:
    return ' / '.join(_key_label(key) for key in keys)


def _command_label(path: tuple[str, ...], node: Command) -> str:
    names = [':', ' '.join((*path, node.name))]
    if node.aliases:
        names.append(' / ')
        names.append(' / '.join(
            ':' + ' '.join((*path, alias)) for alias in node.aliases))
    if node.usage:
        names.extend((' ', node.usage))
    return ''.join(names)


def _command_rows(parent: Command, path: tuple[str, ...],
                  skip: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Flatten a command subtree into (syntax, description) rows."""
    rows: list[tuple[str, str]] = []
    for child in parent.children.values():
        if child.name in skip:
            continue
        rows.append((_command_label(path, child), child.description))
        rows.extend(_command_rows(child, (*path, child.name)))
    return rows


class HelpView:
    """Generates and scrolls help content from live shell state."""

    def __init__(self, schema: dict[str, 'KeySpec'],
                 apps: dict[str, 'App']) -> None:
        self._schema = schema
        self._apps = apps
        self._lines: list[str] = []
        self._scroll = 0

    # ─── Content generation ──────────────────────────────────────

    def build_content(self, app_name: str = '', view_name: str = '', *,
                      global_keys: tuple['GlobalKey', ...] | list['GlobalKey'] = (),
                      global_commands: Command | None = None,
                      app_commands: dict[str, Command] | None = None) -> None:
        """Build help lines scoped to the active app and view.

        Local rows come first, global rows last.  Empty app/view names
        show every app.
        """
        app = self._apps.get(app_name) if app_name else None
        view = app.views.get(view_name) if app and view_name else None
        global_commands = global_commands or Command('')
        app_commands = app_commands or {}

        lines: list[str] = []
        self._render_section(lines, 'KEYS', self._key_blocks(
            app, view, global_keys))
        self._render_section(lines, 'COMMANDS', self._command_blocks(
            app, view, global_commands, app_commands))
        self._render_section(lines, 'CONFIG', self._config_blocks(
            app, view))
        self._lines = lines
        self._scroll = 0

    # ─── Keys ────────────────────────────────────────────────────

    def _key_blocks(self, app: 'App | None', view: 'View | None',
                    global_keys: list['GlobalKey'] | tuple['GlobalKey', ...],
                    ) -> list[tuple[str, list[tuple[str, str]]]]:
        blocks: list[tuple[str, list[tuple[str, str]]]] = []
        views = [(app, view)] if view else self._all_views(app)
        for owner, item in views:
            if item is None:
                continue
            hints = item.key_groups()
            if hints:
                blocks.append((f'{owner.name} - {item.name}', [
                    (_key_labels(keys), description)
                    for keys, description in hints]))
        rows = [(binding.label or _key_labels(binding.keys),
                 binding.description) for binding in global_keys]
        if rows:
            blocks.append(('Global', rows))
        return blocks

    # ─── Commands ────────────────────────────────────────────────

    def _command_blocks(self, app: 'App | None', view: 'View | None',
                        global_commands: Command,
                        app_commands: dict[str, Command],
                        ) -> list[tuple[str, list[tuple[str, str]]]]:
        blocks: list[tuple[str, list[tuple[str, str]]]] = []
        if app is not None:
            if view is not None:
                hints = view.command_hints()
                if hints:
                    blocks.append((f'{app.name} - {view.name}', [
                        (f':{app.name} {view.name} {syntax}'.rstrip(),
                         description)
                        for syntax, description in hints]))
            node = app_commands.get(app.name)
            if node is not None:
                skip = (view.name,) if view is not None else ()
                rows = _command_rows(node, (app.name,), skip=skip)
                if rows:
                    blocks.append((app.name, rows))
        else:
            for name, node in app_commands.items():
                rows = _command_rows(node, (name,))
                if rows:
                    blocks.append((name, rows))
        rows = _global_command_rows(global_commands, set(self._apps))
        if rows:
            blocks.append(('Global', rows))
        return blocks

    # ─── Config ──────────────────────────────────────────────────

    def _config_blocks(self, app: 'App | None', view: 'View | None',
                       ) -> list[tuple[str, list[tuple[str, str, str]]]]:
        blocks: list[tuple[str, list[tuple[str, str, str]]]] = []
        if app is not None and view is not None:
            rows = self._config_rows(f'{app.name}.{view.name}.')
            if rows:
                blocks.append((f'{app.name} - {view.name}', rows))
            leftovers = self._app_leftovers(app)
            if leftovers:
                blocks.append((app.name, leftovers))
        else:
            for owner in ([app] if app is not None else self._apps.values()):
                for name in owner.views:
                    rows = self._config_rows(f'{owner.name}.{name}.')
                    if rows:
                        blocks.append((f'{owner.name} - {name}', rows))
                leftovers = self._app_leftovers(owner)
                if leftovers:
                    blocks.append((owner.name, leftovers))
        rows = [self._config_row(key) for key in sorted(self._schema)
                if '.' not in key]
        if rows:
            blocks.append(('Global', rows))
        return blocks

    def _config_rows(self, prefix: str) -> list[tuple[str, str, str]]:
        return [self._config_row(key) for key in sorted(self._schema)
                if key.startswith(prefix)]

    def _app_leftovers(self, app: 'App') -> list[tuple[str, str, str]]:
        prefix = app.name + '.'
        view_prefixes = tuple(prefix + name + '.' for name in app.views)
        return [self._config_row(key) for key in sorted(self._schema)
                if key.startswith(prefix)
                and not key.startswith(view_prefixes)]

    def _config_row(self, key: str) -> tuple[str, str, str]:
        spec = self._schema[key]
        return key, spec.type, f'default={spec.default}'

    # ─── Rendering / scrolling ───────────────────────────────────

    def _all_views(self, app: 'App | None',
                   ) -> Iterator[tuple['App', 'View']]:
        owners = [app] if app is not None else list(self._apps.values())
        for owner in owners:
            for view in owner.views.values():
                yield owner, view

    @staticmethod
    def _render_section(lines: list[str], title: str,
                        blocks: list[tuple[str, list[tuple[str, ...]]]]) -> None:
        rows = [row for _, block in blocks for row in block]
        if not rows:
            return
        lines.append(f'  {title}')
        lines.append('  ' + '\u2500' * 60)
        lines.append('')
        columns = max(len(row) for row in rows)
        widths = [
            max((len(row[i]) if i < len(row) else 0) for row in rows)
            for i in range(columns)
        ]
        for name, block in blocks:
            lines.append(f'    {name}')
            for row in block:
                cells = [cell.ljust(widths[i]) for i, cell in enumerate(row)]
                lines.append(('      ' + '  '.join(cells)).rstrip())
            lines.append('')

    def render(self, body: 'Window') -> None:
        h = body.height
        visible = self._lines[self._scroll : self._scroll + h]
        body.update_buffer(visible)

    def scroll(self, delta: int, body_height: int,
               body: 'Window | None' = None) -> None:
        max_scroll = max(0, len(self._lines) - body_height)
        self._scroll = max(0, min(self._scroll + delta, max_scroll))
        if body is not None:
            self.render(body)

    @property
    def line_count(self) -> int:
        return len(self._lines)


def _global_command_rows(root: Command,
                         app_names: set[str]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for child in root.children.values():
        if child.name in app_names:
            continue
        rows.append((_command_label((), child), child.description))
        rows.extend(_command_rows(child, (child.name,)))
    return rows
