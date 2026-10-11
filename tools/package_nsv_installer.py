"""Stage a self-contained Windows preparation runtime from build dependencies.

Run with the build Python (64-bit CPython with NumPy). No game data is bundled.
The resulting installer/ directory ships beside skate3.exe and rexruntime.dll.
"""
import argparse
import ast
import filecmp
from pathlib import Path
import shutil
import sys
import subprocess

import numpy


def copy(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or not filecmp.cmp(src, dest, shallow=False):
        shutil.copyfile(src, dest)


def tree(src, dest):
    for p in src.rglob('*'):
        rel = p.relative_to(src)
        if p.is_file() and not {'__pycache__', 'tests', 'test', 'idlelib', 'tkinter'} & set(rel.parts):
            copy(p, dest / rel)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--ffmpeg', type=Path, required=True)
    ap.add_argument('--nav-baker', type=Path, required=True)
    args = ap.parse_args()
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise SystemExit('Use 64-bit Windows CPython to package the Windows installer')
    out = args.output.resolve()
    home = Path(sys.base_prefix)
    for pattern in ('python.exe', 'python*.dll', 'vcruntime*.dll', 'LICENSE.txt'):
        for p in home.glob(pattern):
            copy(p, out / 'python' / p.name)
    for p in (home / 'Lib').iterdir():
        if p.name in ('site-packages', '__pycache__', 'test', 'idlelib', 'tkinter', 'ensurepip'):
            continue
        if p.is_dir():
            tree(p, out / 'python/Lib' / p.name)
        else:
            copy(p, out / 'python/Lib' / p.name)
    tree(home / 'DLLs', out / 'python/DLLs')
    site = Path(numpy.__file__).parent.parent
    for pattern in ('numpy', 'numpy.libs', 'numpy-*.dist-info'):
        for p in site.glob(pattern):
            tree(p, out / 'python/Lib/site-packages' / p.name)
    # Pin import paths; ignore machine Python installations and environment.
    (out / 'python' / f'python{sys.version_info.major}{sys.version_info.minor}._pth').write_bytes(
        b'Lib\nDLLs\nLib/site-packages\n../tools\n')
    copy(args.nav_baker, out / 'tools/nsv_nav_bake.exe')
    here = Path(__file__).resolve().parent
    copy(here.parent / 'third_party/recast/License.txt', out / 'tools/LICENSE-Recast.txt')
    copy(here.parent / 'assets/installer/title.png', out / 'title.png')
    todo = ['prepare_new_san_van', 'build_skate2_dlc_maps', 'build_skate2_audio',
            'build_skate2_livingworld', 'build_jumbotron_video', 'build_nsv_dlc']
    done = set()
    while todo:
        name = todo.pop()
        if name in done:
            continue
        done.add(name)
        p = here / (name + '.py')
        copy(p, out / 'tools' / p.name)
        for node in ast.walk(ast.parse(p.read_text(encoding='utf-8'))):
            names = ([node.module] if isinstance(node, ast.ImportFrom) else
                     [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            for module in names:
                if module and (here / (module + '.py')).is_file():
                    todo.append(module)
    for name in ('nsv_spawns.json', 'nsv_locators.json', 'skate2_livingworld_names.json'):
        copy(here / name, out / 'tools' / name)
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        copy(args.ffmpeg.with_name(name), out / 'media' / name)
    # Preserve the supplied FFmpeg distribution's license/notice files.
    for p in args.ffmpeg.parent.glob('*'):
        if p.is_file() and any(word in p.name.lower() for word in ('license', 'copying', 'notice')):
            copy(p, out / 'media' / p.name)
    for flag, name in (('-L', 'LICENSE-ffmpeg.txt'), ('-version', 'BUILD-ffmpeg.txt')):
        info = subprocess.run([str(args.ffmpeg), flag], capture_output=True, check=True)
        (out / 'media' / name).write_bytes(info.stdout + info.stderr)
    print(f'Staged {len(done)} preparation modules and private runtime at {out}')


if __name__ == '__main__':
    main()
