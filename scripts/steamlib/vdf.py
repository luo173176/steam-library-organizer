"""Minimal, dependency-free Valve KeyValues (VDF/ACF) parser.

Supports quoted and unquoted tokens, nested objects, duplicate keys,
escaped quotes/backslashes, and UTF-8 text. Never mutates input files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class VdfError(ValueError):
    """Raised when a VDF document is structurally invalid."""


@dataclass
class VdfObject(dict):
    """Dict that records duplicate keys and ordering."""

    duplicates: list[str] = field(default_factory=list)

    def add(self, key: str, value: Any) -> None:
        if key in self:
            self.duplicates.append(key)
        self[key] = value


_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}]|[^\s{}"]+', re.S)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unquote(token: str) -> str:
    body = token[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse(text: str) -> VdfObject:
    """Parse a KeyValues document into nested :class:`VdfObject` values."""
    tokens = _TOKEN.findall(text)
    if not tokens:
        raise VdfError("empty document")
    pos = 0

    def parse_block(expect_close: bool) -> VdfObject:
        nonlocal pos
        obj = VdfObject()
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                if not expect_close:
                    raise VdfError("unexpected closing brace")
                pos += 1
                return obj
            if tok == "{":
                raise VdfError("object without key")
            key = _unquote(tok) if tok.startswith('"') else tok
            pos += 1
            if pos >= len(tokens):
                raise VdfError(f"missing value for key {key!r}")
            val = tokens[pos]
            if val == "{":
                pos += 1
                obj.add(key, parse_block(True))
            elif val == "}":
                raise VdfError(f"missing value for key {key!r}")
            else:
                obj.add(key, _unquote(val) if val.startswith('"') else val)
                pos += 1
        if expect_close:
            raise VdfError("unterminated object")
        return obj

    root = parse_block(False)
    if len(root) != 1:
        raise VdfError(f"expected exactly one root key, got {len(root)}")
    return root


def parse_file(path: str, encoding: str = "utf-8") -> VdfObject:
    with open(path, "r", encoding=encoding, errors="replace") as handle:
        return parse(handle.read())


def first_value(obj: Any, key: str, default: Any = None) -> Any:
    """Case-insensitive lookup tolerant of nested single-root documents."""
    if not isinstance(obj, dict):
        return default
    if key in obj:
        return obj[key]
    lowered = key.lower()
    for k, v in obj.items():
        if isinstance(k, str) and k.lower() == lowered:
            return v
    return default


def flatten_app_state(doc: VdfObject) -> VdfObject:
    """Return the AppState object from a manifest document."""
    for key, value in doc.items():
        if isinstance(value, dict) and key.lower() == "appstate":
            return value
    for value in doc.values():
        if isinstance(value, dict):
            return value
    raise VdfError("no AppState object")
