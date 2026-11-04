"""Command tree shared by resolution and tab completion.

The first token resolves against the global root, then against the active
app.  Later tokens descend only inside the chosen branch; the first token
that is not a child becomes an argument.  Completion walks the same tree,
so command names and argument candidates come from one description.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .completion import Completion, token_at, tokenize


Runner = Callable[[list[str]], None]
Completer = Callable[[str], list[str]]


@dataclass
class Command:
    name: str
    run: Runner | None = None
    description: str = ''
    aliases: tuple[str, ...] = ()
    children: dict[str, 'Command'] = field(default_factory=dict)
    complete: Completer | None = None
    usage: str = ''

    def add(self, *children: 'Command') -> 'Command':
        for child in children:
            self.children[child.name] = child
        return self

    def child(self, token: str) -> 'Command | None':
        node = self.children.get(token)
        if node is not None:
            return node
        for child in self.children.values():
            if token in child.aliases:
                return child
        return None

    def candidates(self, partial: str) -> list[str]:
        return [name for name in self.children if name.startswith(partial)]

    def arguments(self, text: str) -> list[str]:
        return self.complete(text) if self.complete is not None else []


@dataclass(frozen=True)
class Resolution:
    command: Command
    args: list[str]


def resolve(root: Command, active: Command | None,
            text: str) -> Resolution | None:
    tokens = text.split()
    if not tokens:
        return None
    node = root.child(tokens[0])
    if node is None and active is not None:
        node = active.child(tokens[0])
    if node is None:
        return None
    args = tokens[1:]
    while args:
        child = node.child(args[0])
        if child is None:
            break
        node = child
        args = args[1:]
    return Resolution(node, args)


def _node_for(root: Command, active: Command | None,
              token: str) -> Command | None:
    node = root.child(token)
    if node is None and active is not None:
        node = active.child(token)
    return node


def complete_buffer(root: Command, active: Command | None,
                    buffer: str, cursor: int) -> Completion:
    start, end = token_at(buffer, cursor)
    partial = buffer[start:cursor]
    before = [span for span in tokenize(buffer) if span[2] <= start]

    if not before:
        candidates = root.candidates(partial)
        if active is not None:
            candidates += active.candidates(partial)
        return Completion(start, end, list(dict.fromkeys(candidates)))

    node = _node_for(root, active, before[0][0])
    if node is None:
        return Completion(start, end, [])

    matched_end = before[0][2]
    descending = True
    arg_start = start
    for token, token_start, token_end in before[1:]:
        child = node.child(token) if descending else None
        if child is None:
            descending = False
            arg_start = token_start
            break
        node = child
        matched_end = token_end

    if descending:
        arg_start = matched_end
        while arg_start < len(buffer) and buffer[arg_start].isspace():
            arg_start += 1

    children = node.candidates(partial) if descending else []
    arguments: list[str] = []
    if node.complete is not None and cursor >= arg_start:
        text = buffer[arg_start:cursor]
        arguments = [c for c in node.arguments(text) if c.startswith(text)]

    if arguments:
        return Completion(arg_start, len(buffer),
                          list(dict.fromkeys(children + arguments)))
    return Completion(start, end, children)
