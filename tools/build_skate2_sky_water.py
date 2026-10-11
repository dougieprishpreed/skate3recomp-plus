"""Extract Skate 2's original skydome and ocean district models for New San Van.

Skate 2 loads world/models/DIST_skybox.rx2 and world/models/DIST_Water.rx2 (plus
their _Textures packs) from miscboot.big by fixed path. Skate 3 instead loads
one data-driven model pair per world, so New San Van inherited the template
world's model (Danny Way's sky/backdrop) and had no bay ocean at all.

The source archive is only read. Its RefPack entries are expanded (the game's
BIG layer does this transparently) and written unchanged under unique S2_
basenames: Skate 3 derives resource IDs from the basename, and its own
miscload.big already has a different DIST_Water.rx2. The output directory must
not exist; the completion marker is written last.
"""
import argparse
import hashlib
import json
import mmap
import re
import struct
from pathlib import Path

from inspect_sfil import dictionary, materials, unpack
from world_archive import entries

MODELS = ('DIST_skybox', 'DIST_Water')
TEXTURE_EXPORT = re.compile(rb'\x9b\x0f\x16\x78(.{8})\xac\x46\x2e\x4a', re.S)
# Hashed per-district lighting pages; bit62 may be set (see texture identity).
LIGHTING = re.compile(r'(chromo|layerpage)_DIST_(skybox|Water)_\d{4}$')
MARKER = 'SKY_WATER_COMPLETE_V1'


def texture_exports(data):
    ids = {}
    for m in TEXTURE_EXPORT.finditer(data):
        if m.start() % 4 == 0:
            ids[int.from_bytes(m[1], 'big')] = m.start()
    return ids


def build(miscboot, output):
    if output.exists():
        raise ValueError('output must be a new directory')
    wanted = {f'data/content/world/models/{m}{s}.rx2': (m, s)
              for m in MODELS for s in ('', '_Textures')}
    files, sources = {}, {}
    with miscboot.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for e in entries(data):
            if e.name in wanted:
                packed = bytes(data[e.offset:e.offset + e.size])
                raw = unpack(packed) if packed[:2] == b'\x10\xfb' else packed
                if raw[:8] != b'\x89RW4xb2\0':
                    raise ValueError(f'{e.name}: expected RW4 arena')
                list(dictionary(raw))  # validates the object table bounds
                model, suffix = wanted[e.name]
                name = f'S2_{model}{suffix}.rx2'
                files[name] = raw
                sources[name] = dict(source=e.name, packed_bytes=len(packed),
                                     packed_sha256=hashlib.sha256(packed).hexdigest())
    if len(files) != len(wanted):
        raise ValueError(f'missing source models: {sorted(set(wanted) - {v["source"] for v in sources.values()})}')

    lighting, details = set(), {}
    for model in MODELS:
        mesh = files[f'S2_{model}.rx2']
        exports = texture_exports(files[f'S2_{model}_Textures.rx2'])
        names = {}
        referenced = set()
        for _, channels in materials(mesh):
            for channel, row in channels.items():
                text = row['text']
                if channel in ('Name', 'AttribulatorMaterialName') or not text:
                    continue
                texture_id = int(row['value'], 16)
                referenced.add(texture_id)
                names[texture_id] = text
                if LIGHTING.fullmatch(text):
                    lighting.add(texture_id)
        missing = referenced - set(exports)
        if missing:
            raise ValueError(f'{model}: material textures absent from pack: '
                             + ', '.join(f'{x:016X}' for x in sorted(missing)))
        details[model] = dict(
            meshes=sum(e['kind'] == 0xEB0023 for e in dictionary(mesh)),
            texture_exports=len(exports),
            referenced={f'{k:016X}': names[k] for k in sorted(referenced)})
    if not lighting:
        raise ValueError('no lighting pages found')
    files['lighting_texture_ids.txt'] = (
        'BAM_LIGHTING_TEXTURE_IDS_V1\n' + str(len(lighting)) + '\n' +
        ''.join(f'{x:016X}\n' for x in sorted(lighting))).encode()
    manifest = dict(version=1, source=str(miscboot), models=details, sources=sources,
                    lighting_ids=[f'{x:016X}' for x in sorted(lighting)],
                    files={n: dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest())
                           for n, b in sorted(files.items())})
    output.mkdir(parents=True)
    for name, payload in files.items():
        (output / name).write_bytes(payload)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / MARKER).write_text(MARKER + '\n')
    print(json.dumps(dict(models=details, lighting_ids=manifest['lighting_ids']), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('miscboot', type=Path, help="Skate 2 data/big/miscboot.big")
    parser.add_argument('output', type=Path)
    a = parser.parse_args()
    build(a.miscboot, a.output)
