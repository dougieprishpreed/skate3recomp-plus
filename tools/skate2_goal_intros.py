#!/usr/bin/env python3
"""Skate 2 Own the Spot intro cutscenes (NIS) for the New San Van goals package.

Skate 2 and Skate 3 share the cutscene system (ICE). Evidence (HANDOFF round 266):

* data/scene/scene_NNN.bin is the scene table: 'scen', version 1, scene count,
  asset count, then 0x68-byte scene records (u32 id, u8 type, floats, u16 asset
  count, u16 first asset, 64-byte name) and 0xB4-byte asset records.
* data/scene/ice_NNN.bin is the ICE database (little endian): a root
  (0x80000000, size) holding one block per scene type (0x80000000 | type,
  size), each a run of self-contained scene objects (1, size, id, ...). Block
  0x80000002 is shared data; it is byte-identical in both games.
* Skate 3 loads only the highest-numbered scene_* and ice_* file (sub_82823898
  scans data\\scene\\scene_* and keeps the largest number). Every retail DLC
  therefore ships a complete superset (Maloof 002, Danny Way 003, Creator 005,
  Play 006).
* A Skate 3 Own the Spot names its intro in its local data:
  IntroPresentationEvents = [{5, '<scene name>', ''}]. Skate 2 goals carry the
  same event with the Skate 2 scene name (ownspot_f_1a_..._intro).
* Cutscene audio references are (0x80, big-endian line id) pairs. Speech adds
  the speaker (Reda = 40), post-produced effects use speaker 99. DLC audio is
  registered with an aud_bigfiles record (folder, resident archive of .snr
  headers, slot); the .sns streams lie loose in that folder (Maloof slot 5,
  Danny Way 4 and 6).
* Subtitles: sub_82481FE8 builds SPEECH_SCRIPTED_ + upper-case file name
  without extension and looks that label up.

This module merges selected Skate 2 scenes into a copy of Skate 3's newest
scene table and ICE database, without changing any existing record or object
(the result is the original bytes plus appended data), and writes resident
archives in the retail layout. Nothing here reads or writes game folders.
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_nsv_dlc as dlc                      # noqa: E402  path_hash, align conventions
from inspect_sfil import unpack                  # noqa: E402  RefPack

SCENE_MAGIC = b'scen'
SCENE_RECORD = 0x68
SCENE_ASSET = 0xB4
SCENE_NAME_AT, SCENE_NAME_SIZE = 0x28, 0x40
SPOT_INTRO, SPOT_OUTRO = 12, 13                  # scene types (string table at 0x821987B8)
ROOT = 0x80000000
SHARED_BLOCK = 0x80000002
SPOT_BLOCK = 0x8000000C
SNR_SIZE = 8


def align(n, a):
    return (n + a - 1) & ~(a - 1)


# ---- scene table --------------------------------------------------------------------------
def read_scene_table(data: bytes):
    """([scene record bytes], [asset record bytes]) of a scene_NNN.bin, validated."""
    magic, version, scenes, assets = struct.unpack_from('>4sIII', data, 0)
    if magic != SCENE_MAGIC or version != 1:
        raise ValueError('not a scene table')
    end = 0x10 + scenes * SCENE_RECORD
    if end + assets * SCENE_ASSET != len(data):
        raise ValueError(f'scene table size {len(data)} != {end}+{assets}*{SCENE_ASSET}')
    records = [data[0x10 + i * SCENE_RECORD:0x10 + (i + 1) * SCENE_RECORD] for i in range(scenes)]
    asset_records = [data[end + i * SCENE_ASSET:end + (i + 1) * SCENE_ASSET] for i in range(assets)]
    for r in records:
        count, first = struct.unpack_from('>HH', r, 0x24)
        if count and first + count > assets:
            raise ValueError(f'{scene_name(r)}: asset range outside the table')
    return records, asset_records


def write_scene_table(records, asset_records) -> bytes:
    return (struct.pack('>4sIII', SCENE_MAGIC, 1, len(records), len(asset_records)) +
            b''.join(records) + b''.join(asset_records))


def scene_name(record: bytes) -> str:
    return record[SCENE_NAME_AT:SCENE_NAME_AT + SCENE_NAME_SIZE].split(b'\0', 1)[0].decode('ascii')


def scene_id(record: bytes) -> int:
    return struct.unpack_from('>I', record, 0)[0]


def scene_type(record: bytes) -> int:
    return record[4]


def asset_strings(asset: bytes):
    return [m.decode('ascii') for m in re.findall(rb'[\x20-\x7e]{3,}', asset)]


# ---- ICE database ----------------------------------------------------------------------------
def read_ice(data: bytes):
    """[[block type, [object bytes incl. its 8-byte header]]], validated."""
    kind, size = struct.unpack_from('<II', data, 0)
    if kind != ROOT or size + 8 != len(data):
        raise ValueError('not an ICE database')
    blocks, at = [], 8
    while at < len(data):
        kind, size = struct.unpack_from('<II', data, at)
        if not kind & ROOT or at + 8 + size > len(data):
            raise ValueError(f'bad ICE block at {at:#x}')
        payload, objects, o = data[at + 8:at + 8 + size], [], 0
        while o < len(payload):
            one, osize = struct.unpack_from('<II', payload, o)
            if one != 1 or o + 8 + osize > len(payload) or osize < 4:
                raise ValueError(f'bad ICE object in block {kind:#x} at {o:#x}')
            objects.append(payload[o:o + 8 + osize])
            o += 8 + osize
        blocks.append([kind, objects])
        at += 8 + size
    return blocks


def write_ice(blocks) -> bytes:
    body = b''.join(struct.pack('<II', kind, sum(map(len, objects))) + b''.join(objects)
                    for kind, objects in blocks)
    return struct.pack('<II', ROOT, len(body)) + body


def object_id(obj: bytes) -> int:
    return struct.unpack_from('<I', obj, 8)[0]


def file_stamp(blocks) -> int:
    """The export stamp every object of one ICE file carries at +20 (base and Skate 2: 0x12FBEC,
    Play DLC 006: 0x18FBB4). A DLC rebuild re-stamps every object, nothing else changes."""
    stamps = {struct.unpack_from('<I', o, 20)[0] for _, objs in blocks for o in objs}
    if len(stamps) != 1:
        raise ValueError(f'ICE objects carry {len(stamps)} export stamps')
    return stamps.pop()


def restamp(obj: bytes, old: int, new: int) -> bytes:
    """Object with its header stamps (+16 when equal to the stamp, +20) moved to another file's stamp."""
    out = bytearray(obj)
    if struct.unpack_from('<I', out, 16)[0] == old:
        struct.pack_into('<I', out, 16, new)
    struct.pack_into('<I', out, 20, new)
    return bytes(out)


def audio_lines(obj: bytes):
    """[(line, speaker byte or None)] the object references: a 0x80 marker word, the big-endian
    line id, then for speech the speaker in the next word's first byte (Reda: 28 00 00 00).
    Post-produced effects (speaker 99 files) carry no speaker word."""
    out = []
    for m in re.finditer(rb'\x80\x00\x00\x00(?=\x00\x00..)', obj, re.S):
        if m.start() % 4 == 0:
            line = struct.unpack_from('>I', obj, m.end())[0]
            if 1000 <= line <= 0xFFFF:
                nxt = obj[m.end() + 4:m.end() + 8]
                speaker = nxt[0] if len(nxt) == 4 and 0 < nxt[0] < 0x80 and nxt[1:] == b'\0\0\0' else None
                out.append((line, speaker))
    return out


# ---- retail compression variant --------------------------------------------------------------
def decode_payload(raw: bytes) -> bytes:
    """Raw, RefPack or either 'chunkref' layout (world_archive reads only the chunk-table one)."""
    if raw[:8] != b'chunkref':
        return unpack(raw) if raw[:2] == b'\x10\xfb' else raw
    count = struct.unpack_from('>I', raw, 20)[0]
    table = [struct.unpack_from('>4I', raw, 32 + 16 * i) for i in range(count)]
    data_at = 32 + 16 * count
    if count > 1 and all(c == 2 for *_, c in table) and \
            data_at + sum(s for _, _, s, _ in table) == len(raw) and raw[data_at:data_at + 2] == b'\x10\xfb':
        from world_archive import decode_archive_payload
        return decode_archive_payload(raw)
    return decode_chunkref(raw)


def decode_chunkref(raw: bytes) -> bytes:
    """Skate 3 scene.big 'chunkref' v2 with per-chunk headers.

    Layout: 'chunkref', version 2, expanded size, block size, chunk count,
    alignment 16, 0; then per chunk (stored size, codec) directly before its data,
    which starts on a 16-byte boundary after the previous chunk. Codec 2 is a
    RefPack stream, codec 4 a stored (uncompressed) block.
    """
    if raw[:8] != b'chunkref':
        return unpack(raw) if raw[:2] == b'\x10\xfb' else raw
    version, expanded, block, count, alignment, zero = struct.unpack_from('>6I', raw, 8)
    if version != 2 or alignment != 16 or zero:
        raise ValueError('unsupported chunkref header')
    out, cursor = bytearray(), 32
    for i in range(count):
        data_at = align(cursor + 8, alignment)
        stored, codec = struct.unpack_from('>II', raw, data_at - 8)
        if data_at + stored > len(raw):
            raise ValueError(f'chunk {i}: outside the payload')
        if codec == 2 and raw[data_at:data_at + 2] == b'\x10\xfb':
            out += unpack(raw[data_at:data_at + stored])
        elif codec == 4 and stored <= block:
            out += raw[data_at:data_at + stored]
        else:
            raise ValueError(f'chunk {i}: unsupported codec {codec}')
        cursor = data_at + stored
    if cursor != len(raw) or len(out) != expanded:
        raise ValueError(f'chunkref expanded {len(out)} != {expanded}')
    return bytes(out)


# ---- merge ---------------------------------------------------------------------------------------
def merge(native_scene: bytes, native_ice: bytes, s2_scene: bytes, s2_ice: bytes, names):
    """Native scene table and ICE plus the named Skate 2 scenes, appended.

    Returns (scene bytes, ice bytes, {name: dict(id, type, assets, lines)}).
    Every native record, asset and object keeps its bytes and order.
    """
    records, assets = read_scene_table(native_scene)
    blocks = read_ice(native_ice)
    s2_records, s2_assets = read_scene_table(s2_scene)
    s2_blocks = read_ice(s2_ice)
    stamp, s2_stamp = file_stamp(blocks), file_stamp(s2_blocks)
    shared = [b for b in blocks if b[0] == SHARED_BLOCK]
    s2_shared = [b for b in s2_blocks if b[0] == SHARED_BLOCK]
    if len(shared) != 1 or len(s2_shared) != 1 or \
            [restamp(o, s2_stamp, stamp) for o in s2_shared[0][1]] != [
                restamp(o, stamp, stamp) for o in shared[0][1]]:
        raise ValueError('shared ICE block differs between the games')
    native_names = {scene_name(r) for r in records}
    native_ids = {scene_id(r) for r in records} | {object_id(o) for _, objs in blocks for o in objs}
    s2_by_name = {scene_name(r): r for r in s2_records}
    s2_objects = {}
    for kind, objs in s2_blocks:
        for o in objs:
            s2_objects[object_id(o)] = (kind, o)
    added, report = [], {}
    for name in names:
        if name in report:
            continue
        rec = s2_by_name.get(name)
        if rec is None:
            raise KeyError(f'Skate 2 scene {name} not found')
        sid = scene_id(rec)
        if name in native_names or sid in native_ids:
            raise ValueError(f'{name}: name or id {sid:#010x} already used by Skate 3')
        if sid not in s2_objects:
            raise KeyError(f'{name}: ICE object {sid:#010x} missing')
        kind, obj = s2_objects[sid]
        count, first = struct.unpack_from('>HH', rec, 0x24)
        new = bytearray(rec)
        struct.pack_into('>HH', new, 0x24, count, len(assets))      # retail: next free asset index
        scene_assets = s2_assets[first:first + count] if count else []
        assets += scene_assets
        records.append(bytes(new))
        target = next((b for b in blocks if b[0] == kind), None)
        if target is None:
            raise ValueError(f'{name}: no native ICE block {kind:#x}')
        target[1].append(restamp(obj, s2_stamp, stamp))
        added.append(name)
        report[name] = dict(id=f'{sid:08X}', type=scene_type(rec), block=f'{kind:08X}',
                            object_bytes=len(obj), header_16=f'{struct.unpack_from("<I", obj, 16)[0]:08X}',
                            stamp=f'{s2_stamp:08X}->{stamp:08X}',
                            assets=[asset_strings(a) for a in scene_assets], lines=audio_lines(obj))
    scene, ice = write_scene_table(records, assets), write_ice(blocks)
    verify_superset(native_scene, native_ice, scene, ice, len(added))
    return scene, ice, report


def verify_superset(native_scene, native_ice, scene, ice, added):
    """The merged files hold every native record, asset and object unchanged and in order."""
    r0, a0 = read_scene_table(native_scene)
    r1, a1 = read_scene_table(scene)
    if r1[:len(r0)] != r0 or len(r1) != len(r0) + added or a1[:len(a0)] != a0:
        raise ValueError('merged scene table does not preserve the native records')
    b0, b1 = read_ice(native_ice), read_ice(ice)
    if [k for k, _ in b0] != [k for k, _ in b1]:
        raise ValueError('merged ICE changed the block order')
    extra = 0
    for (k, objs0), (_, objs1) in zip(b0, b1):
        if objs1[:len(objs0)] != objs0:
            raise ValueError(f'merged ICE block {k:#x} changed a native object')
        extra += len(objs1) - len(objs0)
    if extra != added:
        raise ValueError('merged ICE object count mismatch')
    names = [scene_name(r) for r in r1]
    if len(set(names)) != len(names) or len({scene_id(r) for r in r1}) != len(r1):
        raise ValueError('merged scene table has duplicate names or ids')


# ---- resident archives (retail EB3 family 0x00100400) -----------------------------------------
def resident_archive(files) -> bytes:
    """[('./name.snr', 8-byte header)] -> resident .big like retail nisresident/dlc_*_resident.

    Same EB3 table as build_nsv_dlc.eb3_archive, but family 0x00100400: 16-byte
    data alignment (offsets >> 4) and no directory tree.
    """
    count = len(files)
    dir_order = []
    for path, _ in files:
        folder = path.rsplit('/', 1)[0]
        if folder not in dir_order:
            dir_order.append(folder)
    width = max(len(p.rsplit('/', 1)[1]) for p, _ in files) + 3
    dwidth = max(len(d) for d in dir_order) + 1
    names_at = align(48 + 17 * count, 16)
    dirs_at = align(names_at + count * width, 16)
    names_end = align(dirs_at + len(dir_order) * dwidth, 16)
    offsets, cursor = [], names_end
    for _, data in files:
        offsets.append(cursor)
        cursor = align(cursor + len(data), 16)
    out = bytearray(cursor)
    struct.pack_into('>4s8I', out, 0, b'EB\0\3', count, 0x00100400, names_at, names_end - names_at,
                     (width << 24) | (dwidth << 16) | len(dir_order), 0, cursor, 0)
    for slot, i in enumerate(sorted(range(count), key=lambda i: dlc.path_hash(files[i][0]))):
        path, data = files[i]
        struct.pack_into('>4I', out, 48 + 16 * slot, offsets[i] >> 4, 0, len(data), dlc.path_hash(path))
        folder, name = path.rsplit('/', 1)
        entry = struct.pack('>H', dir_order.index(folder)) + name.encode('ascii') + b'\0'
        out[names_at + slot * width:names_at + slot * width + len(entry)] = entry
    for i, folder in enumerate(dir_order):
        entry = folder.encode('ascii') + b'\0'
        out[dirs_at + i * dwidth:dirs_at + i * dwidth + len(entry)] = entry
    for (_, data), offset in zip(files, offsets):
        out[offset:offset + len(data)] = data
    return bytes(out)


def read_resident(data: bytes):
    """{name: bytes} of a resident archive (retail family 0x00100400)."""
    magic, count, family, names_at, names_size, meta, _, total, tree = struct.unpack_from('>4s8I', data, 0)
    if magic != b'EB\0\3' or family != 0x00100400 or total != len(data) or tree:
        raise ValueError('not a resident archive')
    width, dwidth, ndirs = meta >> 24, (meta >> 16) & 255, meta & 0xFFFF
    dirs_at = align(names_at + count * width, 16)
    dirs = [data[dirs_at + i * dwidth:dirs_at + (i + 1) * dwidth].split(b'\0', 1)[0].decode('ascii')
            for i in range(ndirs)]
    out = {}
    for i in range(count):
        block, _, size, hashed = struct.unpack_from('>4I', data, 48 + 16 * i)
        slot = data[names_at + i * width:names_at + (i + 1) * width]
        name = dirs[struct.unpack_from('>H', slot)[0]] + '/' + slot[2:].split(b'\0', 1)[0].decode('ascii')
        if hashed != dlc.path_hash(name):
            raise ValueError(f'resident hash mismatch: {name}')
        out[name] = data[block << 4:(block << 4) + size]
    return out


def subtitle_label(stream_name: str) -> str:
    """Label Skate 3 looks up for a cutscene line (sub_82481FE8)."""
    base = stream_name.rsplit('/', 1)[-1]
    return 'SPEECH_SCRIPTED_' + base.rsplit('.', 1)[0].upper()
