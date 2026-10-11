"""Read original RW clustered-mesh triangles without modifying the arena.

Offsets are relative to the EB/80006 object, before the game's pointer fixup.
The cluster unit stream is byte-oriented even in a big-endian RW arena.
This reader deliberately rejects unsupported unit kinds and compressed normals.
"""
from __future__ import annotations

import struct
import numpy as np


def clustered_mesh(data: bytes, with_tags=False):
    """Return float64 triangle vertices and the authored mesh bounding box.

    Validation checks both cluster byte counts and the mesh's total unit count.
    The caller must apply the owning collision volume's instance transform.
    """
    if len(data) < 0x60:
        raise ValueError('truncated clustered mesh')
    u32 = lambda at: struct.unpack_from('>I', data, at)[0]
    bounds = np.array([struct.unpack_from('>3f', data, p) for p in (0, 16)])
    units, table, count = u32(0x28), u32(0x34), u32(0x40)
    granularity = struct.unpack_from('>f', data, 0x38)[0]
    group_bytes, surface_bytes = data[0x3e:0x40]
    if (not np.isfinite(bounds).all() or np.any(bounds[0] > bounds[1]) or
            not 0 < granularity < 1 or group_bytes > 2 or surface_bytes > 2 or
            count > (len(data) - table) // 4 or table < 0x60 or u32(0x48) != units):
        raise ValueError('unsupported clustered mesh header')
    triangles, tags, seen_units = [], [], 0
    for index in range(count):
        at = u32(table + index * 4)
        if at < table + count * 4 or at + 16 > len(data):
            raise ValueError('cluster pointer outside mesh')
        n, byte_count, unit_start, normal_start, size, nv, nn, mode = struct.unpack_from(
            '>5H3B', data, at)
        start = at + 16 + unit_start * 16
        if at + size > len(data) or start + byte_count != at + size or nn:
            raise ValueError('unsupported cluster size or normals')
        vertex_start = at + 16
        if mode == 0:
            vertex_end = vertex_start + nv * 16
            vertices = np.frombuffer(data, dtype='>f4', count=nv * 4,
                                     offset=vertex_start).reshape(-1, 4)[:, :3].astype(float)
        elif mode == 1:
            origin = np.array(struct.unpack_from('>3i', data, vertex_start), dtype=np.int64)
            vertex_start += 12
            vertex_end = vertex_start + nv * 6
            vertices = (np.frombuffer(data, dtype='>u2', count=nv * 3,
                                      offset=vertex_start).reshape(-1, 3).astype(np.int64) + origin) * granularity
        elif mode == 2:
            vertex_end = vertex_start + nv * 12
            vertices = np.frombuffer(data, dtype='>i4', count=nv * 3,
                                     offset=vertex_start).reshape(-1, 3).astype(float) * granularity
        else:
            raise ValueError(f'unsupported vertex compression {mode}')
        if vertex_end > start or normal_start != unit_start or not np.isfinite(vertices).all():
            raise ValueError('invalid cluster vertices')
        if nv and (np.any(vertices < bounds[0] - 2 * granularity) or
                   np.any(vertices > bounds[1] + 2 * granularity)):
            raise ValueError('decoded vertices outside authored mesh bounds')
        cursor = start
        for _ in range(n):
            if cursor >= start + byte_count:
                raise ValueError('truncated cluster unit stream')
            flags = data[cursor]
            corners = {1: 3, 2: 4}.get(flags & 31)
            if corners is None:
                raise ValueError(f'unsupported collision unit {flags:#x}')
            length = (1 + corners + (corners if flags & 32 else 0) +
                      (group_bytes if flags & 64 else 0) +
                      (surface_bytes if flags & 128 else 0))
            if cursor + length > start + byte_count:
                raise ValueError('truncated collision unit')
            ids = list(data[cursor + 1:cursor + 1 + corners])
            if max(ids) >= nv:
                raise ValueError('collision vertex index outside cluster')
            if corners != 3:
                # Quad diagonal/winding requires its own runtime evidence.
                raise ValueError('quad collision units not yet decoded')
            triangles.append(vertices[ids])
            tag_at = cursor + 1 + corners + (corners if flags & 32 else 0)
            group = int.from_bytes(data[tag_at:tag_at+group_bytes], 'little') if flags & 64 else 0
            tag_at += group_bytes if flags & 64 else 0
            surface = int.from_bytes(data[tag_at:tag_at+surface_bytes], 'little') if flags & 128 else 0
            tags.append((group, surface))
            cursor += length
        if cursor != start + byte_count:
            raise ValueError('unconsumed collision unit bytes')
        seen_units += n
    if seen_units != units:
        raise ValueError('cluster unit total differs from mesh header')
    result = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3), bounds
    return (*result, np.asarray(tags, dtype=np.uint16).reshape(-1, 2)) if with_tags else result


def static_triangles(arena: bytes, objects, with_tags=False):
    """Yield collision from authored static instances, including their transforms.

    EB000D instance +0x84 references EB000A, whose root is an 80001 volume.
    Do not treat uninstanced prototypes, DMO bounds or render meshes as floors.
    """
    cache = {}
    for obj in objects:
        if obj['kind'] != 0xeb000d:
            continue
        base, size = obj['pointer'], obj['size']
        count, flags, start = struct.unpack_from('>3I', arena, base + 4)
        if start < 32 or start + count * 160 > size:
            raise ValueError('invalid collision instance table')
        for i in range(count):
            at = base + start + i * 160
            ref = struct.unpack_from('>I', arena, at + 132)[0]
            if ref >= len(objects) or objects[ref]['kind'] != 0xeb000a:
                continue
            descriptor = objects[ref]
            d = descriptor['pointer']
            _, roots, table = struct.unpack_from('>3I', arena, d)
            if roots != 1 or table != 12 or descriptor['size'] != 20:
                raise ValueError('unsupported collision root descriptor')
            root = struct.unpack_from('>I', arena, d + table)[0]
            if root >= len(objects) or objects[root]['kind'] != 0x80001:
                raise ValueError('collision root is not a volume')
            volume = objects[root]['pointer']
            kind, mesh_ref = struct.unpack_from('>II', arena, volume + 64)
            if kind != 6 or mesh_ref >= len(objects) or objects[mesh_ref]['kind'] != 0x80006:
                raise ValueError('unsupported static collision volume')
            if mesh_ref not in cache:
                mesh = objects[mesh_ref]
                cache[mesh_ref] = clustered_mesh(arena[mesh['pointer']:mesh['pointer'] + mesh['size']], True)
            triangles, _, tags = cache[mesh_ref]
            local = np.array(struct.unpack_from('>16f', arena, volume)).reshape(4, 4)
            world = np.array(struct.unpack_from('>16f', arena, at)).reshape(4, 4)
            # Collision volumes store an affine 3x4 in padded rows; w is unused.
            local[:, 3] = (0, 0, 0, 1)
            transform = local @ world
            points = triangles @ transform[:3, :3] + transform[3, :3]
            if np.linalg.det(transform[:3, :3]) < 0:
                points = points[:, (0, 2, 1), :]
            bounds = np.array([struct.unpack_from('>3f', arena, at + p) for p in (64, 80)])
            if (not np.isfinite(points).all() or np.any(points < bounds[0] - .01) or
                    np.any(points > bounds[1] + .01)):
                raise ValueError('transformed collision lies outside instance bounds')
            yield (points, tags) if with_tags else points
