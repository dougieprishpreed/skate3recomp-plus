"""Stage a private desktop preparation runtime from build dependencies.

Windows uses the build CPython/NumPy. POSIX requires --python-runtime with a
redistributable Python/NumPy tree. No game data is bundled.
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
        shutil.copy2(src, dest)


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
    ap.add_argument('--python-runtime', type=Path,
                    help='POSIX redistributable Python root, already containing NumPy')
    args = ap.parse_args()
    if sys.maxsize <= 2**32:
        raise SystemExit('Use 64-bit Python to package the installer')
    out = args.output.resolve()
    if sys.platform != 'win32':
        if not args.python_runtime or not (args.python_runtime / 'bin/python3').is_file():
            raise SystemExit('POSIX packaging requires a redistributable --python-runtime')
        runtime = args.python_runtime.resolve()
        if out.is_relative_to(runtime) or runtime.is_relative_to(out):
            raise SystemExit('Python source and output must be separate trees')
        subprocess.run([str(runtime / 'bin/python3'), '-I', '-c',
                        'import numpy, ssl, ctypes, multiprocessing'], check=True)
        stage_posix_python(runtime, out / 'python')
    else:
        stage_windows_python(out)
    suffix = '.exe' if sys.platform == 'win32' else ''
    copy(args.nav_baker, out / ('tools/nsv_nav_bake' + suffix))
    stage_shared_tools(out)
    for name in ('ffmpeg', 'ffprobe'):
        copy(args.ffmpeg.with_name(name + suffix), out / 'media' / (name + suffix))
    # Preserve the supplied FFmpeg distribution's license/notice files.
    supplied = set()
    for p in args.ffmpeg.parent.glob('*'):
        if p.is_file() and any(word in p.name.lower() for word in ('license', 'copying', 'notice')):
            copy(p, out / 'media' / p.name)
            supplied.add(p.name.lower())
    # Names that differ only by case collide on Windows, macOS and exFAT/NTFS drives
    # (LICENSE-ffmpeg.txt once overwrote the build's LICENSE-FFmpeg.txt there).
    for flag, name in (('-L', 'NOTICE-ffmpeg.txt'), ('-version', 'BUILD-ffmpeg.txt')):
        if name.lower() in supplied:
            raise ValueError(f'{name} would replace a supplied FFmpeg file')
        result = subprocess.run([str(args.ffmpeg), flag], capture_output=True, check=True)
        (out / 'media' / name).write_bytes(result.stdout + result.stderr)


def stage_posix_python(runtime, destination):
    for source in runtime.rglob('*'):
        relative = source.relative_to(runtime)
        if '__pycache__' in relative.parts or source.suffix == '.pyc':
            continue
        target = destination / relative
        if source.is_symlink():
            if not source.resolve().is_relative_to(runtime):
                raise ValueError('The private interpreter contains an external symlink')
            link = source.readlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() and target.readlink() == link:
                continue
            if target.exists() or target.is_symlink():
                raise ValueError('Conflicting interpreter link; use a new staging directory')
            target.symlink_to(link, target_is_directory=source.is_dir())
        elif source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            copy(source, target)


def stage_windows_python(out):
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


def stage_shared_tools(out):
    here = Path(__file__).resolve().parent
    copy(here.parent / 'third_party/recast/License.txt', out / 'tools/LICENSE-Recast.txt')
    copy(here.parent / 'assets/installer/title.png', out / 'title.png')
    todo = ['prepare_new_san_van', 'build_skate2_dlc_maps', 'build_skate2_audio',
            'build_skate2_livingworld', 'build_jumbotron_video', 'build_nsv_dlc',
            'extract_update_archive']  # the in-game updater's Linux unpacker
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
    print(f'Staged {len(done)} preparation modules and private runtime at {out}')


if __name__ == '__main__':
    main()
