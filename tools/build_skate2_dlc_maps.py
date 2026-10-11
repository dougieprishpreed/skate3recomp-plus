#!/usr/bin/env python3
"""Install Skate 2's DLC maps for the New San Vanelona Locations entries.

Reads the Skate 2 map DLC packages (read-only) and preserves their original
assets as loose world streams. The three Classic maps gain population paint,
native traffic lanes and pedestrian Recast navigation before publication:

  <root>/mods/skate2/dlc-maps-v1/<Stream>/...   (catalogs, globals, cells)
  <root>/mods/skate2/dlc-maps-v1/lighting_texture_ids.txt
  <root>/mods/skate2/dlc-maps-v1/manifest.json
  <root>/mods/skate2/dlc-maps-v1/DLC_MAPS_COMPLETE_V1

The runtime (src/skate3_world_loading.cpp) mounts each stream folder at
data/content/world/stream/<Stream> and loads it in place of BAM when one of
the DLC entries in tools/nsv_spawns.json (field "stream") is chosen.

Packages (skate2ng DLC, title 4541087F):
  skate1_classic_xbox2.big     DLC_School, DLC_Community_Center, DLC_Parkade
  maloof_money_cup_xbox2.big   DLC_MaloofMoneyCup
  rob_fantasy_plaza_xbox2.big  DLC_Rob_Fantasy_Plaza

Usage (repository root):
  python tools/build_skate2_dlc_maps.py --source "<Skate 2 DLC folder>"
      --skate2-source "<Skate 2 data>" --native-data "<Skate 3 data>"
      [--root out/build/relwithdebinfo] [--nav-baker "<helper>"]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import shutil
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import world_archive  # noqa: E402
from build_bam_lighting_ids import inventory  # noqa: E402
from skate2_navigation import prepare_navigation
from skate2_dlc_livingworld import prepare_livingworld

REPO = HERE.parent
FOLDER = 'dlc-maps-v1'
MARKER = 'DLC_MAPS_COMPLETE_V1'
PACKAGES = {
    'skate1_classic_xbox2.big': ('DLC_School', 'DLC_Community_Center', 'DLC_Parkade'),
    'maloof_money_cup_xbox2.big': ('DLC_MaloofMoneyCup',),
    'rob_fantasy_plaza_xbox2.big': ('DLC_Rob_Fantasy_Plaza',),
}
STREAM_PREFIX = 'data/content/world/stream/'
SECTION_MAGIC = {'.xsm': b'CMAP', '.xst': b'ATOC', '.xsf': b'SFIL'}
LIVINGWORLD_STREAMS = ('DLC_School', 'DLC_Community_Center', 'DLC_Parkade')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def check_stream(folder, stream):
    """The catalogs and globals the loader opens, version 3, like BAM's."""
    required = [f'{stream}_{group}{suffix}' for group in ('Pres', 'Sim', 'Tex')
                for suffix in ('.xsm', '.xst')] + ['cPres_Global.xsf', 'cSim_Global.xsf']
    for name in required:
        path = folder / name
        if not path.is_file():
            raise SystemExit(f'{stream}: missing {name}')
        head = path.read_bytes()[:8]
        if head != SECTION_MAGIC[path.suffix] + b'\0\0\0\3':
            raise SystemExit(f'{stream}: {name} is not a version 3 {SECTION_MAGIC[path.suffix]!r}')


def find_package(source, name):
    hits = sorted(source.rglob(name))
    if len(hits) != 1:
        raise SystemExit(f'expected exactly one {name} under {source}, found {len(hits)}')
    return hits[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--source', type=Path,
                    help='folder holding the Skate 2 DLC packages (searched recursively)')
    ap.add_argument('--root', type=Path, default=REPO / 'out' / 'build' / 'relwithdebinfo')
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--native-data', type=Path)
    ap.add_argument('--nav-baker', type=Path, default=HERE/'nsv_nav_bake.exe')
    ap.add_argument('--ocean-model', type=Path)
    ap.add_argument('--skate2-source', type=Path)
    args = ap.parse_args()
    dest = args.root / 'mods' / 'skate2' / FOLDER
    if args.uninstall:
        shutil.rmtree(dest, ignore_errors=True)
        print(f'removed {dest}')
        return
    if args.source is None or not args.source.is_dir():
        raise SystemExit('--source must name the folder with the Skate 2 DLC packages')
    if args.native_data is None or not args.nav_baker.is_file():
        raise SystemExit('--native-data and --nav-baker are required for DLC navigation')
    if args.skate2_source is None or not (args.skate2_source/'content/worldbam.big').is_file():
        raise SystemExit('--skate2-source must name the original Skate 2 data folder')

    # Stage next to the destination and swap in only once complete, so a
    # failed run never leaves a half-written install the runtime would accept.
    staging = dest.with_name(FOLDER + '.staging')
    if staging.exists():
        raise SystemExit(f'Unpublished stage already exists: {staging}; inspect it before retrying')
    staging.mkdir(parents=True)
    manifest = dict(version=1, packages={}, streams={})
    lighting = set()
    for package, streams in PACKAGES.items():
        path = find_package(args.source, package)
        with path.open('rb') as stream, \
                mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            world_archive.catalog(data)  # validates every section's indexed size
            entries = world_archive.entries(data)
            found = sorted({e.name[len(STREAM_PREFIX):].split('/', 1)[0]
                            for e in entries if e.name.startswith(STREAM_PREFIX)})
            if found != sorted(streams):
                raise SystemExit(f'{package}: streams {found}, expected {sorted(streams)}')
            for e in entries:
                if not e.name.startswith(STREAM_PREFIX):
                    continue
                relative = e.name[len(STREAM_PREFIX):]
                if '..' in relative.split('/'):
                    raise SystemExit(f'{package}: unsafe path {e.name}')
                out = staging / relative
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data[e.offset:e.offset + e.size])
        manifest['packages'][package] = dict(bytes=path.stat().st_size, sha256=sha256(path),
                                             streams=list(streams))
        for name in streams:
            folder = staging / name
            check_stream(folder, name)
            ids, names = inventory(folder)
            lighting.update(ids)
            manifest['streams'][name] = dict(
                package=package, files=sum(1 for p in folder.iterdir() if p.is_file()),
                bytes=sum(p.stat().st_size for p in folder.iterdir() if p.is_file()),
                textures=len(names), lighting_ids=len(ids))
            print(f'{name}: {manifest["streams"][name]["files"]} files, '
                  f'{len(names)} textures, {len(ids)} lighting pages')
    ocean = args.ocean_model or args.root/'mods/skate2/sky-water-v1/S2_DIST_Water.rx2'
    # Keep preparation fixtures in this unpublished stage, and remove only
    # this run's owned temporary directory before installing the streams.
    with tempfile.TemporaryDirectory(prefix='.navigation-',dir=staging) as scratch_name:
        scratch = Path(scratch_name)
        for name in LIVINGWORLD_STREAMS:
            manifest['streams'][name]['livingworld'] = prepare_livingworld(
                staging/name,args.skate2_source,ocean,args.nav_baker,scratch/name/'traffic')
            manifest['streams'][name]['navigation'] = prepare_navigation(
                staging/name,args.native_data,args.nav_baker,scratch/name,ocean,name,True)
    ids = sorted(lighting)
    (staging / 'lighting_texture_ids.txt').write_text(
        'BAM_LIGHTING_TEXTURE_IDS_V1\n' + str(len(ids)) + '\n' + ''.join(f'{x:016X}\n' for x in ids))
    (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (staging / MARKER).write_text('complete\n')
    backup = None
    if dest.exists():
        from datetime import datetime
        backup = args.root/'install-backups'/('dlc-maps-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(parents=True,exist_ok=False)
        dest.rename(backup/FOLDER)
    try:
        staging.rename(dest)
    except BaseException:
        if backup is not None and not dest.exists():
            (backup/FOLDER).rename(dest)
        raise
    print(f'installed {len(manifest["streams"])} streams, {len(ids)} lighting pages -> {dest}')


if __name__ == '__main__':
    main()
