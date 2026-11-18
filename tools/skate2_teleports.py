#!/usr/bin/env python3
"""In-game teleport markers for New San Van (Skate 3 'teleports' challenges).

Skate 3 ships the machinery in the base game (db.big): challenge group
'teleports' (challengebanks/main: ChallengeType 45, Global 2, state graph
challenge_stategraph\\teleport) and its local data in
challenge_local_data_framework (TeleporterTrigger, TeleportExitLocation).
Danny Way's Hawaiian Dream DLC adds teleports as children of a copy of that
group: per teleport a 'challenges' record (Name, global and local refs), a
'challenge_global_data' record (Location = start marker locator,
RequiredChallengeHull = mission hull holding the trigger volume, Title,
World) and a 'challenge_local_data' record (TeleporterTrigger = trigger
volume instance id, TeleportExitLocation = world + exit locator name).

Nothing here depends on the DLC. The markers reuse Skate 2 data the goals
package already builds from:

* Trigger volumes: a Skate 2 online-only mission hull with exactly one
  trigger volume (eb0019 + its box shape: 80001 kind 4 box, 80006 box mesh),
  copied under an nsv_ hull name with the volume moved to a small box at the
  start point. The asset is re-encoded as a literal-only RefPack stream (a
  valid stream for the game's own decoder) in the same SFIL v3 layout.
* Locators: unused Skate 2 online locator groups in the Waterfront arena the
  goals package already ships, moved to the start and exit points.

Layouts (verified against the source files here):
  eb0019 volume (0xF0 bytes from object +0x10): +0x10 4x4 matrix (identity),
  +0x50 AABB min, +0x60 AABB max, +0x80 four footprint points (x, ymin, z, 1),
  +0xC8 u64 volume instance id, +0xEC point count.
  80001 kind 4 (box shape): +0 4x4 matrix rows (rotation, translation row 3),
  +0x40 kind, +0x44 half extents.
  80006 clustered mesh: bounds at +0/+0x10, vertices per cluster in mode 1
  (int origin + u16 * granularity) or mode 2 (i32 * granularity).
  SFIL v3: file header 128 bytes; asset header 208 bytes (block 0 descriptor
  at +128: expanded, packed, compression); size = 80 + packed bytes; stride =
  208 + packed rounded up to 128.
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from inspect_sfil import assets, dictionary, unpack  # noqa: E402

FE_LOCATIONS_CLASS = 0xBC69C54E6E102D88   # class key in tWorldLocationID (retail templates)


# ---- RefPack / SFIL ---------------------------------------------------------------------------
def refpack_literal(data: bytes) -> bytes:
    """A RefPack stream made of literal runs only (valid for any RefPack decoder)."""
    if len(data) >= 1 << 24:
        raise ValueError('RefPack literal stream limited to 16 MB')
    out = bytearray(b'\x10\xfb' + len(data).to_bytes(3, 'big'))
    pos = 0
    while len(data) - pos >= 4:
        run = min(112, ((len(data) - pos) // 4) * 4)
        out.append(0xE0 | ((run - 4) >> 2))
        out += data[pos:pos + run]
        pos += run
    rest = len(data) - pos
    out.append(0xFC | rest)
    out += data[pos:]
    if unpack(bytes(out)) != data:
        raise ValueError('RefPack literal stream does not round-trip')
    return bytes(out)


def rebuild_sfil(xsf: bytes, arena: bytes) -> bytes:
    """Single-asset SFIL v3 with its arena replaced (compressed, resource block kept empty)."""
    if xsf[:8] != b'SFIL\0\0\0\3':
        raise ValueError('expected version 3 SFIL')
    pos = 128
    guid, size, header, stride = struct.unpack_from('>QIII', xsf, pos)
    if header != 128 or pos + stride != len(xsf):
        raise ValueError('expected one SFIL asset')
    if struct.unpack_from('>III', xsf, pos + 160) != (0, 0, 0):
        raise ValueError('resource block not empty')
    packed = refpack_literal(arena)
    head = bytearray(xsf[:pos + 208])
    struct.pack_into('>III', head, pos + 128, len(arena), len(packed), 1)
    new_stride = (208 + len(packed) + 127) & ~127
    struct.pack_into('>QIII', head, pos, guid, 80 + len(packed), 128, new_stride)
    out = bytes(head) + packed
    out += bytes(pos + new_stride - len(out))
    (_, _, (check, _)), = list(assets(out))
    if check != arena:
        raise ValueError('rebuilt SFIL does not read back')
    return out


# ---- trigger hull -----------------------------------------------------------------------------
def _corner_map(values, old_box, lo_new, hi_new):
    """Map the corners of the old (rotated) box shape to the new axis-aligned box.

    old_box = (centre, rotation rows 3x3, half extents) of the hull's kind-4 box shape.
    """
    centre, rows, half = old_box
    out = []
    for v in values:
        local = (np.asarray(v, dtype=float) - centre) @ rows.T / half
        if np.any(np.abs(np.abs(local) - 1) > 0.02):
            raise ValueError('trigger mesh vertex is not a corner of the box shape')
        out.append([hi_new[a] if local[a] > 0 else lo_new[a] for a in range(3)])
    return out


def _patch_mesh(arena: bytearray, at: int, size: int, old_box, lo_new, hi_new):
    data = arena[at:at + size]
    u32 = lambda o: struct.unpack_from('>I', data, o)[0]
    table, count = u32(0x34), u32(0x40)
    gran = struct.unpack_from('>f', data, 0x38)[0]
    struct.pack_into('>3f', arena, at, *lo_new)
    struct.pack_into('>3f', arena, at + 16, *hi_new)
    for index in range(count):
        c = u32(table + index * 4)
        n, byte_count, unit_start, normal_start, csize, nv, nn, mode = struct.unpack_from('>5H3B', data, c)
        vs = c + 16
        if mode == 2:
            raw = np.frombuffer(bytes(data), dtype='>i4', count=nv * 3, offset=vs).reshape(-1, 3)
            new = _corner_map(raw * gran, old_box, lo_new, hi_new)
            ints = [int(round(x / gran)) for p in new for x in p]
            struct.pack_into(f'>{nv * 3}i', arena, at + vs, *ints)
        elif mode == 1:
            origin = np.array(struct.unpack_from('>3i', data, vs), dtype=np.int64)
            raw = np.frombuffer(bytes(data), dtype='>u2', count=nv * 3, offset=vs + 12).reshape(-1, 3)
            new = _corner_map((raw.astype(np.int64) + origin) * gran, old_box, lo_new, hi_new)
            q = np.array([[int(round(x / gran)) for x in p] for p in new], dtype=np.int64)
            org = q.min(axis=0)
            off = q - org
            if off.max() > 0xFFFF:
                raise ValueError('trigger box too large for 16-bit mesh vertices')
            struct.pack_into('>3i', arena, at + vs, *org.tolist())
            struct.pack_into(f'>{nv * 3}H', arena, at + vs + 12, *off.ravel().tolist())
        else:
            raise ValueError(f'unsupported trigger mesh vertex mode {mode}')


def move_trigger_hull(xsf: bytes, lo_new, hi_new):
    """(new xsf bytes, volume id): the hull's single trigger volume moved to the box [lo, hi]."""
    (_, _, (arena, _)), = list(assets(xsf))
    arena = bytearray(arena)
    objs = list(dictionary(bytes(arena)))
    vols = [e for e in objs if e['kind'] == 0xEB0019]
    if len(vols) != 1 or struct.unpack_from('>I', arena, vols[0]['pointer'] + 8)[0] != 1:
        raise ValueError('expected exactly one trigger volume')
    v = vols[0]['pointer'] + 0x10
    lo_old = struct.unpack_from('>3f', arena, v + 0x50)
    hi_old = struct.unpack_from('>3f', arena, v + 0x60)
    if struct.unpack_from('>16f', arena, v + 0x10) != (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1):
        raise ValueError('trigger volume matrix is not identity')
    if struct.unpack_from('>I', arena, v + 0xEC)[0] != 4:
        raise ValueError('expected a 4-point trigger footprint')
    volume_id = struct.unpack_from('>Q', arena, v + 0xC8)[0]
    struct.pack_into('>4f', arena, v + 0x50, *lo_new, 0.0)
    struct.pack_into('>4f', arena, v + 0x60, *hi_new, 0.0)
    for i in range(4):
        x, y, z, w = struct.unpack_from('>4f', arena, v + 0x80 + 16 * i)
        nx = hi_new[0] if abs(x - hi_old[0]) < abs(x - lo_old[0]) else lo_new[0]
        nz = hi_new[2] if abs(z - hi_old[2]) < abs(z - lo_old[2]) else lo_new[2]
        struct.pack_into('>4f', arena, v + 0x80 + 16 * i, nx, lo_new[1], nz, w)
    centre = [(a + b) / 2 for a, b in zip(lo_new, hi_new)]
    half = [(b - a) / 2 for a, b in zip(lo_new, hi_new)]
    box_shapes = [e for e in objs if e['kind'] == 0x80001 and
                  struct.unpack_from('>I', arena, e['pointer'] + 0x40)[0] == 4]
    meshes = [e for e in objs if e['kind'] == 0x80006]
    if len(box_shapes) != 1 or len(meshes) != 1:
        raise ValueError(f'expected one box shape and one mesh, got {len(box_shapes)} and {len(meshes)}')
    b0 = box_shapes[0]['pointer']
    m = np.array(struct.unpack_from('>16f', arena, b0)).reshape(4, 4)
    old_box = (m[3, :3], m[:3, :3], np.array(struct.unpack_from('>3f', arena, b0 + 0x44)))
    _patch_mesh(arena, meshes[0]['pointer'], meshes[0]['size'], old_box, lo_new, hi_new)
    struct.pack_into('>16f', arena, b0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, *centre, 0)
    struct.pack_into('>3f', arena, b0 + 0x44, *half)
    return rebuild_sfil(xsf, bytes(arena)), volume_id


# ---- locators ---------------------------------------------------------------------------------
def facing_matrix(position, facing):
    """Skate locator transform rows: right, up, facing, position (facing in the XZ plane)."""
    fx, fz = facing
    n = math.hypot(fx, fz)
    fx, fz = fx / n, fz / n
    return [fz, 0.0, -fx, 0.0, 0.0, 1.0, 0.0, 0.0, fx, 0.0, fz, 0.0, *position, 1.0]


def move_locator_groups(rx2: bytes, moves: dict) -> bytes:
    """Arena bytes with the named LOCATIONDESCDATA groups' transforms replaced.

    Each moved group must have '<name>::' child records, and they all move onto the group's
    new transform. Skate 3's own teleport start (teleport_dwtn_01_toDwtnskpk_locator_01) has one
    (::spawn_01), as every working goal start does; the childless groups used up to round 272
    never got their teleport streamed in (log 349).
    """
    out = bytearray(rx2)
    found = set()
    for e in dictionary(rx2):
        if e['kind'] != 0xEB0009:
            continue
        o = e['pointer']
        groups, records = struct.unpack_from('>II', rx2, o)
        g0 = o + 0x20
        r0 = g0 + groups * 0x80

        def name_at(at):
            off = struct.unpack_from('>I', rx2, at)[0]
            return rx2[o + off:rx2.index(b'\0', o + off)].decode('latin1')
        children = {}
        for i in range(records):
            r = r0 + i * 0x70
            children.setdefault(name_at(r + 0x60).split('::')[0], []).append(r)
        for i in range(groups):
            g = g0 + i * 0x80
            name = name_at(g + 0x68)
            if name in moves:
                if not children.get(name):
                    raise ValueError(f'locator group {name} has no child records')
                for at in [g] + children[name]:
                    struct.pack_into('>16f', out, at, *moves[name])
                found.add(name)
    missing = set(moves) - found
    if missing:
        raise KeyError(f'locator groups not found: {sorted(missing)}')
    return bytes(out)
