"""Build the New San Van mod (all of it, or selected components) from installer scratch and
publish with rollback.

The host installer extracts both games with its shared XDVDFS reader and DLC
with the runtime's STFS reader. A full preparation never reads an existing mod.
A component update (--components) builds only the requested components and the
ones derived from them (tools/nsv_components.py); inputs from components that
are not being rebuilt are read from the installed copy, and only the rebuilt
components are published.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from prepare_skate2_world import prepare
from skate2_navigation import prepare_navigation
from extract_world_loose import extract
from build_skate2_sky_water import build as sky_water
import nsv_components

HERE = Path(__file__).resolve().parent
CONTENT = Path(nsv_components.CONTENT)
FOLDERS = ('world-support-gvr-v1', 'proxy-v1', 'sky-water-v1', 'jumbotron',
           'dlc-maps-v1', 'audio-v1', 'livingworld-v1')
MANIFEST = 'install-manifest.json'


def run(name, *args):
    print(f'Preparing {name}', flush=True)
    # Honor the host's read-only/signed resources policy in preparation children.
    bytecode = ['-B'] if sys.dont_write_bytecode else []
    subprocess.run([sys.executable, *bytecode, '-E', '-s', str(HERE / (name + '.py')),
                    *map(str, args)], check=True)


def publish_pairs(stage: Path, root: Path, content_root: Path, components):
    """(staged, installed) pairs for a full install or for the given components."""
    if components is None:
        pairs = [(stage / 'mods/skate2' / n, root / 'mods/skate2' / n)
                 for n in (*FOLDERS, 'nsv_spawns.txt', MANIFEST)]
        for branch in ('00000002', 'Headers/00000002'):
            pairs.extend((p, content_root / CONTENT / branch / p.name)
                         for p in sorted((stage / CONTENT / branch).iterdir()))
        return pairs
    pairs = []
    for cid in components:
        for out in nsv_components.BY_ID[cid].outputs:
            pairs.append((nsv_components.resolve(out, stage / 'mods/skate2', stage / CONTENT),
                          nsv_components.resolve(out, root / 'mods/skate2', content_root / CONTENT)))
    pairs.append((stage / 'mods/skate2' / MANIFEST, root / 'mods/skate2' / MANIFEST))
    return pairs


def publish(stage: Path, root: Path, content_root: Path, components=None):
    """Journal before every rename; recover a power loss before starting again."""
    journal = root / 'install-state/nsv-transaction.json'
    if journal.exists():
        raise ValueError('An unfinished publication must be recovered first')
    backup_name = 'nsv-' + uuid.uuid4().hex
    backup_dirs = set()
    pairs = publish_pairs(stage, root, content_root, components)
    # Completion record is always installed last.
    pairs.sort(key=lambda p: p[0].name == MANIFEST)
    operations = []
    journal.parent.mkdir(parents=True, exist_ok=True)

    def save():
        tmp = journal.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(operations, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(journal)

    try:
        for number, (src, dest) in enumerate(pairs):
            if not src.exists():
                raise ValueError(f'Preparation did not produce {src.name}')
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Keep each backup on its destination volume, including custom
            # user content roots. Backup renames must remain atomic.
            owner = root if dest.is_relative_to(root / 'mods/skate2') else content_root
            backup = owner / 'install-backups' / backup_name
            old = backup / str(number)
            backup_dirs.add(backup)
            op = dict(source=str(src), destination=str(dest), backup=str(old),
                      had_old=dest.exists())
            operations.append(op)
            save()
            if op['had_old']:
                old.parent.mkdir(parents=True, exist_ok=True)
                dest.rename(old)
            shutil.move(str(src), str(dest))
    except BaseException:
        recover(root, content_root)
        raise
    journal.unlink()
    for backup in backup_dirs:
        if backup.exists():
            (backup / 'manifest.json').write_text(json.dumps(operations, indent=2), encoding='utf-8')


def recover(root: Path, content_root: Path):
    journal = root / 'install-state/nsv-transaction.json'
    if not journal.exists():
        return
    ops = json.loads(journal.read_text(encoding='utf-8'))
    # Never use an on-disk journal to operate outside the installation.
    for op in ops:
        dest, backup = Path(op['destination']).resolve(), Path(op['backup']).resolve()
        if not (dest.is_relative_to(root / 'mods/skate2') or
                dest.is_relative_to(content_root / CONTENT)):
            raise ValueError('Unsafe transaction destination')
        if not (backup.is_relative_to(root / 'install-backups') or
                backup.is_relative_to(content_root / 'install-backups')):
            raise ValueError('Unsafe transaction backup')
    for op in reversed(ops):
        dest, backup = Path(op['destination']), Path(op['backup'])
        if backup.exists() or not op['had_old']:
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink(missing_ok=True)
        if backup.exists():
            backup.rename(dest)
    journal.unlink()


def staged_files(stage: Path, components):
    """sha256/size of every staged file this run will publish (manifest keys are stage-relative)."""
    if components is None:
        roots = [stage / 'mods/skate2', stage / CONTENT]
    else:
        roots = [nsv_components.resolve(out, stage / 'mods/skate2', stage / CONTENT)
                 for cid in components for out in nsv_components.BY_ID[cid].outputs]
    files = {}
    for top in roots:
        for p in ([top] if top.is_file() else sorted(top.rglob('*'))):
            if p.is_file() and p.name != MANIFEST:
                with p.open('rb') as f:
                    digest = hashlib.file_digest(f, 'sha256').hexdigest()
                files[p.relative_to(stage).as_posix()] = dict(bytes=p.stat().st_size, sha256=digest)
    return files


def owned(key: str, components) -> bool:
    """Is manifest entry `key` (stage-relative) one of the given components' outputs?"""
    for cid in components:
        for out in nsv_components.BY_ID[cid].outputs:
            prefix = (CONTENT.as_posix() + '/' + out[len('CONTENT/'):] if out.startswith('CONTENT/')
                      else 'mods/skate2/' + out)
            if key == prefix or key.startswith(prefix + '/'):
                return True
    return False


def write_manifest(stage: Path, root: Path, components):
    files = staged_files(stage, components)
    installed = root / 'mods/skate2' / MANIFEST
    if components is not None and installed.is_file():
        previous = json.loads(installed.read_text(encoding='utf-8')).get('files', {})
        files = {**{k: v for k, v in previous.items() if not owned(k, components)}, **files}
    versions = {c.id: list(c.markers) for c in nsv_components.COMPONENTS}
    manifest = dict(version=2, components=versions, files=dict(sorted(files.items())))
    path = stage / 'mods/skate2' / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, help='extracted Skate 2 data directory')
    ap.add_argument('--native-data', type=Path, required=True)
    ap.add_argument('--dlc-source', type=Path, help='extracted Skate 2 DLC')
    ap.add_argument('--stage', type=Path, required=True,
                    help='scratch root already containing extracted Skate 3 DLC and headers')
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--content-root', type=Path, required=True)
    ap.add_argument('--ffmpeg', type=Path)
    ap.add_argument('--nav-baker', type=Path, default=HERE / 'nsv_nav_bake.exe')
    ap.add_argument('--components', default='all',
                    help='comma-separated component ids to update (tools/nsv_components.py), or all')
    args = ap.parse_args()
    root, stage, content_root = args.root.resolve(), args.stage.resolve(), args.content_root.resolve()
    if stage == root or stage.is_relative_to(root / 'mods'):
        raise ValueError('Preparation requires a separate scratch stage')
    selected = nsv_components.parse(args.components)
    full = args.components.strip() in ('', 'all')
    need = nsv_components.needs(selected)
    if need['skate2'] and args.source is None:
        raise ValueError('These updates need the Skate 2 game files')
    if need['skate2_dlc'] and args.dlc_source is None:
        raise ValueError('These updates need the Skate 2 DLC')
    if 'jumbotron' in selected and args.ffmpeg is None:
        raise ValueError('The jumbotron clip needs the bundled media tool')
    recover(root, content_root)
    if not full:
        print('Updating New San Van: ' + ', '.join(selected), flush=True)
    mods = stage / 'mods/skate2'
    installed = root / 'mods/skate2'
    # Inputs a selected component reads from another component: the rebuilt
    # copy in the stage, or the installed one.
    def input_of(cid, path):
        return (mods if cid in selected else installed) / path
    if 'city' in selected:
        prepare(args.source, stage)
    if 'sky' in selected:
        sky_water(args.source / 'big/miscboot.big', mods / 'sky-water-v1')
    ocean = input_of('sky', 'sky-water-v1/S2_DIST_Water.rx2')
    if 'city' in selected:
        print('Preparing pedestrian navigation from city collision...', flush=True)
        prepare_navigation(mods / 'world-support-gvr-v1', args.native_data,
                           args.nav_baker, stage / 'navigation-scratch', ocean)
    if 'skyline' in selected:
        extract(args.source / 'content/worldproxy.big', mods / 'proxy-v1')
    if 'dlc_maps' in selected:
        run('build_skate2_dlc_maps', '--source', args.dlc_source, '--root', stage,
            '--native-data', args.native_data, '--nav-baker', args.nav_baker,
            '--skate2-source', args.source, '--ocean-model', ocean)
    if 'audio' in selected:
        run('build_skate2_audio', '--skate2', args.source, '--native-data', args.native_data,
            '--dlc-source', args.dlc_source, '--root', stage)
    if 'livingworld' in selected:
        run('build_skate2_livingworld', '--source', args.source, '--native-data', args.native_data,
            '--root', stage, '--bam-stream', input_of('city', 'world-support-gvr-v1'))
    if 'jumbotron' in selected:
        run('build_jumbotron_video', '--movies', args.source / 'movies', '--root', stage,
            '--ffmpeg', args.ffmpeg)
    if 'menu' in selected:
        run('build_nsv_dlc', '--root', stage,
            '--audio-root', stage if 'audio' in selected else root,
            '--livingworld-root', stage if 'livingworld' in selected else root)
    for cid in selected:
        missing = nsv_components.missing_markers(nsv_components.BY_ID[cid], mods, stage / CONTENT)
        if missing:
            raise ValueError(f'{cid} preparation did not complete: ' + ', '.join(missing))
    write_manifest(stage, root, None if full else selected)
    publish(stage, root, content_root, None if full else selected)
    print('New San Van update complete.' if not full else 'New San Van installation complete.',
          flush=True)


if __name__ == '__main__':
    main()
