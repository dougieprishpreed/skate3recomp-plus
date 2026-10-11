#!/usr/bin/env python3
"""Install Skate 2's world audio for New San Van and the Skate 2 DLC maps.

Positional emitters. A world's ambient emitters are maps (.ems). Skate 2 and
Skate 3 share the format: a big-endian u32 count, then 72-byte records (id,
flags, position, box extents, volume, heading, 64-bit emitter key, debug
colour). The key is h64(name) of an aud_emitter record in the game vault, and
an emitter plays the bank of the same name, so a Skate 2 record works in
Skate 3 when Skate 3's vault (db.big, skatercollections) has that aud_emitter.

  BAM (city)   Skate 2 data/audio sfx, crowds, music, reverb (all records
               resolve). Skate 2's speakers map and the speaker records in
               the DLC maps use speaker emitters Skate 3 does not have; they
               are left out and counted in the manifest.
  DLC maps     each map's own map, named by its Locations record in the DLC
               vault (e.g. DLC_School -> data\\audio\\DLC_school.ems).

Painted audio (New San Van only; the DLC maps paint nothing). BAM paints its
ambience beds, its randomized sound zones and its reverb with WorldPainter
layers that Skate 3 cannot read (tools/skate2_worldpainter.py). This tool
writes what the runtime and the New San Van package need to play them:

  worldpainter.txt     Skate 2 layer -> Skate 3 layer key, Skate 2 paint value
                       -> Skate 3 record key, for the three audio layers.
  vault_records.json   records for tools/build_nsv_dlc.py's vault:
                       aud_wp_ambiences/nsv_<bed area> (Skate 2's volume and
                       fades) and aud_wp_emitters/nsv_<zone> (Skate 2's sound
                       zones, emitters Skate 3 lacks left out).
  beds/<n>_dlc_ambience.snr/.sns
                       Skate 2 beds Skate 3 does not ship, as numbered loose
                       beds (n = 2000 + the Skate 2 bed), the stream Skate 3
                       plays for any bed number missing from its own archive
                       (sub_824D3990). Skate 2 beds Skate 3 ships under another
                       name (same stream) use Skate 3's bed number.
Reverb needs no records: Skate 3 ships the same 24 presets (reverb10 was
retuned and BAM never paints it).

Output (originals are never modified):
  <root>/mods/skate2/audio-v1/<map>.ems     filtered copies
  <root>/mods/skate2/audio-v1/empty.ems     no records (Skate 3 ships such maps)
  <root>/mods/skate2/audio-v1/worlds.txt    stream -> map per role, for the runtime
  <root>/mods/skate2/audio-v1/worldpainter.txt, vault_records.json, beds/
  <root>/mods/skate2/audio-v1/manifest.json, AUDIO_COMPLETE_V2

The runtime (src/skate3_world_loading.cpp) mounts the folder at
data\\audio\\skate2, gives the audio world these maps in place of the template
district's while New San Van or a DLC map loads, serves the beds at
data\\audio\\<n>_dlc_ambience.*, and translates BAM's audio layers. Run
tools/build_nsv_dlc.py afterwards to put the records into the package.

Usage (repository root):
  python tools/build_skate2_audio.py [--skate2 "<Skate 2>/data"]
      [--dlc-source "<Skate 2 DLC folder>"] [--root out/build/relwithdebinfo] [--uninstall]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import shutil
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import skate2_worldpainter as wp  # noqa: E402
import world_archive  # noqa: E402
from attrib_vault import Vault  # noqa: E402
from build_skate2_dlc_maps import PACKAGES  # noqa: E402
from inspect_sfil import unpack  # noqa: E402
from vlt64 import h64  # noqa: E402

REPO = HERE.parent
FOLDER = 'audio-v1'
MARKER = 'AUDIO_COMPLETE_V2'
OLD_MARKERS = ('AUDIO_COMPLETE_V1',)
RECORD = 72
ROLES = ('sfx', 'music', 'reverb', 'crowds', 'speakers')
CITY = {'sfx': 'sfx.ems', 'music': 'music.ems', 'reverb': 'reverb.ems', 'crowds': 'crowds.ems'}
DLC_PACKAGES = {
    'skate1_classic_xbox2.big': 'dlc_skate1_classic',
    'maloof_money_cup_xbox2.big': 'dlc_maloof_money_cup',
    'rob_fantasy_plaza_xbox2.big': 'dlc_rob_fantasy_plaza',
}
FE_LOCATIONS = h64('fe_locations')
AUD_EMITTER = h64('aud_emitter')
AUD_WP_AMBIENCES = h64('aud_wp_ambiences')
AUD_WP_EMITTERS = h64('aud_wp_emitters')
AUD_REVERB = h64('aud_reverb')
REFSPEC = 0x97C5113DE9BD1AD6          # Attrib::RefSpec (array entries)
BED_BASE = 2000                       # loose bed number = 2000 + Skate 2 bed
PREFIX = 'nsv_'                       # New San Van's own records
AUDIO_LAYERS = {0x0F: ('audio_ambience', 'ambience'), 0x10: ('audio_emitters', 'emitters'),
                0x12: ('audio_reverb', 'reverb')}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def archive_files(path, wanted):
    """{name: bytes} for archive entries accepted by wanted(name); RefPack expanded."""
    out = {}
    with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for entry in world_archive.entries(data):
            if wanted(entry.name):
                raw = bytes(data[entry.offset:entry.offset + entry.size])
                # BIGF archives carry no compression flag; vault files may
                # still be RefPack blocks.
                packed = entry.expanded or (entry.name.endswith(('.vlt', '.bin')) and raw[:2] == b'\x10\xfb')
                out[entry.name] = unpack(raw) if packed else raw
    return out


def vault(path, name):
    files = archive_files(path, lambda n: n in (f'data/db/{name}.vlt', f'data/db/{name}.bin'))
    if len(files) != 2:
        raise SystemExit(f'{path} lacks {name}.vlt/.bin')
    return Vault(files[f'data/db/{name}.vlt'], files[f'data/db/{name}.bin'])


def records(data, name):
    if len(data) < 4:
        raise SystemExit(f'{name}: truncated')
    count = struct.unpack_from('>I', data, 0)[0]
    if len(data) != 4 + count * RECORD:
        raise SystemExit(f'{name}: {len(data)} bytes is not 4 + {count} x {RECORD}')
    return [data[4 + i * RECORD:4 + (i + 1) * RECORD] for i in range(count)]


def emitter_key(record):
    return struct.unpack_from('>Q', record, 48)[0]


def filtered(data, name, known):
    rows = records(data, name)
    kept = [r for r in rows if emitter_key(r) in known]
    dropped = {}
    for r in rows:
        if emitter_key(r) not in known:
            key = f'{emitter_key(r):016X}'
            dropped[key] = dropped.get(key, 0) + 1
    return struct.pack('>I', len(kept)) + b''.join(kept), len(rows), len(kept), dropped


def dlc_maps(source):
    """{stream: (ems file name, bytes, package)} from the DLC packages."""
    result = {}
    for package, vault_name in DLC_PACKAGES.items():
        hits = sorted(source.rglob(package))
        if not hits:
            raise SystemExit(f'{package} not found under {source}')
        files = archive_files(hits[0], lambda n: n.endswith('.ems') or
                              n in (f'data/db/{vault_name}.vlt', f'data/db/{vault_name}.bin'))
        dlc = Vault(files[f'data/db/{vault_name}.vlt'], files[f'data/db/{vault_name}.bin'])
        for record in dlc.records():
            if record.cls != FE_LOCATIONS:
                continue
            texts = dlc.strings_of(record)
            ems = [t for t in texts if t.lower().endswith('.ems')]
            streams = [t for t in texts if t in PACKAGES[package]]
            if len(ems) == 1 and len(streams) == 1:
                member = 'data/audio/' + ems[0].replace('\\', '/').split('/')[-1]
                if member not in files:
                    raise SystemExit(f'{package}: {member} named by {streams[0]} is missing')
                result[streams[0]] = (member.split('/')[-1], files[member], package)
    return result


# ---- ambience beds ----------------------------------------------------------------
def beds(audio, suffix):
    """{bed number: (member name, bytes)} from <audio>/ambience[resident].big
    (members 'NN_<name>.sns' / '.snr'; the game reads the number off the name)."""
    archive = audio / ('ambience.big' if suffix == '.sns' else 'ambienceresident.big')
    out = {}
    for name, data in archive_files(archive, lambda n: n.endswith(suffix)).items():
        base = name.rsplit('/', 1)[-1]
        if base[:2].isdigit():
            out[int(base[:2])] = (base, data)
    return out


def same_stream(a, b):
    """The same recording re-packed: a fifth of 48-byte samples recur (0 for
    unrelated beds; Skate 3 re-blocked the Skate 2 streams it kept)."""
    probes = range(4096, len(a) - 64, max(1, len(a) // 200))
    hits = sum(a[o:o + 48] in b for o in probes)
    return hits * 10 >= len(probes)


def map_beds(s2_audio, s3_audio):
    """{Skate 2 bed: (Skate 3 bed or BED_BASE + bed, resident, stream, shared name)}."""
    s2_snr, s2_sns = beds(s2_audio, '.snr'), beds(s2_audio, '.sns')
    s3_snr, s3_sns = beds(s3_audio, '.snr'), beds(s3_audio, '.sns')
    result = {}
    for bed, (name, header) in sorted(s2_snr.items()):
        if bed not in s2_sns:
            raise SystemExit(f'Skate 2 bed {name} has no stream')
        twins = [n for n, (_, h) in s3_snr.items() if h == header]
        shared = None
        if len(twins) == 1 and twins[0] in s3_sns and same_stream(s2_sns[bed][1], s3_sns[twins[0]][1]):
            shared = twins[0]
        number = shared if shared is not None else BED_BASE + bed
        result[bed] = (number, header, s2_sns[bed][1],
                       s3_sns[shared][0] if shared is not None else None)
    return result


# ---- painted audio records ---------------------------------------------------------
def record_json(cls, name, parent, types, layout, entries):
    return {'class': cls, 'collection': name, 'parent': f'{parent:016X}',
            'types': [f'{t:016X}' for t in types], 'layout': layout.hex() if layout else None,
            'entries': entries}


def ambience_records(s2, bed_map):
    """One aud_wp_ambiences record per Skate 2 bed area: its volume and fades,
    the bed number Skate 3 plays it from."""
    names = {h64(n): n for n in wp.VALUES['ambience'].values()}
    out = {}
    for r in s2.records():
        if r.cls != AUD_WP_AMBIENCES or r.collection not in names or r.layout is None:
            continue
        volume, bed, fade_in, fade_out = struct.unpack_from('>fIff', s2.bin, r.layout)
        if bed not in bed_map:
            raise SystemExit(f'{names[r.collection]}: Skate 2 bed {bed} missing')
        layout = struct.pack('>fIff', volume, bed_map[bed][0], fade_in, fade_out)
        out[names[r.collection]] = record_json('aud_wp_ambiences', PREFIX + names[r.collection],
                                               r.parent, r.types, layout, [])
    missing = set(names.values()) - set(out)
    if missing:
        raise SystemExit(f'Skate 2 vault lacks ambience records {sorted(missing)}')
    return out


def emitter_records(s2, known):
    """One aud_wp_emitters record per Skate 2 sound zone (e_* and the
    transition zones t_*): Skate 2's record with the aud_emitter references
    Skate 3 lacks removed, together with their weights."""
    names = {h64(n): n for n in wp.VALUES['emitters'].values()}
    out, dropped = {}, {}
    for r in s2.records():
        if r.cls != AUD_WP_EMITTERS or r.collection not in names:
            continue
        name = names[r.collection]
        refs = [e for e in r.entries if e.type == REFSPEC and e.target is not None]
        if len(refs) > 1 or r.layout is None:
            raise SystemExit(f'{name}: unexpected aud_wp_emitters layout')
        # A zone without its own arrays inherits default's (an empty list).
        count = struct.unpack_from('>H', s2.bin, refs[0].target)[0] if refs else 0
        if refs and struct.unpack_from('>H', s2.bin, refs[0].target + 4)[0] != 24:
            raise SystemExit(f'{name}: RefSpec arrays of unexpected elements')
        keep = [i for i in range(count)
                if struct.unpack_from('>Q', s2.bin, refs[0].target + 8 + 24 * i + 8)[0] in known]
        dropped[name] = count - len(keep)
        entries = []
        for e in r.entries:
            if e.target is None:
                entries.append({'key': f'{e.key:016X}', 'type': r.types.index(e.type),
                                'flags': e.flags, 'value': e.value})
                continue
            n, _, width, pad = struct.unpack_from('>HHHH', s2.bin, e.target)
            if n != count:
                raise SystemExit(f'{name}: array lengths differ')
            items = [s2.bin[e.target + 8 + width * i:e.target + 8 + width * (i + 1)] for i in keep]
            array = struct.pack('>HHHH', len(items), len(items), width, pad) + b''.join(items)
            entries.append({'key': f'{e.key:016X}', 'type': r.types.index(e.type),
                            'flags': e.flags, 'data': array.hex()})
        out[name] = record_json('aud_wp_emitters', PREFIX + name, r.parent, r.types,
                                s2.bin[r.layout:r.layout + 16], entries)
    missing = set(names.values()) - set(out)
    if missing:
        raise SystemExit(f'Skate 2 vault lacks sound zones {sorted(missing)}')
    return out, dropped


def translation_lines(reverbs):
    """worldpainter.txt: layer and value translations for the audio layers.
    A painted value is h64 of the record it names; New San Van's beds and
    zones are its own records (nsv_*), reverb uses Skate 3's presets."""
    lines = ['# Skate 2 WorldPainter -> Skate 3, written by tools/build_skate2_audio.py.',
             '# layer <Skate 2 eLayerID> <Skate 3 layer key> <name>',
             '# value <Skate 2 paint value> <Skate 3 record key> <class/record>']
    for layer, (name, _) in sorted(AUDIO_LAYERS.items()):
        lines.append(f'layer {layer} {h64(name):016X} {name}')
    for category, cls in (('ambience', 'aud_wp_ambiences'), ('emitters', 'aud_wp_emitters')):
        for value, name in sorted(wp.VALUES[category].items()):
            lines.append(f'value {value} {h64(PREFIX + name):016X} {cls}/{PREFIX + name}')
    for value, name in sorted(wp.VALUES['reverb'].items()):
        if h64(name) not in reverbs:
            raise SystemExit(f'Skate 3 vault lacks aud_reverb/{name}')
        lines.append(f'value {value} {h64(name):016X} aud_reverb/{name}')
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--skate2', type=Path, default=REPO.parent / 'Skate 2' / 'data')
    parser.add_argument('--dlc-source', type=Path)
    parser.add_argument('--native-data', type=Path)
    parser.add_argument('--root', type=Path, default=REPO / 'out' / 'build' / 'relwithdebinfo')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    dest = args.root / 'mods' / 'skate2' / FOLDER
    if args.uninstall:
        if dest.exists():
            shutil.rmtree(dest)
        print(f'removed {dest}')
        return

    native = args.native_data or args.root / 'game' / 'data'
    s3 = vault(native / 'big' / 'db.big', 'skatercollections')
    s2 = vault(args.skate2 / 'db' / 'db.big', 'skater')
    known = {r.collection for r in s3.records() if r.cls == AUD_EMITTER}
    reverbs = {r.collection for r in s3.records() if r.cls == AUD_REVERB}
    if len(known) < 100:
        raise SystemExit(f'only {len(known)} aud_emitter records in Skate 3 vault')
    out, worlds = {}, {}
    manifest = {'version': 2, 'maps': {}, 'worlds': {}, 'beds': {}, 'zones_dropped': {}}

    def add(name, data, source):
        payload, total, kept, dropped = filtered(data, name, known)
        out[name] = payload
        manifest['maps'][name] = {'source': source, 'source_sha256': sha256(data),
                                  'records': total, 'kept': kept, 'dropped': dropped}

    for name in CITY.values():
        path = args.skate2 / 'audio' / name
        add(name, path.read_bytes(), str(path))
    worlds['BAM'] = dict(CITY, speakers='empty.ems')
    if args.dlc_source:
        for stream, (name, data, package) in sorted(dlc_maps(args.dlc_source).items()):
            add(name, data, f'{package}:data/audio/{name}')
            worlds[stream] = {'sfx': name}
    out['empty.ems'] = struct.pack('>I', 0)
    for stream, roles in worlds.items():
        manifest['worlds'][stream] = {role: roles.get(role, 'empty.ems') for role in ROLES}

    bed_map = map_beds(args.skate2 / 'audio', native / 'audio')
    for bed, (number, header, stream, shared) in sorted(bed_map.items()):
        manifest['beds'][f'{bed:02d}'] = shared or f'beds/{number}_dlc_ambience'
        if shared is None:
            out[f'beds/{number}_dlc_ambience.snr'] = header
            out[f'beds/{number}_dlc_ambience.sns'] = stream
    ambiences = ambience_records(s2, bed_map)
    zones, dropped = emitter_records(s2, known)
    manifest['zones_dropped'] = {k: v for k, v in dropped.items() if v}
    vault_records = {'source': 'tools/build_skate2_audio.py',
                     'records': list(ambiences.values()) + list(zones.values())}

    staging = dest.with_name(FOLDER + '.staging')
    if staging.exists():
        shutil.rmtree(staging)
    (staging / 'beds').mkdir(parents=True)
    for name, payload in out.items():
        (staging / name).write_bytes(payload)
    lines = ['# Skate 2 world audio maps, written by tools/build_skate2_audio.py.',
             '# <world stream> <role>=<map in this folder> for sfx music reverb crowds speakers']
    lines += [stream + ' ' + ' '.join(f'{r}={m}' for r, m in roles.items())
              for stream, roles in manifest['worlds'].items()]
    (staging / 'worlds.txt').write_text('\n'.join(lines) + '\n', encoding='ascii')
    (staging / 'worldpainter.txt').write_text('\n'.join(translation_lines(reverbs)) + '\n',
                                              encoding='ascii')
    (staging / 'vault_records.json').write_text(json.dumps(vault_records, indent=1) + '\n',
                                                encoding='ascii')
    (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='ascii')
    (staging / MARKER).write_text(MARKER + '\n', encoding='ascii')
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)
    for name, info in manifest['maps'].items():
        print(f"{name:28s} {info['kept']:4d}/{info['records']:4d} emitters"
              + (f"  (left out: {sum(info['dropped'].values())} not in Skate 3)" if info['dropped'] else ''))
    shared = sum(1 for v in manifest['beds'].values() if not v.startswith('beds/'))
    print(f'ambience beds: {shared} shipped by Skate 3, {len(bed_map) - shared} added as loose beds')
    print(f'painted audio records: {len(ambiences)} bed areas, {len(zones)} sound zones '
          f'({sum(dropped.values())} emitter references left out: not in Skate 3)')
    print(f'installed {len(worlds)} worlds -> {dest}; now run tools/build_nsv_dlc.py')


if __name__ == '__main__':
    main()
