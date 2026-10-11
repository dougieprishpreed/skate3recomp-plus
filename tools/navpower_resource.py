"""Strict reader for the version-23 NavPower resources shipped with Skate 3.

Evidence: 8293B620 (container/checksum), 82937450 (version/layers),
82938158 and 82938878 (polygons/links), 829419F0 (binary spatial tree).
No runtime pointers are accepted. This is an offline format reader, not a bake.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Polygon:
    offset: int
    vertices: tuple
    neighbors: tuple
    flags: tuple


@dataclass(frozen=True)
class Layer:
    index: int
    bounds: tuple
    agent_settings: tuple
    polygons: tuple[Polygon, ...]
    tree_bytes: int


def read_rw_object(data: bytes) -> tuple[tuple[Layer, ...], ...]:
    """Read resource pointers from the EB0027 wrapper, past optional link data."""
    if len(data) < 48:
        raise ValueError('truncated NavPower RW object')
    count, table = struct.unpack_from('>II', data, 4)
    if table < 48 or count > (len(data) - table) // 4:
        raise ValueError('invalid NavPower resource pointer table')
    result = []
    for i in range(count):
        offset = struct.unpack_from('>I', data, table + i * 4)[0]
        if offset < table + count * 4 or offset + 24 > len(data):
            raise ValueError('invalid NavPower resource pointer')
        size = struct.unpack_from('>I', data, offset + 8)[0] + 24
        if offset + size > len(data):
            raise ValueError('NavPower resource extends outside RW object')
        result.append(read_resource(data[offset:offset + size]))
    return tuple(result)


def read_resource(data: bytes) -> tuple[Layer, ...]:
    def words(at, count=1):
        if at < 0 or at + count * 4 > len(data):
            raise ValueError('NavPower word range outside resource')
        return struct.unpack_from('>' + str(count) + 'I', data, at)

    def floats(at, count):
        result = struct.unpack('>' + str(count) + 'f', struct.pack('>' + str(count) + 'I', *words(at, count)))
        if not all(math.isfinite(v) for v in result):
            raise ValueError('non-finite NavPower geometry')
        return result

    endian, version, size, checksum, flags, reserved = words(0, 6)
    if endian != 0xffffffff or version != 2 or flags or reserved or size + 24 != len(data) or size % 4:
        raise ValueError('unsupported NavPower container')
    if sum(words(24, size // 4)) & 0xffffffff != checksum:
        raise ValueError('NavPower checksum mismatch')
    if not size:
        return ()
    kind, chunk_size, child_count = words(24, 3)
    if kind != 0x10000 or chunk_size != size - 12 or child_count:
        raise ValueError('unsupported NavPower chunk')
    endian, version, count = words(36, 3)
    if endian != 0xffffffff or version != 23 or not 0 < count <= 32:
        raise ValueError('unsupported NavPower mesh version/layer count')
    layers, base = [], 48
    for _ in range(count):
        mesh_version, index, polygon_bytes, layer_size = words(base, 4)
        if mesh_version != 23 or index >= 32 or layer_size < 0x138 + polygon_bytes + 28 or base + layer_size > len(data):
            raise ValueError('invalid NavPower layer')
        settings = floats(base + 0x10, 4)
        bounds = floats(base + 0x20, 6)
        if any(bounds[i] > bounds[i + 3] for i in range(3)):
            raise ValueError('invalid NavPower layer bounds')
        cursor, end = 0x138, 0x138 + polygon_bytes
        polygons = []
        while cursor < end:
            if cursor + 52 > end:
                raise ValueError('truncated NavPower polygon')
            polyflags = words(base + cursor + 40, 3)
            corners = polyflags[0] & 127
            if corners < 3 or cursor + 52 + corners * 24 > end:
                raise ValueError('invalid NavPower polygon vertex count')
            vertices, neighbors = [], []
            for j in range(corners):
                edge = base + cursor + 52 + j * 24
                neighbors.append(words(edge)[0])
                vertices.append(floats(edge + 4, 3))
            polygons.append(Polygon(cursor, tuple(vertices), tuple(neighbors), polyflags))
            cursor += 52 + corners * 24
        offsets = {p.offset for p in polygons}
        if any(n and n not in offsets for p in polygons for n in p.neighbors):
            raise ValueError('NavPower polygon neighbor outside layer')
        tree = base + end
        tree_bounds = floats(tree, 6)
        tree_size = words(tree + 24)[0]
        if tree + 28 + tree_size != base + layer_size or tree_bounds != bounds:
            raise ValueError('NavPower tree size/bounds mismatch')
        start, stop = tree + 28, tree + 28 + tree_size
        leaves, nodes = set(), set()

        def visit(at, depth=0):
            if at in nodes or depth > 64 or not start <= at < stop:
                raise ValueError('invalid NavPower spatial tree topology')
            nodes.add(at)
            tag = words(at)[0]
            if tag & 0x80000000:
                poly = tag & 0x7fffffff
                if poly not in offsets or poly in leaves:
                    raise ValueError('invalid or repeated NavPower tree leaf')
                leaves.add(poly)
                return at + 4
            axis, right = tag >> 28, at + (tag & 0x0fffffff)
            if axis > 2 or right < at + 16 or right >= stop:
                raise ValueError('invalid NavPower tree split')
            floats(at + 4, 2)
            if visit(at + 12, depth + 1) != right:
                raise ValueError('NavPower left subtree extent mismatch')
            return visit(right, depth + 1)

        if visit(start) != stop or leaves != offsets:
            raise ValueError('NavPower spatial tree does not cover polygons')
        layers.append(Layer(index, bounds, settings, tuple(polygons), tree_size))
        base += layer_size
    if base != len(data):
        raise ValueError('unconsumed NavPower resource bytes')
    return tuple(layers)


def write_resource(vertices, polygons, neighbors) -> bytes:
    """Serialize convex, upward-wound polygons for the native v23 loader.

    The runtime constructor 8292F358 initializes polygon flags; 82938878
    relocates neighbor offsets. The unused 256-byte authoring header is zero.
    Tile-to-tile boundary connections belong to the native loader (829367C8),
    never persistent guest pointers. Input neighbor -1 denotes an open edge.
    """
    import numpy as np
    vertices = np.asarray(vertices, dtype=np.float32)
    if vertices.ndim != 2 or vertices.shape[1] != 3 or not np.isfinite(vertices).all():
        raise ValueError('invalid navigation vertices')
    if not polygons or len(polygons) != len(neighbors):
        raise ValueError('missing polygons or adjacency')
    offsets, points, cursor = [], [], 0x138
    for ids, links in zip(polygons, neighbors):
        if not 3 <= len(ids) <= 127 or len(ids) != len(links):
            raise ValueError('invalid polygon edge count')
        if any(i < 0 or i >= len(vertices) for i in ids):
            raise ValueError('polygon vertex outside table')
        v = vertices[list(ids)].astype(np.float64)
        # NavPower uses upward winding. Every convex edge must agree.
        turns = np.cross(np.roll(v, -1, axis=0)-v, np.roll(v, -2, axis=0)-np.roll(v, -1, axis=0))[:, 1]
        if np.min(turns) < -1e-5 or np.max(turns) <= 0:
            raise ValueError('non-convex or downward-wound polygon')
        if any(n < -1 or n >= len(polygons) for n in links):
            raise ValueError('polygon neighbor outside table')
        offsets.append(cursor)
        points.append(v)
        cursor += 52 + len(ids)*24
    # Check reciprocal edge geometry, rather than accepting matching indices.
    for i, (ids, links) in enumerate(zip(polygons, neighbors)):
        for j, n in enumerate(links):
            if n < 0:
                continue
            a, b = ids[j], ids[(j+1) % len(ids)]
            if not any(polygons[n][k] == b and polygons[n][(k+1) % len(polygons[n])] == a
                       and neighbors[n][k] == i for k in range(len(polygons[n]))):
                raise ValueError('non-reciprocal navigation edge')
    lo = np.array([v.min(axis=0) for v in points])
    hi = np.array([v.max(axis=0) for v in points])
    bounds = (*lo.min(axis=0), *hi.max(axis=0))
    layer = bytearray(cursor)
    struct.pack_into('>3I4x4f6f', layer, 0, 23, 0, cursor-0x138,
                     .12, .35, .20, 1.6, *bounds)
    for i, (v, links) in enumerate(zip(points, neighbors)):
        at, n = offsets[i], len(v)
        center = v.mean(axis=0)
        radius = float(np.linalg.norm(v-center, axis=1).max())
        # Center/radius bounds are conservative after conversion to float32.
        radius = float(np.nextafter(np.float32(radius + 1e-4), np.float32(np.inf)))
        struct.pack_into('>5f4I', layer, at+16, *center, radius,
                         3.4028234663852886e38, 0, 0x7fff80 | n, 0x1100, (n-1)<<24)
        for j, link in enumerate(links):
            struct.pack_into('>I3f2I', layer, at+52+j*24,
                             offsets[link] if link >= 0 else 0, *v[j], 0xffff0000, 0)

    def tree(ids):
        if len(ids) == 1:
            return struct.pack('>I', 0x80000000 | offsets[ids[0]])
        axis = int(np.argmax(hi[ids].max(axis=0)-lo[ids].min(axis=0)))
        ids = sorted(ids, key=lambda i: (float(lo[i, axis]+hi[i, axis]), i))
        mid = len(ids)//2
        left, right = ids[:mid], ids[mid:]
        a, b = tree(left), tree(right)
        return struct.pack('>I2f', (axis<<28) | (12+len(a)),
                           hi[left, axis].max(), lo[right, axis].min()) + a + b

    nodes = tree(list(range(len(polygons))))
    layer.extend(struct.pack('>6fI', *bounds, len(nodes)))
    layer.extend(nodes)
    struct.pack_into('>I', layer, 12, len(layer))
    mesh = struct.pack('>3I', 0xffffffff, 23, 1) + layer
    payload = struct.pack('>3I', 0x10000, len(mesh), 0) + mesh
    checksum = sum(struct.unpack('>'+str(len(payload)//4)+'I', payload)) & 0xffffffff
    result = struct.pack('>6I', 0xffffffff, 2, len(payload), checksum, 0, 0) + payload
    read_resource(result)
    return result


def wrap_resource(resource: bytes) -> bytes:
    """One resource, no authored off-mesh links; offsets fixed by 82965500."""
    read_resource(resource)
    wrapper = bytearray(64)
    struct.pack_into('>5I', wrapper, 0, 48, 1, 48, 0, 64)
    struct.pack_into('>I', wrapper, 48, 64)
    return bytes(wrapper) + resource
