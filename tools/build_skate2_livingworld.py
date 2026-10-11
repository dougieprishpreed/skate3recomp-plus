#!/usr/bin/env python3
"""Prepare isolated original living-world actors and map-specific Attrib records.

Original archives are read-only. Output lives under mods/skate2/livingworld-v1.
The runtime loads the full original vehicle roster. Pedestrian navigation,
animation and sound registration still need their own integration. Run
build_nsv_dlc.py afterwards to package the prepared vault records.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import hashlib
import json
import mmap
import re
import struct
import sys
from pathlib import Path

from attrib_vault import Vault
from world_archive import decode_archive_payload
from skate2_worldpainter import VALUES, stream_layers
from vlt64 import h64
from world_archive import entries, build_eb_archive
from skate2_traffic_audio import retarget_traffic_bank
from skate2_speech import isolate_events, read_events, native_namespaces, PREFERRED_NAMESPACE, prepare_tables

REPO = Path(__file__).resolve().parent.parent
PREFIX = 'nsv_s2_'
CLASSES = ['livingworld_census', 'livingworld_categorygroups',
           'livingworld_entitycategories', 'livingworld_entities',
           'livingworld_models', 'livingworld_vehicle_drivers',
           'livingworld_vehicle_characteristics']
# Fixed fields, checked against both vaults and their S3 consumers. Do not
# infer a layout size from the number of distinct types in a record.
FIXED = {'livingworld_census': 32, 'livingworld_entities': 32,
         'livingworld_models': 8, 'livingworld_vehicle_drivers': 40,
         'livingworld_vehicle_characteristics': 64}


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    if path.exists():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        backup = path.parent / (path.name + '.before-' + digest)
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    path.write_bytes(data)


def json_file(path, data):
    put(path, (json.dumps(data, indent=2) + '\n').encode('ascii'))


def read_big(path, want=lambda name: True):
    with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        return {e.name: decode_archive_payload(bytes(raw[e.offset:e.offset+e.size]))
                for e in entries(raw) if want(e.name)}


def validate_vehicle_compatibility(source, native_data):
    # Presentation reuses the native vehicle animation database, and texture
    # routing recognizes imported identities without replacing native assets.
    # Prove both assumptions for the supplied data, not a research snapshot.
    def vehicle_db(path):
        files = read_big(path, lambda n: n.endswith('/VehiclesDB.abin'))
        if len(files) != 1:
            raise ValueError(f'{path}: missing or ambiguous vehicle animation database')
        return next(iter(files.values()))
    if vehicle_db(source/'big/anim.big') != vehicle_db(native_data/'big/miscload.big'):
        raise ValueError('original and native vehicle animation databases differ')
    def texture_ids(path):
        with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            return {int(Path(e.name).stem, 16) for e in entries(raw)
                    if '/texture/' in e.name and e.name.endswith('.rx2')}
    if texture_ids(source/'content/livingworld.big') & texture_ids(native_data/'content/livingworld.big'):
        raise ValueError('original actor texture identities collide with native actors')


def prepare_assets(source, out):
    files = read_big(source / 'content/livingworld.big')
    recipes, assets, textures = {}, [], set()
    for name, data in files.items():
        parts = name.split('/')
        if '/recipe/' in name:
            family, filename = parts[-2:]
            stem, suffix = filename.rsplit('.', 1)
            if suffix == 'recipe':
                n = struct.unpack_from('>I', data, 4)[0]
                original = data[8:8+n]
                assert struct.unpack_from('>I', data)[0] == 7
                assert original.rstrip(b'\0').decode() == stem
                renamed = PREFIX.encode() + original
                data = data[:4] + struct.pack('>I', len(renamed)) + renamed + data[8+n:]
                recipes[stem] = family
            elif suffix == 'xml':
                data, count = re.subn(rb'(<compositeasset n=")([^"]+)',
                                     lambda m: m[1] + PREFIX.encode() + m[2], data, count=1)
                assert count == 1
            target = f'data/content/recipe/{PREFIX}{family}/{PREFIX}{filename}'
        elif '/model/' in name:
            assert len(parts) == 7 and parts[3] == 'model'
            parts[2] = PREFIX + parts[2]
            parts[4] = PREFIX + parts[4]
            target = '/'.join(parts)
        elif '/texture/' in name:
            parts[2] = PREFIX + parts[2]
            target = '/'.join(parts)
            assert name.endswith('.rx2')
            # The basename namespace is also checked by the path adapter.
            textures.add(int(Path(name).stem, 16))
        else:
            raise ValueError(f'unexpected actor asset path: {name}')
        put(out / 'loose' / target, data)
        assets.append(dict(source=name, target=target, bytes=len(data),
                           sha256=hashlib.sha256(data).hexdigest()))

    # Verify every original recipe reference closes over the original archive.
    basenames = {Path(n).stem.lower() for n in files if n.endswith('.rx2')}
    for name, data in files.items():
        if not name.endswith('.xml'):
            continue
        for ident in re.findall(rb'(?:arenaid|<sp id)="(0x[0-9a-fA-F]+)"', data):
            assert ident.decode().lower() in basenames, (name, ident)
    json_file(out / 'assets.json', assets)
    put(out / 'texture_ids.txt', ('NSV_LW_TEXTURE_IDS_V1\n' +
        str(len(textures)) + '\n' + ''.join(f'{k:016X}\n' for k in sorted(textures))).encode())
    return recipes


def prepare_records(recipes, out, source, bam_stream):
    rev = json.loads(Path(__file__).with_name('skate2_livingworld_names.json').read_text())
    if any(h64(n) != int(k, 16) for k, n in rev.items()):
        raise ValueError('living-world naming schema has an invalid key')
    name = lambda k: rev.get(f'{k:016x}', f'id_{k:016x}')
    files = read_big(source/'db/db.big', lambda n: n in {'data/db/skater.vlt', 'data/db/skater.bin'})
    v = Vault(files['data/db/skater.vlt'], files['data/db/skater.bin'])
    records = {(r.cls, r.collection): r for r in v.records()}
    cls_names = {h64(n): n for n in CLASSES}
    chosen = set()
    boundaries = {len(v.bin)}
    for r in records.values():
        if r.layout is not None:
            boundaries.add(r.layout)
        boundaries.update(e.target for e in r.entries if e.target is not None)
    boundaries.update(v.pointers.values())
    boundaries = sorted(boundaries)

    def span(at):
        return boundaries[bisect.bisect_right(boundaries, at)] - at

    def include(key):
        if key in chosen:
            return
        r = records[key]
        chosen.add(key)
        if r.parent:
            include((r.cls, r.parent))
        blocks = [(e.target, span(e.target)) for e in r.entries if e.target is not None]
        if r.layout is not None:
            blocks.append((r.layout, FIXED[cls_names[r.cls]]))
        for at, size in blocks:
            for i in range(at, at + size - 15, 4):
                pair = struct.unpack_from('>QQ', v.bin, i)
                if pair in records and pair[0] in cls_names:
                    include(pair)
    # Census/categories/entities preserve the original category distribution.
    # Models with archive-backed recipes plus their ancestors retain the
    # engine's parent/category variant selection mechanism.
    for key, r in records.items():
        if r.cls in {h64('livingworld_census'), h64('livingworld_vehicle_drivers'),
                     h64('livingworld_vehicle_characteristics')}:
            include(key)
        if r.cls == h64('livingworld_models') and r.layout is not None:
            recipe = v.string(v.pointers.get(('bin', r.layout)))
            if recipe in recipes:
                include(key)
    renamed = {k: PREFIX + name(k[1]) for k in chosen}
    remap = {k: h64(n) for k, n in renamed.items()}
    # Block boundaries let variable-size entry blobs retain their exact bytes
    # and padding. Fixed-layout fields use the explicit schema sizes above.
    def blob(at, size=None, text_override=None):
        if size is None:
            size = span(at)
        assert size > 0 and at + size <= len(v.bin)
        raw = bytearray(v.bin[at:at+size])
        # RefSpec is class u64, collection u64, cache u64. Retarget only
        # recognized source collection pairs, never numeric actor/model IDs.
        for i in range(0, size - 15, 4):
            pair = struct.unpack_from('>QQ', raw, i)
            if pair in remap:
                struct.pack_into('>Q', raw, i+8, remap[pair])
            elif pair[0] in cls_names and pair[1]:
                raise ValueError(f'unprepared living-world reference: {name(pair[0])}/{name(pair[1])}')
        ptrs = []
        for (space, off), target in v.pointers.items():
            if space == 'bin' and at <= off < at + size:
                text = v.string(target)
                # Empty strings at StrE+8 are valid. Arbitrary structures
                # require an explicit schema instead of a guessed pointer.
                assert text is not None or v.bin[target:target+1] == b'\0'
                ptrs.append(dict(offset=off-at, string=text_override if text_override is not None else text or ''))
        return dict(data=raw.hex(), pointers=ptrs)

    result, model_groups = [], {'vehicles': [], 'pedestrians': []}
    voices = []
    for key in sorted(chosen):
        r = records[key]
        cls, coll = cls_names[r.cls], renamed[key]
        layout = None
        if r.layout is not None:
            recipe = v.string(v.pointers.get(('bin', r.layout))) if cls == 'livingworld_models' else None
            layout = blob(r.layout, FIXED[cls], PREFIX + recipe if recipe in recipes else None)
            if cls == 'livingworld_models':
                # NONE changed from 100 to 104, including inherited defaults.
                # Original voices require isolated speech registration; native NONE avoids wrong dialogue.
                raw = bytearray.fromhex(layout['data'])
                if recipes.get(recipe) == 'livingworld':
                    voice = struct.unpack_from('>I', raw, 4)[0]
                    if not 0 <= voice <= 100:
                        raise ValueError('invalid original pedestrian voice')
                    voices.append((h64(coll), 104 if voice == 100 else voice))
                if struct.unpack_from('>I', raw, 4)[0] == 100 or recipes.get(recipe) == 'livingworld':
                    struct.pack_into('>I', raw, 4, 104)
                layout['data'] = raw.hex()
                if recipe in recipes:
                    model_groups['vehicles' if recipes[recipe] == 'vehicle' else 'pedestrians'].append(coll)
        es = []
        for e in r.entries:
            row = dict(key=f'{e.key:016X}', type=r.types.index(e.type), flags=e.flags)
            if e.target is None:
                row['value'] = e.value
            elif e.type == h64('EA::Reflection::Text'):
                row['string'] = v.string(e.target) or ''
            else:
                row.update(blob(e.target))
            es.append(row)
        result.append(dict(**{'class': cls}, collection=coll,
                           parent=f'{remap[(r.cls,r.parent)]:016X}' if r.parent else '0',
                           types=[f'{t:016X}' for t in r.types], layout=layout,
                           entries=es, source=name(r.collection)))
    groups = dict(model_groups, actors=model_groups['vehicles'] + model_groups['pedestrians'])
    for family, models in groups.items():
        raw = struct.pack('>4H', len(models), len(models), 24, 0) + b''.join(
            struct.pack('>QQQ', h64('livingworld_models'), h64(n), 0) for n in sorted(models))
        result.append(dict(**{'class': 'livingworld_load_groups'},
                           collection=PREFIX+family, parent='0', layout=None,
                           types=[f'{h64("Attrib::RefSpec"):016X}'], entries=[
                               dict(key=f'{h64("models"):016X}', type=0, flags=0xA00, data=raw.hex())]))
    paint = stream_layers(bam_stream)
    for layer, table, expected in [(2, 'npc_census', 10), (5, 'vehicle_census', 8)]:
        found = set(paint[layer]) & VALUES[table].keys()
        if len(found) != expected:
            raise ValueError(f'incomplete BAM {table}: expected {expected} named values, got {len(found)}')
    lines = ['# Original census; runtime requires prepared navigation for pedestrians.']
    for layer, table, dest in [(2, 'npc_census', 'livingworld_npc_census'),
                               (5, 'vehicle_census', 'livingworld_vehicle_census')]:
        lines.append(f'layer {layer} {h64(dest):016X} {dest}')
        for value in sorted(paint[layer]):
            label = VALUES[table].get(value)
            if not label:
                continue
            # Executable paint table spelling vs both titles' actual vault.
            if label == 'district_park01_vehicles_census':
                label = 'district_parks01_vehicles_census'
            key = (h64('livingworld_census'), h64(label))
            assert key in renamed, label
            lines.append(f'value {value} {remap[key]:016X} {renamed[key]}')
    put(out/'worldpainter.txt', ('\n'.join(lines)+'\n').encode())
    json_file(out/'vault_records.json', dict(records=result))
    if len(voices) != 38:
        raise ValueError('expected all 38 pedestrian voice mappings')
    put(out/'pedestrian_voices.txt', ('NSV_PED_VOICES_V1\n38\n' +
        ''.join(f'{key:016X} {voice}\n' for key, voice in sorted(voices))).encode('ascii'))
    return dict(records=len(result), classes=dict(collections.Counter(r['class'] for r in result)),
                vehicle_models=len(model_groups['vehicles']), pedestrian_models=len(model_groups['pedestrians']))


def prepare_speech_audio(source, out, native_data):
    """Keep the original samples/programs together in an isolated numeric bank.

    Names are not registered globally: native SFX sends numeric events. Only
    common events with identical IDs may be dispatched to the original bank.
    """
    archive = source/'audio/english/livingworldspeech.big'
    bank = read_big(archive, lambda n: n.endswith(('.evt', 'hdr.big', 'sth.big')))
    native = read_big(native_data/'audio/english/livingworldspeech.big', lambda n: n.endswith('.evt'))
    evt = [v for n,v in bank.items() if n.endswith('.evt')]
    if len(evt) != 1 or len(native) != 1:
        raise ValueError('missing or ambiguous living-world speech metadata')
    original = read_events(evt[0])
    known = {e.name: e.ident for e in read_events(next(iter(native.values()))).events}
    common = [e.ident for e in original.events if known.get(e.name) == e.ident]
    if len(common) != 47:
        raise ValueError('unsupported living-world speech event mapping')
    occupied = native_namespaces(native_data)
    namespace = PREFERRED_NAMESPACE
    while namespace in occupied:
        namespace += 1
    files = {'livingworld_Events.evt': isolate_events(evt[0], namespace, occupied)}
    tables = []
    for suffix in ('hdr.big', 'sth.big'):
        matches = [v for n,v in bank.items() if n.endswith(suffix)]
        if len(matches) != 1:
            raise ValueError('missing or ambiguous original speech table')
        tables.append(matches[0])
    hdr, sth, sample_ids = prepare_tables(*tables, native_data)
    files['livingworldhdr.big'], files['livingworldsth.big'] = hdr, sth
    put(out/'speech_samples.txt', ('NSV_SPEECH_SAMPLES_V1\n' +
        f'{len(evt[0])} {len(sample_ids)}\n' +
        ''.join(f'{ident:04X}\n' for ident in sorted(sample_ids))).encode('ascii'))
    samples = read_big(archive, lambda n: n.endswith('.dat'))
    if {Path(n).stem for n in samples} != {Path(e.name).stem for e in entries(hdr)}:
        raise ValueError('speech sample/header membership mismatch')
    files['samples.big'] = build_eb_archive(list(samples.items()))
    result = []
    for name, data in files.items():
        target = 'data/audio/nsv_s2_speech/' + name
        put(out/'loose'/target, data)
        result.append(dict(target=target, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
    put(out/'speech_events.txt', ('NSV_SPEECH_EVENTS_V1\n47\n' +
        ''.join(f'{ident:04X}\n' for ident in sorted(common))).encode('ascii'))
    json_file(out/'speech_audio.json', result)
    return result


def prepare_traffic_audio(source, out, native_data):
    # The installed files are only the traffic banks used by the runtime.
    # Behavior/pedestrian research extracts do not belong in a mod install.
    traffic = []
    source_audio = read_big(source/'audio/vehicle.big')
    source_csi = source_audio['AEMS_TRAFFIC.csi']
    registry = read_big(native_data/'audio/audiofiles.big', lambda n: n.endswith('/AEMS_TRAFFIC.csi'))
    if len(registry) != 1:
        raise ValueError('native traffic CSI registry missing or ambiguous')
    native_csi = next(iter(registry.values()))
    for name, data in source_audio.items():
        if not name.endswith('.abk'):
            continue
        target = 'data/audio/nsv_s2_traffic/' + Path(name).name
        adapted, bindings = retarget_traffic_bank(data, source_csi, native_csi)
        put(out/'loose'/target, adapted)
        traffic.append(dict(source=name, target=target, bytes=len(adapted),
                            sha256=hashlib.sha256(adapted).hexdigest(),
                            source_sha256=hashlib.sha256(data).hexdigest(), bindings=bindings))
    if len(traffic) != 10:
        raise ValueError('eight original engines, horn and skid required')
    json_file(out/'traffic_audio.json', traffic)
    # Verify the actual VFS OpenFile route for every vehicle dependency,
    # not just the directory aliases. Header + size checks catch stale mounts.
    runtime = json.loads((out/'assets.json').read_bytes()) + traffic + prepare_speech_audio(source, out, native_data)
    lines = ['NSV_LW_FILES_V2', str(len(runtime))]
    for a in runtime:
        data = (out/'loose'/a['target']).read_bytes()
        lines.append(f"{a['target']} {len(data)} {data[:8].hex()}")
    put(out/'runtime_files.txt', ('\n'.join(lines)+'\n').encode())
    return len(traffic)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, default=REPO.parent/'Skate 2/data')
    ap.add_argument('--root', type=Path, default=REPO/'out/build/relwithdebinfo')
    ap.add_argument('--native-data', type=Path, help='Skate 3 data directory (default: <root>/game/data)')
    ap.add_argument('--bam-stream', type=Path, help='Prepared BAM stream (default: <root>/mods/skate2/world-support-gvr-v1)')
    args = ap.parse_args()
    native_data = args.native_data or args.root/'game/data'
    bam_stream = args.bam_stream or args.root/'mods/skate2/world-support-gvr-v1'
    if not (args.source/'db/db.big').is_file() or not bam_stream.is_dir():
        ap.error('original Skate 2 db.big and prepared BAM stream are required')
    validate_vehicle_compatibility(args.source, native_data)
    out = args.root.resolve()/'mods/skate2/livingworld-v1'
    out.mkdir(parents=True, exist_ok=True)
    (out/'LIVINGWORLD_COMPLETE_V1').unlink(missing_ok=True)
    recipes = prepare_assets(args.source, out)
    counts = prepare_records(recipes, out, args.source, bam_stream)
    counts.update(recipes=len(recipes), traffic_banks=prepare_traffic_audio(args.source, out, native_data),
                  pedestrian_models_enabled=True, original_pedestrian_speech=True, original_bank_registration=True)
    json_file(out/'summary.json', counts)
    put(out/'LIVINGWORLD_COMPLETE_V1', b'NSV original actors; full original vehicles and traffic banks\n')
    print(json.dumps(counts, indent=2))


if __name__ == '__main__':
    main()
