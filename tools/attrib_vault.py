"""Read-only Attrib vault (.vlt + .bin) reader for Skate 2 and Skate 3.

Big-endian layout, as written by tools/build_nsv_dlc.py: the .vlt holds the
chunks Vers, StrN, DepN, DatN (records), ExpN (exports) and PtrN (pointer
fixups); the .bin holds StrE (strings) and the data area. A record is
  u64 collection, u64 class, u64 parent, u32 entries, u32 shift, u32 entries,
  u16 types, u16 type slots, u32 layout, u32 0, u64 type keys[slots],
  entries x (u64 attribute, u32 value, u16 type index, u16 flags)
and an entry's value is inline unless PtrN redirects it into the .bin.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    key: int
    value: int
    type: int | None
    flags: int
    target: int | None     # .bin offset when the value is a pointer


@dataclass(frozen=True)
class Record:
    collection: int
    cls: int
    parent: int
    entries: tuple
    types: tuple = ()          # the record's type keys (layout fields first)
    layout: int | None = None  # .bin offset of the fixed-field block


def _chunks(data):
    pos = 0
    while pos + 8 <= len(data):
        size = struct.unpack_from('>I', data, pos + 4)[0]
        if size < 8 or pos + size > len(data):
            raise ValueError(f'invalid vault chunk at {pos:#x}')
        yield data[pos:pos + 4], pos, size
        pos += size


class Vault:
    def __init__(self, vlt, binf):
        self.vlt, self.bin = vlt, binf
        chunks = {tag: (pos, size) for tag, pos, size in _chunks(vlt)}
        if b'ExpN' not in chunks or b'PtrN' not in chunks:
            raise ValueError('vault without ExpN/PtrN')
        self.pointers = {}
        pos, size = chunks[b'PtrN']
        cursor, space = pos + 8, None
        while cursor + 16 <= pos + size:
            offset, kind, flags, _, target = struct.unpack_from('>IHHII', vlt, cursor)
            cursor += 16
            if kind == 2:        # selects the space later offsets live in
                space = 'bin' if flags == 1 else 'vlt'
            elif kind == 3:
                self.pointers[(space, offset)] = target
        pos, _ = chunks[b'ExpN']
        count = struct.unpack_from('>I', vlt, pos + 12)[0]
        self.exports = [struct.unpack_from('>QQII', vlt, pos + 16 + i * 24) for i in range(count)]

    def string(self, offset):
        """Printable C string at a .bin offset, else None."""
        if offset is None or not 0 <= offset < len(self.bin):
            return None
        end = self.bin.find(b'\0', offset)
        raw = self.bin[offset:end if end >= 0 else len(self.bin)]
        if not raw or any(c < 32 or c > 126 for c in raw):
            return None
        return raw.decode('latin1')

    def records(self):
        for _, _, _, at in self.exports:
            coll, cls, parent, count, _, _, ntypes, slots, _, _ = struct.unpack_from(
                '>QQQIIIHHII', self.vlt, at)
            types = struct.unpack_from(f'>{slots}Q', self.vlt, at + 48)
            base = at + 48 + slots * 8
            entries = []
            for i in range(count):
                key, value, tindex, flags = struct.unpack_from('>QIHH', self.vlt, base + i * 16)
                entries.append(Entry(key, value, types[tindex] if tindex < len(types) else None,
                                     flags, self.pointers.get(('vlt', base + i * 16 + 8))))
            yield Record(coll, cls, parent, tuple(entries), types[:ntypes],
                         self.pointers.get(('vlt', at + 40)))

    def strings_of(self, record):
        return [s for s in (self.string(e.target) for e in record.entries) if s]
