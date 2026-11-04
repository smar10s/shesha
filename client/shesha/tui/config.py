"""Write-only key-value config backed by JSON, with schema validation.

Keys are flat or dot-scoped (app.key). Values are Any (strings, ints,
floats, lists). Radio state lives in the daemon; config is the user's
preferences, loaded once at startup and persisted at exit.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from shesha.protocol import ACCUM_CODES, WINDOW_CODES

from .theme import STYLES, DEFAULT_STYLE


# ─── KeySpec ─────────────────────────────────────────────────────────────

@dataclass
class KeySpec:
    """Defines a valid config key with type, constraints, and defaults."""
    type: str                                      # 'str' | 'int' | 'float' | 'bool' | 'gain_val' | 'fft_size' | 'window_type' | 'accum_mode'
    default: Any = None
    allowed: list[str] | None = None               # discrete value validation
    min: int | float | None = None
    max: int | float | None = None
    confirmed: bool = False                        # True = persists only on hardware RESP_OK

    _WINDOW_TYPE_NAMES: dict[str, str] = field(default_factory=lambda: {
        **{name: str(int(code)) for name, code in WINDOW_CODES.items()},
        **{name.replace('-', '_'): str(int(code))
           for name, code in WINDOW_CODES.items()},
        **{str(int(code)): str(int(code))
           for code in WINDOW_CODES.values()},
    }, repr=False, hash=False, compare=False)

    _ACCUM_MODE_NAMES: dict[str, str] = field(default_factory=lambda: {
        **{name: str(int(code)) for name, code in ACCUM_CODES.items()},
        **{str(int(code)): str(int(code))
           for code in ACCUM_CODES.values()},
    }, repr=False, hash=False, compare=False)

    _VALID_FFT_SIZES: set[int] = field(default_factory=lambda: {256, 512, 1024, 2048, 4096, 8192},
                                       repr=False, hash=False, compare=False)

    _BOOL_TRUE: frozenset[str] = field(
        default_factory=lambda: frozenset({'true', '1', 'yes', 'on'}),
        repr=False, hash=False, compare=False)
    _BOOL_FALSE: frozenset[str] = field(
        default_factory=lambda: frozenset({'false', '0', 'no', 'off'}),
        repr=False, hash=False, compare=False)

    def validate(self, value: Any) -> tuple[bool, str]:
        """Return (valid, error_message)."""
        s = str(value)

        if self.type == 'int':
            try:
                _vi = int(s)
            except (ValueError, TypeError):
                return False, f'{value}: not an integer'
            if self.min is not None and _vi < self.min:
                return False, f'{_vi} < {self.min}'
            if self.max is not None and _vi > self.max:
                return False, f'{_vi} > {self.max}'
            return True, ''

        if self.type == 'float':
            try:
                _vf = float(s)
            except (ValueError, TypeError):
                return False, f'{value}: not a number'
            if self.min is not None and _vf < self.min:
                return False, f'{_vf} < {self.min}'
            if self.max is not None and _vf > self.max:
                return False, f'{_vf} > {self.max}'
            return True, ''

        if self.type == 'gain_val':
            if s in ('auto', 'fast', 'fast_attack', 'slow', 'slow_attack'):
                return True, ''
            try:
                float(s)
            except (ValueError, TypeError):
                return False, f'{value}: must be number or auto/fast/slow'
            return True, ''

        if self.type == 'fft_size':
            try:
                v = int(s)
            except (ValueError, TypeError):
                return False, f'{value}: not an integer'
            if v not in self._VALID_FFT_SIZES:
                return False, f'{v}: must be 256/512/1024/2048/4096/8192'
            return True, ''

        if self.type == 'window_type':
            if s.lower() not in self._WINDOW_TYPE_NAMES:
                return False, f'{value}: must be rect/hann/blackman_harris/flat_top or 0-3'
            return True, ''

        if self.type == 'accum_mode':
            if s.lower() not in self._ACCUM_MODE_NAMES:
                return False, f'{value}: must be none/max/avg or 0-2'
            return True, ''

        if self.type == 'bool':
            if s.lower() not in self._BOOL_TRUE | self._BOOL_FALSE:
                return False, f'{value}: must be true/false'
            return True, ''

        # 'str' and 'list' pass through
        if self.allowed is not None and s not in self.allowed:
            return False, f'{value}: must be one of {", ".join(self.allowed)}'
        return True, ''

    def normalize(self, value: Any) -> Any:
        """Return canonical form of value."""
        s = str(value)

        if self.type in ('int', 'fft_size'):
            return str(int(s))

        if self.type == 'float':
            return str(float(s))

        if self.type == 'gain_val':
            return s

        if self.type == 'window_type':
            return self._WINDOW_TYPE_NAMES[s.lower()]

        if self.type == 'accum_mode':
            return self._ACCUM_MODE_NAMES[s.lower()]

        if self.type == 'bool':
            return 'true' if s.lower() in self._BOOL_TRUE else 'false'

        if self.type == 'list':
            return value if isinstance(value, list) else []

        return s


# ─── Global key registry ─────────────────────────────────────────────────

KEY_SPECS: dict[str, KeySpec] = {
    'theme':               KeySpec('str', default=DEFAULT_STYLE,
                                   allowed=list(STYLES.keys())),
    'freq':                KeySpec('int', default=5_180_000_000, min=69_000_000, max=6_100_000_000, confirmed=True),
    'gain':                KeySpec('gain_val', default='50', confirmed=True),
    'tx_gain':             KeySpec('float', default=0.0, min=-89.75, max=0.0, confirmed=True),
    'bw':                  KeySpec('int', default=20_000_000, min=200_000, max=56_000_000, confirmed=True),
    'sample_rate':         KeySpec('int', default=20_000_000, min=520_833, max=61_440_000, confirmed=True),
    'fps':                 KeySpec('int', default=30, min=1, max=100),
    'workers':             KeySpec('int', default=1, min=1, max=2),
    'cmd_history':         KeySpec('list', default=[]),
    'cmd_history_length':  KeySpec('int', default=100, min=0),
}


# ─── Config ──────────────────────────────────────────────────────────────

class Config:
    """Write-only key-value config backed by JSON.

    Validates all set() calls against KEY_SPECS registry. Unknown keys
    are rejected. Values are normalized to their canonical form.
    Loaded once at startup; radio values are seeded into a freshly
    started daemon, never pushed into a running one. `persist()` writes
    daemon-confirmed state back to disk.
    """

    _schema: dict[str, KeySpec] = dict(KEY_SPECS)

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._data: dict[str, Any] = {}
        self._load()

    @classmethod
    def register_keys(cls, specs: dict[str, KeySpec]) -> None:
        cls._schema.update(specs)

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    self._data = json.load(f)
            except json.JSONDecodeError:
                self._data = {}

    def _save(self) -> None:
        with open(self.path, 'w') as f:
            json.dump(self._data, f, indent=2, sort_keys=True)
            f.write('\n')

    def get(self, key: str, default: Any = '') -> Any:
        if key in self._data:
            return self._data[key]
        spec = self._schema.get(key)
        if spec is not None:
            return spec.default
        return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self._data.get(key, ''))
        except (ValueError, TypeError):
            return default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._data.get(key, ''))
        except (ValueError, TypeError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Truthiness of *key*, accepting stored strings or bool defaults."""
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).lower() in ('true', '1', 'yes', 'on')

    def get_normalized(self, key: str) -> str | None:
        """Canonical string form of *key*'s value, or None if unset.

        Falls back to the schema default, normalized the same way a stored
        value would be — so symbolic defaults (e.g. window ``hann``) become
        their wire form instead of leaking into the daemon as text.
        """
        spec = self._schema.get(key)
        if spec is None:
            return None
        value = self.get(key)
        if value in (None, ''):
            return None
        return str(spec.normalize(value))

    @classmethod
    def confirmed_keys(cls) -> list[str]:
        """Config keys whose value is hardware-confirmed radio state."""
        return [k for k, spec in cls._schema.items() if spec.confirmed]

    def set(self, key: str, value: Any) -> None:
        spec = self._schema.get(key)
        if spec is None:
            raise ValueError(f"unknown config key: '{key}'")
        if spec.type != 'list':
            valid, err = spec.validate(value)
            if not valid:
                raise ValueError(f"invalid value for '{key}': {err}")
        value = spec.normalize(value)
        self._data[key] = value
        if not spec.confirmed:
            self._save()

    def persist(self, key: str, value: Any) -> None:
        """Write daemon-confirmed state to disk (no observers to fire)."""
        spec = self._schema.get(key)
        if spec is None:
            return
        if spec.type != 'list':
            valid, _ = spec.validate(value)
            if not valid:
                return
        self._data[key] = spec.normalize(value)
        self._save()

    def keys(self) -> list[str]:
        return list(self._data.keys())

    def set_raw(self, key: str, value: Any) -> None:
        """Set a value without validation or notification. For internal list data."""
        self._data[key] = value
        self._save()

    def delete_key(self, key: str) -> None:
        """Remove a key from stored data."""
        if key in self._data:
            del self._data[key]
            self._save()
