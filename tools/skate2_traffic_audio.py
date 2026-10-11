"""Retarget original ABK external bindings to the resident native CSI registry.

Only the ID pairs addressed by the ABK's external relocation table change.
Graphs, samples, strings and relocation targets keep their original bytes.
"""
from __future__ import annotations

import struct


def cstring(data, offset):
    if not 0 <= offset < len(data):
        raise ValueError('external binding string outside file')
    end = data.find(b'\0', offset)
    if end < 0:
        raise ValueError('unterminated external binding string')
    raw = data[offset:end]
    if not raw or any(c < 32 or c > 126 for c in raw):
        raise ValueError('invalid external binding name')
    return raw.decode('ascii')


def csi_bindings(data):
    if len(data) < 44 or data[:8] != b'MOIR\0\x03\x01\0':
        raise ValueError('unsupported CSI registry')
    callbacks, cues = struct.unpack_from('>HH', data, 10)
    registry = struct.unpack_from('>H', data, 16)[0]
    result = {}
    for i in range(callbacks + cues):
        at = 44 + i * 12
        if at + 6 > len(data):
            raise ValueError('truncated CSI registry')
        offset, ident = struct.unpack_from('>IH', data, at)
        name = cstring(data, offset)
        kind = 2 if i < callbacks else 1
        key = kind, name
        if key in result:
            raise ValueError('duplicate CSI binding')
        result[key] = registry, ident
    return result


def abk_bindings(data):
    if len(data) < 0x5c or data[:16] != bytes.fromhex('41424b43010102020503000100000000'):
        raise ValueError('unsupported traffic ABK')
    if struct.unpack_from('>I', data, 0x14)[0] != len(data):
        raise ValueError('traffic ABK size mismatch')
    table = struct.unpack_from('>I', data, 0x38)[0]
    if not 0x5c <= table <= len(data) - 4:
        raise ValueError('invalid ABK external relocation table')
    count = struct.unpack_from('>I', data, table)[0]
    if count > (len(data) - table - 4) // 12:
        raise ValueError('truncated ABK external relocation table')
    for i in range(count):
        target, reference, tags = struct.unpack_from('>III', data, table + 4 + i * 12)
        kind = tags >> 24
        if kind not in (1, 2) or target + 8 > table or reference < table + 4 + count * 12:
            raise ValueError('invalid traffic ABK external binding')
        if reference + 4 > len(data):
            raise ValueError('external reference outside ABK')
        yield dict(kind=kind, name=cstring(data, reference + 4), offset=reference,
                   target=target, ids=struct.unpack_from('>HH', data, reference))


def retarget_traffic_bank(data, source_csi, native_csi):
    source, native = csi_bindings(source_csi), csi_bindings(native_csi)
    if source.keys() != native.keys():
        raise ValueError('traffic CSI name/kind sets differ')
    patched, bindings = bytearray(data), []
    for binding in abk_bindings(data):
        key = binding['kind'], binding['name']
        if key not in source or binding['ids'] != source[key]:
            raise ValueError('traffic ABK reference does not match source CSI')
        replacement = native[key]
        struct.pack_into('>HH', patched, binding['offset'], *replacement)
        bindings.append(dict(**binding, native_ids=replacement))
    if not bindings:
        raise ValueError('traffic ABK has no external bindings')
    return bytes(patched), bindings
