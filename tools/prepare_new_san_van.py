"""Build the complete mod from installer scratch and publish with rollback.

The host installer extracts both games with its shared XDVDFS reader and DLC
with the runtime's STFS reader. This entry point never reads an existing mod.
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

HERE = Path(__file__).resolve().parent
CONTENT = Path('0000000000000000/454108E6')
FOLDERS = ('world-support-gvr-v1', 'proxy-v1', 'sky-water-v1', 'jumbotron',
           'dlc-maps-v1', 'audio-v1', 'livingworld-v1')


def run(name, *args):
    print(f'Preparing {name}', flush=True)
    # Honor the host's read-only/signed resources policy in preparation children.
    bytecode = ['-B'] if sys.dont_write_bytecode else []
    subprocess.run([sys.executable, *bytecode, '-E', '-s', str(HERE / (name + '.py')),
                    *map(str, args)], check=True)


def publish(stage: Path, root: Path, content_root: Path):
    """Journal before every rename; recover a power loss before starting again."""
    journal = root / 'install-state/nsv-transaction.json'
    if journal.exists():
        raise ValueError('An unfinished publication must be recovered first')
    backup_name = 'nsv-' + uuid.uuid4().hex
    backup_dirs = set()
    pairs = [(stage / 'mods/skate2' / n, root / 'mods/skate2' / n)
             for n in (*FOLDERS, 'nsv_spawns.txt', 'install-manifest.json')]
    for branch in ('00000002', 'Headers/00000002'):
        pairs.extend((p, content_root / CONTENT / branch / p.name)
                     for p in sorted((stage / CONTENT / branch).iterdir()))
    # Completion record is always installed last.
    pairs.sort(key=lambda p: p[0].name == 'install-manifest.json')
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--native-data', type=Path, required=True)
    ap.add_argument('--dlc-source', type=Path, required=True)
    ap.add_argument('--stage', type=Path, required=True,
                    help='scratch root already containing extracted Skate 3 DLC and headers')
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--content-root', type=Path, required=True)
    ap.add_argument('--ffmpeg', type=Path, required=True)
    ap.add_argument('--nav-baker', type=Path, default=HERE / 'nsv_nav_bake.exe')
    args = ap.parse_args()
    root, stage, content_root = args.root.resolve(), args.stage.resolve(), args.content_root.resolve()
    if stage == root or stage.is_relative_to(root / 'mods'):
        raise ValueError('Preparation requires a separate scratch stage')
    recover(root, content_root)
    prepare(args.source, stage)
    mods = stage / 'mods/skate2'
    sky_water(args.source / 'big/miscboot.big', mods / 'sky-water-v1')
    print('Preparing pedestrian navigation from city collision...', flush=True)
    prepare_navigation(mods / 'world-support-gvr-v1', args.native_data,
                       args.nav_baker, stage / 'navigation-scratch')
    extract(args.source / 'content/worldproxy.big', mods / 'proxy-v1')
    run('build_skate2_dlc_maps', '--source', args.dlc_source, '--root', stage,
        '--native-data',args.native_data,'--nav-baker',args.nav_baker,'--skate2-source',args.source)
    run('build_skate2_audio', '--skate2', args.source, '--native-data', args.native_data,
        '--dlc-source', args.dlc_source, '--root', stage)
    run('build_skate2_livingworld', '--source', args.source, '--native-data', args.native_data,
        '--root', stage)
    run('build_jumbotron_video', '--movies', args.source / 'movies', '--root', stage,
        '--ffmpeg', args.ffmpeg)
    run('build_nsv_dlc', '--root', stage)
    files = {}
    for folder in (mods, stage / CONTENT):
        for p in sorted(folder.rglob('*')):
            if p.is_file():
                with p.open('rb') as f:
                    digest = hashlib.file_digest(f, 'sha256').hexdigest()
                files[p.relative_to(stage).as_posix()] = dict(bytes=p.stat().st_size, sha256=digest)
    (mods / 'install-manifest.json').write_text(json.dumps(dict(version=1, files=files), indent=2),
                                               encoding='utf-8')
    publish(stage, root, content_root)
    print('New San Van installation complete.', flush=True)


if __name__ == '__main__':
    main()
