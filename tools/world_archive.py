"""Strict, indexed readers for the local Skate BIG4 and EB3 archives.

An archive index is not a world registration or a payload compatibility test.
Offsets always come from file records, never distances between marker strings.
"""
from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    name: str
    offset: int
    size: int                   # stored bytes
    hash: int | None = None
    expanded: int | None = None  # RefPack-expanded size; None when stored raw


def u32(data, offset):
    return struct.unpack_from('>I', data, offset)[0]


def path_hash(name):
    # Verified against every indexed path in the original BlackBox archive.
    # Root-directory files ('./name', e.g. data/audio/ambience.big) hash
    # without the './' (as tools/build_nsv_dlc.py writes retail DLC archives).
    name = name.replace('\\', '/')
    if name.startswith('./'):
        name = name[2:]
    value = 5381
    for byte in name.encode('ascii'):
        value = (value * 33 + byte) & 0xffffffff
    return value


def big_entries(data):
    if len(data) < 16 or data[:4] not in (b'BIG4', b'BIGF'):
        raise ValueError('expected BIG4/BIGF')
    count, end = struct.unpack_from('>II', data, 8)
    if not 16 <= end <= len(data) or count > (end - 16) // 9:
        raise ValueError('invalid BIG directory bounds')
    result, pos = [], 16
    for _ in range(count):
        if pos + 8 >= end:
            raise ValueError('truncated BIG record')
        offset, size = struct.unpack_from('>II', data, pos)
        stop = data.find(b'\0', pos + 8, end)
        if stop < 0:
            raise ValueError('unterminated BIG filename')
        name = data[pos + 8:stop].decode('ascii')
        if not name or offset < end or offset + size > len(data):
            raise ValueError(f'invalid BIG entry: {name!r} {offset=} {size=}')
        result.append(Entry(name.replace('\\', '/'), offset, size))
        pos = stop + 1
    return result


def eb_entries(data):
    if len(data) < 48 or data[:4] != b'EB\0\3':
        raise ValueError('expected EB3')
    count, family, names_at, names_size, metadata, reserved, size = struct.unpack_from('>7I', data, 4)
    # Family byte 1 is the file alignment shift: 0x0400 worlds (16-byte
    # blocks), 0x0600 db/audio archives (64-byte blocks).
    if family not in (0x00100400, 0x00100600) or reserved != 0 or size != len(data):
        raise ValueError('unsupported EB3 header family/reserved/size')
    shift = (family >> 8) & 0xFF
    width, directory_width, directories = metadata >> 24, (metadata >> 16) & 255, metadata & 65535
    end = names_at + names_size
    if width < 4 or not directories or not directory_width:
        raise ValueError('unsupported EB3 name geometry')
    if 48 + count * 16 > names_at or end > len(data):
        raise ValueError('EB3 binary index overlaps names or exceeds file')
    dirs_at = (names_at + count * width + 15) & ~15
    if dirs_at + directories * directory_width > end:
        raise ValueError('EB3 names/directories exceed table')
    dirs = []
    for index in range(directories):
        slot = data[dirs_at + index * directory_width:dirs_at + (index + 1) * directory_width]
        if b'\0' not in slot:
            raise ValueError('unterminated EB3 directory')
        dirs.append(slot.split(b'\0', 1)[0].decode('ascii'))
    result, previous = [], -1
    for index in range(count):
        block, high, length, hashed = struct.unpack_from('>4I', data, 48 + index * 16)
        slot = data[names_at + index * width:names_at + (index + 1) * width]
        directory = struct.unpack_from('>H', slot)[0]
        if directory >= directories or b'\0' not in slot[2:]:
            raise ValueError('invalid EB3 filename slot')
        name = dirs[directory] + '/' + slot[2:].split(b'\0', 1)[0].decode('ascii')
        offset = block << shift
        # A non-zero second word is the stored size of a RefPack-compressed
        # file (db/audio archives); the third word is then its expanded size.
        stored = high or length
        if offset < end or offset + stored > len(data):
            raise ValueError(f'invalid EB3 file range: {name}')
        if hashed != path_hash(name) or hashed <= previous:
            raise ValueError(f'EB3 path hash mismatch/duplicate/unsorted: {name}')
        previous = hashed
        result.append(Entry(name, offset, stored, hashed, length if high else None))
    return result


def entries(data):
    return eb_entries(data) if data[:4] == b'EB\0\3' else big_entries(data)


def catalog(data):
    """Check indexed section sizes, including the real 8-byte CMAP prefix."""
    result = []
    expected = {'.xmm': (b'MMAP', 0, 24), '.xst': (b'ATOC', 64, 24),
                '.xsm': (b'CMAP', 104, 32), '.xss': (b'CSPA', 80, 24),
                '.xsf': (b'SFIL', None, None)}
    for entry in entries(data):
        suffix = Path(entry.name).suffix.lower()
        if suffix not in expected:
            continue
        magic, stride, header = expected[suffix]
        if entry.size < 24 or data[entry.offset:entry.offset + 4] != magic:
            raise ValueError(f'{entry.name}: expected {magic!r}')
        version, _, group, count = struct.unpack_from('>4I', data, entry.offset + 4)
        if version != 3:
            raise ValueError(f'{entry.name}: unknown section version {version}')
        if stride is not None and entry.size != header + count * stride:
            raise ValueError(f'{entry.name}: section size {entry.size} != {header}+{count}*{stride}')
        result.append(dict(name=entry.name, offset=entry.offset, size=entry.size,
                           magic=magic.decode(), version=version, group=f'{group:08X}',
                           count=count if stride else None))
    return result


def inspect(path):
    with Path(path).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        return dict(path=str(path), size=len(data), entries=len(entries(data)), sections=catalog(data))


def decode_archive_payload(raw):
    """Decode raw, RefPack or chunkref archive entries without research imports."""
    from inspect_sfil import unpack
    if raw[:2] == b'\x10\xfb':
        return unpack(raw)
    if raw[:8] != b'chunkref':
        return raw
    version, expanded, block_size, count, width = struct.unpack_from('>5I', raw, 8)
    assert version == 2 and width == 16 and block_size == 0x20000
    at, chunks = 32 + 16 * count, []
    assert at <= len(raw)
    for i in range(count):
        stored, codec = struct.unpack_from('>II', raw, 32 + 16 * i + 8)
        assert at + stored <= len(raw) and codec == 2
        chunks.append(unpack(raw[at:at+stored]))
        at += stored
    result = b''.join(chunks)
    assert at == len(raw) and len(result) == expanded
    return result



def build_eb_archive(files):
    """Pack uncompressed payloads in the native EB3 fixed-index format.

    Unlike world-stream conversion this relocates payloads, so use only where
    file offsets are archive-local. Root names use ./, as in retail audio.

    The 16-byte index is followed by one storage-mode byte per entry, padded to
    16, before the names table. The game's EB3 lookup (82A823E8) reads that
    byte at index_start + align16(count * 16) + index and the BigFile read
    (82A841F0) treats 0 as raw, 1 as RefPack, 2-4 as chunked and any other
    value as no data. Retail archives write zeros there; without the array the
    names table was read as modes and most entries returned no data (log147).
    """
    def align(n):
        return (n + 15) & ~15
    files = [(name.replace('\\', '/'), bytes(data)) for name, data in files]
    files = [('./' + name if '/' not in name else name, data) for name, data in files]
    files.sort(key=lambda row: path_hash(row[0]))
    if not files or len({path_hash(n) for n, _ in files}) != len(files):
        raise ValueError('empty archive or duplicate/colliding paths')
    dirs = sorted({name.rsplit('/', 1)[0] for name, _ in files})
    width = max(len(name.rsplit('/', 1)[1].encode('ascii')) for name, _ in files) + 3
    dwidth = max(len(name.encode('ascii')) for name in dirs) + 1
    if width > 255 or dwidth > 255 or len(dirs) > 65535:
        raise ValueError('unsupported EB3 name geometry')
    modes_at = align(48 + 16 * len(files))  # all zero: stored raw
    names_at = align(modes_at + len(files))
    dirs_at = align(names_at + width * len(files))
    end = align(dirs_at + dwidth * len(dirs))
    out = bytearray(end)
    for i, (name, data) in enumerate(files):
        offset = len(out)
        out.extend(data)
        out.extend(bytes(align(len(out)) - len(out)))
        struct.pack_into('>4I', out, 48 + i * 16, offset >> 4, 0, len(data), path_hash(name))
        folder, leaf = name.rsplit('/', 1)
        encoded = struct.pack('>H', dirs.index(folder)) + leaf.encode('ascii') + b'\0'
        at = names_at + i * width
        out[at:at + len(encoded)] = encoded
    for i, folder in enumerate(dirs):
        encoded = folder.encode('ascii') + b'\0'
        at = dirs_at + i * dwidth
        out[at:at + len(encoded)] = encoded
    struct.pack_into('>4s7I', out, 0, b'EB\0\3', len(files), 0x00100400,
                     names_at, end - names_at, width << 24 | dwidth << 16 | len(dirs), 0, len(out))
    eb_entries(out)
    return bytes(out)
