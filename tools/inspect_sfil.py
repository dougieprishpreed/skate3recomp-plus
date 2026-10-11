"""Read original SFIL assets and RefPack blocks without modifying game data."""
import struct


def unpack(data):
    if data[:2] != b'\x10\xfb':
        raise ValueError('expected RefPack header')
    size = int.from_bytes(data[2:5], 'big')
    if size > 128 * 1024 * 1024:
        raise ValueError('oversized RefPack block')
    pos, out = 5, bytearray()
    while pos < len(data):
        c = data[pos]
        pos += 1
        if c >= 252:
            lit, length, offset = c & 3, 0, 0
        elif c >= 224:
            lit, length, offset = ((c & 31) << 2) + 4, 0, 0
        elif c >= 192:
            a, b, e = data[pos:pos + 3]
            pos += 3
            lit, length = c & 3, ((c & 12) << 6) + e + 5
            offset = ((c & 16) << 12) + (a << 8) + b + 1
        elif c >= 128:
            a, b = data[pos:pos + 2]
            pos += 2
            lit, length, offset = a >> 6, (c & 63) + 4, ((a & 63) << 8) + b + 1
        else:
            a = data[pos]
            pos += 1
            lit, length, offset = c & 3, ((c & 28) >> 2) + 3, ((c & 96) << 3) + a + 1
        if pos + lit > len(data) or len(out) + lit + length > size:
            raise ValueError('RefPack block exceeds declared bounds')
        out.extend(data[pos:pos + lit])
        pos += lit
        if length and offset > len(out):
            raise ValueError('invalid RefPack back reference')
        for _ in range(length):
            out.append(out[-offset])
        if c >= 252:
            break
    if len(out) != size:
        raise ValueError('RefPack size mismatch')
    return bytes(out)


def assets(data, *, include_resource=True):
    """Yield arenas; optionally omit expanded resource buffers for header scans.

    With include_resource=False, the second block is None. Its packed range
    and compression tag are still validated, but its contents are not decoded.
    """
    if data[:8] != b'SFIL\0\0\0\3':
        raise ValueError('expected version 3 SFIL')
    pos = 128
    while pos < len(data):
        guid, size, header, stride = struct.unpack_from('>QIII', data, pos)
        if header != 128 or stride < 208 or pos + stride > len(data) or size > stride - header:
            raise ValueError(f'invalid SFIL asset at {pos:x}')
        cursor = pos + 208
        blocks = []
        for i in range(2):
            expanded, packed, compression = struct.unpack_from('>III', data, pos + 128 + i * 32)
            if cursor + packed > pos + header + size or compression not in (0, 1):
                raise ValueError('invalid SFIL block')
            block = data[cursor:cursor + packed]
            cursor += packed
            if i == 1 and not include_resource:
                blocks.append(None)
                continue
            block = unpack(block) if compression else block
            if len(block) != expanded:
                raise ValueError('SFIL expanded size mismatch')
            blocks.append(block)
        if cursor != pos + header + size:
            raise ValueError('SFIL payload size mismatch')
        yield guid, pos, blocks
        pos += stride


def dictionary(data):
    if data[:8] != b'\x89RW4xb2\0':
        raise ValueError('expected RW4 arena')
    count = struct.unpack_from('>I', data, 32)[0]
    offset = struct.unpack_from('>I', data, 48)[0]
    if count > len(data) // 24 or offset + count * 24 > len(data):
        raise ValueError('invalid RW4 dictionary')
    for i in range(count):
        pointer, relocation, size, alignment, index, kind = struct.unpack_from('>6I', data, offset + i * 24)
        yield dict(index=i, pointer=pointer, relocation=relocation, size=size, kind=kind)


def materials(data):
    for entry in dictionary(data):
        if entry['kind'] != 0xeb0005:
            continue
        start, size = entry['pointer'], entry['size']
        block = data[start:start + size]
        count, channels, records, _, _ = struct.unpack_from('>5I', block)
        def string(offset):
            if offset >= len(block):
                raise ValueError('material string out of bounds')
            end = block.find(b'\0', offset)
            if end < 0:
                raise ValueError('unterminated material string')
            return block[offset:end].decode('ascii')
        for i in range(count):
            n, _, offset = struct.unpack_from('>3I', block, records + i * 12)
            if n > channels or offset + n * 32 > len(block):
                raise ValueError('material channels out of bounds')
            rows = {}
            for k in range(n):
                w = struct.unpack_from('>8I', block, offset + k * 32)
                rows[string(w[0])] = dict(flags=w[1], value=f'{w[4]:08X}{w[5]:08X}',
                                         text=string(w[6]) if w[6] else '',
                                         scalar=struct.unpack('>f', struct.pack('>I', w[4]))[0])
            yield i, rows


def resolve_reference(data, reference):
    """Resolve an RW4 dictionary or sub-object reference, before relocation."""
    entries = list(dictionary(data))
    if reference & 0xff800000 == 0x00800000:
        manifest = struct.unpack_from('>I', data, 0x34)[0]
        # Manifest section 3 is the sub-reference table (type 0x10007).
        section = manifest + struct.unpack_from('>I', data, manifest + 20)[0]
        kind, count = struct.unpack_from('>2I', data, section)
        if kind != 0x10007 or (reference & 0x7fffff) >= count:
            raise ValueError('invalid RW4 sub-reference')
        table = struct.unpack_from('>I', data, section + 20)[0]
        index, offset = struct.unpack_from('>2I', data, table + (reference & 0x7fffff) * 8)
    else:
        index, offset = reference, 0
    entry = entries[index]
    if offset >= entry['size']:
        raise ValueError('RW4 reference exceeds object')
    return entry, offset
