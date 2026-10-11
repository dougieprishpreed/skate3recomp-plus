"""Validate original speech event banks before assigning an isolated namespace.

The event programs and their sample selections remain original. The native
registration routines 82EACA58/82EACC30 read the namespace from bytes 8/9,
the program-offset table at 24, and 32-byte named callback records. This does
not implement speech playback or claim that a bank has been registered.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct
import mmap

PREFERRED_NAMESPACE = 0x4E53


@dataclass(frozen=True)
class SpeechEvent:
    name: str
    ident: int
    binding: int
    program: int


@dataclass(frozen=True)
class SpeechBank:
    namespace: int
    events: tuple[SpeechEvent, ...]


def read_events(data: bytes) -> SpeechBank:
    if len(data) < 24 or data[:4] != bytes.fromhex('0315b007'):
        raise ValueError('unsupported speech event bank')
    names_table = struct.unpack_from('>I', data, 4)[0]
    count = struct.unpack_from('>H', data, 16)[0]
    # Runtime relocation mutates byte 11 and the callback records. Accept only
    # on-disc input, never a dump containing guest pointers or registered state.
    if data[10:12] != b'\0\0' or data[18:20] != b'\0\0':
        raise ValueError('registered or unsupported speech event bank')
    programs_start = (24 + count * 2 + 3) & ~3
    strings = names_table + count * 32
    if not count or names_table % 4 or not programs_start <= names_table < strings <= len(data):
        raise ValueError('speech event tables outside bank')
    events, names, identifiers, bindings = [], set(), set(), set()
    previous = programs_start - 1
    for i in range(count):
        program = struct.unpack_from('>H', data, 24 + i * 2)[0] * 4
        if not previous < program or program < programs_start or program + 8 > names_table:
            raise ValueError('invalid speech event program offset')
        previous = program
        ident = struct.unpack_from('>H', data, program)[0]
        offset, binding, *reserved = struct.unpack_from('>8I', data, names_table + i * 32)
        start = strings + offset
        end = data.find(b'\0', start) if start < len(data) else -1
        if end <= start or any(reserved):
            raise ValueError('invalid speech event callback record')
        raw = data[start:end]
        if any(c < 32 or c > 126 for c in raw):
            raise ValueError('invalid speech event name')
        name = raw.decode('ascii')
        if name in names or ident in identifiers or binding in bindings:
            raise ValueError('duplicate speech event name, ID or binding')
        names.add(name)
        identifiers.add(ident)
        bindings.add(binding)
        events.append(SpeechEvent(name, ident, binding, program))
    # PPC registration puts byte 8 in the low byte of the dispatched event ID.
    return SpeechBank(int.from_bytes(data[8:10], 'little'), tuple(events))


def isolate_events(data: bytes, namespace: int, occupied: set[int]) -> bytes:
    """Assign an explicitly reserved namespace; do not alter program bytecode.

    The caller must also load the matching original hdr/sth/sample bank and
    route actor events to this namespace. Both retail living-world banks use
    namespace 1, so substituting one unchanged would conflict at registration.
    """
    source = read_events(data)
    if not 0 <= namespace <= 0xffff or namespace in occupied or namespace == source.namespace:
        raise ValueError('speech namespace is invalid or already occupied')
    result = bytearray(data)
    result[8:10] = namespace.to_bytes(2, 'little')
    parsed = read_events(result)
    if parsed.events != source.events or parsed.namespace != namespace:
        raise ValueError('speech namespace adaptation changed event programs')
    return bytes(result)


def native_namespaces(native_data):
    """Read namespaces, not playback bank IDs, from the installed native banks."""
    from world_archive import entries, decode_archive_payload
    occupied = set()
    for name in ('maincast', 'livingworld', 'cameraman', 'announcer'):
        path = native_data/'audio/english'/(name+'speech.big')
        with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            banks = [entry for entry in entries(data) if entry.name.endswith('.evt')]
            if len(banks) != 1:
                raise ValueError('missing or ambiguous native speech event bank: '+str(path))
            entry = banks[0]
            event = decode_archive_payload(data[entry.offset:entry.offset+entry.size])
            occupied.add(read_events(event).namespace)
    return occupied


SAMPLE_ID_BIAS = 0x8000


def prepare_tables(header, stream, native_data):
    """Adapt directory layout and sample identity; preserve encoded audio."""
    from world_archive import entries, decode_archive_payload, build_eb_archive
    original = [entries(header), entries(stream)]
    native_ids = set()
    for name in ('maincast', 'livingworld', 'cameraman', 'announcer'):
        path = native_data/'audio/english'/(name+'speech.big')
        with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            tables = [e for e in entries(raw) if e.name.endswith('hdr.big')]
            if len(tables) != 1:
                raise ValueError('missing native speech headers')
            e = tables[0]
            table = decode_archive_payload(raw[e.offset:e.offset+e.size])
            native_ids.update(struct.unpack_from('>H', table, e.offset)[0] for e in entries(table))
    files, ids = [], set()
    for e in original[0]:
        data = bytearray(header[e.offset:e.offset+e.size])
        if len(data) < 16:
            raise ValueError('truncated speech sample header')
        ident = struct.unpack_from('>H', data)[0]
        mapped = ident + SAMPLE_ID_BIAS
        if mapped > 65535 or mapped <= max(native_ids) or ident in ids:
            raise ValueError('conflicting speech sample identity')
        ids.add(ident)
        struct.pack_into('>H', data, 0, mapped)
        files.append((e.name, data))
    hdr = build_eb_archive(files)
    sth = build_eb_archive([(e.name, stream[e.offset:e.offset+e.size]) for e in original[1]])
    # Native loader pairs by index, not by filename. Extension hashing must
    # preserve the same order for both tables; do not assume this for new data.
    h, t = entries(hdr), entries(sth)
    if len(h) != len(t) or any(a.name[:-4] != b.name[:-4] for a, b in zip(h, t)):
        raise ValueError('speech header/stream index mismatch')
    return hdr, sth, ids
