"""Create a clean, portable Windows release; never copy game data.

settings.toml: --settings copies a development settings file, with the World
gameplay settings and the sky choice reset to their defaults; without it the
checked-in release defaults are used.
"""
import argparse
from pathlib import Path
import shutil
import tomllib
from debug_settings_template import defaults

ROOT = Path(__file__).resolve().parent.parent

# Every release starts with these at their defaults whatever the copied file
# holds: the World menu's gameplay settings (skate3_cheats.cpp), and the sky,
# which setup asks for.
RESET_PREFIXES = ('skate3_cheat_',)
RESET_NAMES = ('skate3_skate2_sky', 'skate3_skate2_night')


def copied_settings(path):
    source = defaults()
    original = path.read_text(encoding='utf-8')
    lines, reset = [], {}
    for line in original.splitlines():
        name = line.split('=', 1)[0].strip()
        if '=' in line and not line.lstrip().startswith('#') and (
                name.startswith(RESET_PREFIXES) or name in RESET_NAMES):
            if name not in source:
                raise SystemExit(f'No source default for {name}')
            line = f'{name} = {source[name]}'
            reset[name] = source[name]
        lines.append(line)
    text = '\n'.join(lines) + '\n'
    before, after = tomllib.loads(original), tomllib.loads(text)
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    if not changed <= reset.keys():
        raise SystemExit(f'Unexpected settings changes: {sorted(changed - reset.keys())}')
    return text, reset


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--settings', type=Path,
                    help='settings.toml to ship (World gameplay settings and sky reset)')
    args = ap.parse_args()
    build, output = args.build.resolve(), args.output.resolve()
    if output.exists():
        raise SystemExit('Choose a new release folder; existing releases are not overwritten')
    required = [build / 'skate3.exe', build / 'installer/python/python.exe',
                build / 'installer/tools/prepare_new_san_van.py',
                build / 'installer/tools/nsv_nav_bake.exe',
                build / 'installer/media/ffmpeg.exe', build / 'installer/media/ffprobe.exe']
    if any(not p.is_file() for p in required):
        raise SystemExit('Build and stage the installer before packaging')
    if args.settings:
        settings, reset = copied_settings(args.settings.resolve())
    else:
        settings, reset = copied_settings(ROOT / 'config/release-defaults.toml')
    output.mkdir(parents=True)
    shutil.copyfile(build / 'skate3.exe', output / 'skate3.exe')
    for p in build.glob('*.dll'):
        shutil.copyfile(p, output / p.name)
    shutil.copytree(build / 'installer', output / 'installer',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (output / 'portable.txt').write_bytes(b'')
    (output / 'settings.toml').write_bytes(settings.replace('\n', '\r\n').encode())
    shutil.copyfile(ROOT / 'packaging/README.txt', output / 'README.txt')
    # Include upstream notices for the SDK and its embedded dependencies.
    licenses = output / 'licenses'
    licenses.mkdir()
    sdk = ROOT / 'third_party/rexglue-sdk'
    for directory in (sdk, *(p for p in (sdk / 'thirdparty').iterdir() if p.is_dir())):
        label = 'ReXGlue' if directory == sdk else directory.name
        for p in directory.iterdir():
            if p.is_file() and any(word in p.name.lower() for word in ('license', 'copying', 'notice')):
                shutil.copyfile(p, licenses / (label + '-' + p.name))
    if args.settings:
        print(f'settings.toml from {args.settings}; defaults restored: ' +
              ', '.join(f'{k} = {v}' for k, v in sorted(reset.items())))
    print(output)


if __name__ == '__main__':
    main()
