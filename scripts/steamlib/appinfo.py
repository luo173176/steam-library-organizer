"""Read-only parser for Steam's binary appinfo.vdf cache.

The file is only read; nothing under the Steam installation is written.
Used as an offline source of canonical app names/types and as evidence that
an AppID is known to the local client.

Two on-disk layouts exist:

* ``v1`` (Steam 2024+): magic, version=1, string-table offset (u64), then
  records of ``appid u32 | size u32 | 60-byte header | payload``. Keys inside
  the payload are ``u32`` indices into the string table, not inline strings.
* ``v27-v29`` (legacy): magic, version, string-table offset, then records of
  ``appid u32 | size u32 | header | payload`` with inline NUL-terminated keys.

``size`` counts the bytes after the 8-byte ``appid/size`` prefix, so the next
record starts at ``record_start + 8 + size``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

MAGIC = 0x07564429
_MAX_RECORDS = 200_000
# Bytes between ``size`` and the payload for the compact v1 layout.
V1_HEADER = 60


class AppInfoError(ValueError):
    pass


@dataclass
class AppInfoEntry:
    appid: int
    info_state: int
    last_updated: int
    change_number: int
    data: dict[str, Any]


class _Reader:
    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.buf):
            raise AppInfoError(f"truncated at {self.pos} (+{n})")
        out = self.buf[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.take(4))[0]

    def cstr(self) -> str:
        start = self.pos
        end = self.buf.find(b"\x00", start)
        if end < 0:
            raise AppInfoError("unterminated string")
        self.pos = end + 1
        return self.buf[start:end].decode("utf-8", "replace")


def _read_kv(r: _Reader, depth: int = 0) -> dict[str, Any]:
    if depth > 24:
        raise AppInfoError("nesting too deep")
    out: dict[str, Any] = {}
    while True:
        kind = r.u8()
        if kind == 0x08:
            return out
        if kind == 0x00:
            key = r.cstr()
            out[key] = _read_kv(r, depth + 1)
        elif kind == 0x01:
            key = r.cstr()
            out[key] = r.cstr()
        elif kind == 0x02:
            key = r.cstr()
            out[key] = r.i32()
        elif kind == 0x03:
            key = r.cstr()
            out[key] = r.f32()
        elif kind == 0x07:
            key = r.cstr()
            out[key] = r.u64()
        elif kind == 0x0A:
            key = r.cstr()
            out[key] = r.i64()
        elif kind == 0x0B:
            key = r.cstr()
            out[key] = r.u64()
        else:
            raise AppInfoError(f"unknown kv type 0x{kind:02x} at {r.pos - 1}")


def _read_kv_indexed(r: _Reader, strings: list[str], depth: int = 0) -> dict[str, Any]:
    """KeyValues where every key is a u32 index into ``strings``."""
    if depth > 40:
        raise AppInfoError("nesting too deep")
    out: dict[str, Any] = {}
    while True:
        kind = r.u8()
        if kind == 0x08:
            return out
        if kind not in (0x00, 0x01, 0x02, 0x03, 0x07, 0x0A, 0x0B):
            raise AppInfoError(f"unknown kv type 0x{kind:02x} at {r.pos - 1}")
        idx = r.u32()
        if idx >= len(strings):
            raise AppInfoError(f"string index {idx} out of range")
        key = strings[idx]
        if kind == 0x00:
            out[key] = _read_kv_indexed(r, strings, depth + 1)
        elif kind == 0x01:
            out[key] = r.cstr()
        elif kind == 0x02:
            out[key] = r.i32()
        elif kind == 0x03:
            out[key] = r.f32()
        elif kind == 0x07:
            out[key] = r.u64()
        elif kind == 0x0A:
            out[key] = r.i64()
        else:  # 0x0B
            out[key] = r.u64()


def _read_string_table(raw: bytes, offset: int) -> list[str]:
    """v1 string table: u32 count, then NUL-terminated UTF-8 strings."""
    if offset <= 0 or offset + 4 > len(raw):
        raise AppInfoError(f"bad string table offset {offset}")
    count = struct.unpack("<I", raw[offset : offset + 4])[0]
    if count > 500_000:
        raise AppInfoError(f"implausible string count {count}")
    pos = offset + 4
    out: list[str] = []
    for _ in range(count):
        end = raw.find(b"\x00", pos)
        if end < 0:
            raise AppInfoError("truncated string table")
        out.append(raw[pos:end].decode("utf-8", "replace"))
        pos = end + 1
    return out


def iter_entries(path: str | Path) -> Iterator[AppInfoEntry]:
    raw = Path(path).read_bytes()
    r = _Reader(raw)
    magic = r.u32()
    if magic != MAGIC:
        raise AppInfoError(f"unexpected magic 0x{magic:08x}")
    version = r.u32()
    if version not in (1, 27, 28, 29):
        raise AppInfoError(f"unsupported appinfo version {version}")
    string_table_offset = r.i64()

    if version == 1:
        yield from _iter_v1(raw, r.pos, string_table_offset)
        return

    count = 0
    while r.pos + 4 <= string_table_offset and count < _MAX_RECORDS:
        start = r.pos
        appid = r.u32()
        if appid == 0:
            break
        size = r.u32()
        info_state = r.u32()
        last_updated = r.u32()
        if version >= 28:
            r.u64()  # pics token
            r.take(20)  # sha1
            change_number = r.u32()
        else:
            change_number = 0
        if size == 0 or size > 64 * 1024 * 1024:
            raise AppInfoError(f"bad record size {size} at {start}")
        payload = r.take(size)
        try:
            kv = _read_kv(_Reader(payload))
        except AppInfoError:
            kv = {}
        yield AppInfoEntry(appid, info_state, last_updated, change_number, kv)
        count += 1


def _iter_v1(raw: bytes, start: int, string_table_offset: int) -> Iterator[AppInfoEntry]:
    strings = _read_string_table(raw, string_table_offset)
    pos = start
    limit = min(string_table_offset, len(raw))
    count = 0
    while pos + 8 <= limit and count < _MAX_RECORDS:
        appid, size = struct.unpack("<II", raw[pos : pos + 8])
        if appid == 0 or size == 0:
            break
        end = pos + 8 + size
        if end > len(raw) or size < V1_HEADER:
            raise AppInfoError(f"bad v1 record at {pos} (appid {appid}, size {size})")
        header = raw[pos + 8 : pos + 8 + V1_HEADER]
        info_state, last_updated = struct.unpack("<II", header[:8])
        # The remaining header bytes are a pics token + SHA-1, not a change
        # number; v1 does not expose one, so leave it at 0 rather than invent it.
        change_number = 0
        payload = raw[pos + 8 + V1_HEADER : end]
        try:
            kv = _read_kv_indexed(_Reader(payload), strings)
        except AppInfoError:
            kv = {}
        yield AppInfoEntry(appid, info_state, last_updated, change_number, kv)
        pos = end
        count += 1


def index_appinfo(path: str | Path) -> dict[int, dict[str, Any]]:
    """Return appid -> {name, type, developers, publishers, release_date}."""
    out: dict[int, dict[str, Any]] = {}
    for entry in iter_entries(path):
        app = entry.data.get("appinfo", entry.data)
        common = app.get("common", {}) if isinstance(app, dict) else {}
        name = common.get("name") if isinstance(common, dict) else None
        app_type = common.get("type") if isinstance(common, dict) else None
        if not name:
            continue
        out[entry.appid] = {
            "name": str(name),
            "type": str(app_type) if app_type else "",
            "last_updated": entry.last_updated,
            "info_state": entry.info_state,
        }
    return out


# appinfo.vdf writes capitalised type labels ("Game", "Tool", "Config", ...)
# while the store API and classify.py use the lowercase vocabulary.
_TYPE_MAP = {
    "game": "game",
    "application": "software",
    "tool": "tool",
    "demo": "demo",
    "dlc": "dlc",
    "config": "config",
    "music": "music",
    "video": "video",
    "hardware": "hardware",
    "series": "series",
    "beta": "beta",
    "mod": "mod",
    "advertising": "advertising",
}


def normalize_type(raw: str | None) -> str:
    """Map an appinfo/store type label onto the lowercase vocabulary."""
    key = str(raw or "").strip().casefold()
    return _TYPE_MAP.get(key, key)
