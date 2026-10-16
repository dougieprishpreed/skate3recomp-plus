"""Create or verify the PRIVATE, minimal code-generation input bundle.

Never upload these files to the public source repo, Actions artifacts or caches.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

INPUTS = {
    'game/default.xex': (6615040, '1db39496585c521d17a2137804f42cf73ebed2b32cac166ec42dbf772f4dcf7f'),
    'game/data/webkit/EAWebkit.xex': (4923392, '0ee66b9558c888147f4ffdfd748cfc28950e80e46a97321da2de2afdb068523f'),
    'title-update/TU3': (1773568, 'a3fcff1e0b6059d307e9877063e50a99aca8ac41c93ca054de653f7bf16bca0e'),
}


def verify(data, name):
    size, digest = INPUTS[name]
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f'Unsupported or damaged build input: {name}')


def create(game, tu, output):
    if output.exists():
        raise ValueError('Choose a new private bundle; existing files are preserved')
    files = {name: tu if name.startswith('title-update/') else game / name.removeprefix('game/')
             for name in INPUTS}
    # Validate every input before creating anything.
    for name, path in files.items():
        verify(path.read_bytes(), name)
    output.mkdir(parents=True)
    with zipfile.ZipFile(output / 'codegen-inputs.zip', 'x', zipfile.ZIP_DEFLATED) as z:
        for name, path in files.items():
            z.write(path, name)
    manifest = {'format': 1, 'revision': 'skate3-retail-tu3-v1',
                'files': {n: {'bytes': v[0], 'sha256': v[1]} for n, v in INPUTS.items()}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print('Private bundle created: exactly three verified inputs, 13,312,000 uncompressed bytes.')


def extract(archive, output):
    if output.exists():
        raise ValueError('Choose an empty input destination')
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) != len(INPUTS) or set(z.namelist()) != set(INPUTS):
            raise ValueError('Unexpected entries in private build bundle')
        data = {}
        for item in z.infolist():
            if item.file_size != INPUTS[item.filename][0]:
                raise ValueError('Invalid input size')
            data[item.filename] = z.read(item)
            verify(data[item.filename], item.filename)
    for name, payload in data.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    print('Private build inputs verified. No game installation is used by CI.')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    make = sub.add_parser('create')
    make.add_argument('--game', type=Path, required=True)
    make.add_argument('--tu', type=Path, required=True)
    make.add_argument('--output', type=Path, required=True)
    unpack = sub.add_parser('extract')
    unpack.add_argument('--archive', type=Path, required=True)
    unpack.add_argument('--output', type=Path, required=True)
    a = ap.parse_args()
    if a.command == 'create':
        create(a.game.resolve(), a.tu.resolve(), a.output.resolve())
    else:
        extract(a.archive.resolve(), a.output.resolve())


if __name__ == '__main__':
    main()
