"""Stage the three runner packages as GitHub release assets the in-game updater recognises.

  python tools/stage_release.py --tag v0.2a --artifacts <folder> [--output out/release/v0.2a]

<folder> holds the downloaded runner artifacts: Skate3Recomp-Plus-<platform>.zip
(+ .zip.sha256) and <platform>-build.json for win-amd64, linux-amd64 and
macos-arm64. Each package is verified against its runner checksum and build
record, then copied byte-for-byte as
  Skate3Recomp-Plus-<tag>-Windows-x64.zip / -Linux-x64.zip / -macOS-arm64.zip
with .sha256 files and SHA256SUMS.txt. Upload those three ZIPs to a GitHub
release whose tag is <tag> (not a pre-release): players running an older
version are then offered the update at startup.

The tag's letter is the patch number of the build's numeric version
(v0.2 = 0.2.0, v0.2a = 0.2.1); the runner builds must carry that version and a
release (non -dev) display name, so set SKATE3_FULL_VERSION in CMakeLists.txt
first. Nothing is uploaded or published by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {'win-amd64': 'Windows-x64', 'linux-amd64': 'Linux-x64', 'macos-arm64': 'macOS-arm64'}


def tag_version(tag: str) -> str:
    m = re.fullmatch(r'v(\d+)\.(\d+)([a-z]?)', tag)
    if not m:
        raise SystemExit(f'Tag must look like v0.2 or v0.2a, not {tag!r}')
    patch = ord(m[3]) - ord('a') + 1 if m[3] else 0
    return f'{int(m[1])}.{int(m[2])}.{patch}'


def sha256(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--tag', required=True)
    ap.add_argument('--artifacts', type=Path, required=True)
    ap.add_argument('--output', type=Path)
    a = ap.parse_args()
    numeric = tag_version(a.tag)
    output = a.output or ROOT / 'out/release' / a.tag
    if output.exists():
        raise SystemExit(f'{output} exists; existing releases are preserved')
    staged = []
    for platform, label in PLATFORMS.items():
        package = a.artifacts / f'Skate3Recomp-Plus-{platform}.zip'
        record = a.artifacts / f'{platform}-build.json'
        checksum = package.with_suffix('.zip.sha256')
        for p in (package, record, checksum):
            if not p.is_file():
                raise SystemExit(f'Missing {p}')
        digest = sha256(package)
        if checksum.read_text().split()[0] != digest:
            raise SystemExit(f'{package.name} does not match its runner checksum')
        stamp = json.loads(record.read_text(encoding='utf8'))['build_stamp']
        built_numeric = re.search(r'SKATE3_VERSION_NUMERIC "([^"]+)"', stamp)[1]
        built_display = re.search(r'SKATE3_VERSION_STRING "([^"]+)"', stamp)[1]
        if built_numeric != numeric:
            raise SystemExit(f'{platform} was built as {built_numeric}; {a.tag} needs {numeric}')
        if built_display.endswith('-dev'):
            raise SystemExit(f'{platform} is a development build ({built_display}); set the release '
                             'SKATE3_FULL_VERSION and rebuild')
        with zipfile.ZipFile(package) as z:
            tops = {n.split('/')[0] for n in z.namelist()}
            if tops != {f'Skate3Recomp-Plus-{platform}'} or z.testzip() is not None:
                raise SystemExit(f'{package.name} has an unexpected layout')
        staged.append((package, f'Skate3Recomp-Plus-{a.tag}-{label}.zip', digest, record))
    output.mkdir(parents=True)
    sums = []
    for package, name, digest, record in staged:
        shutil.copyfile(package, output / name)
        if sha256(output / name) != digest:
            raise SystemExit(f'Copy of {name} changed')
        (output / (name + '.sha256')).write_text(f'{digest}  {name}\n')
        shutil.copyfile(record, output / record.name)
        sums.append(f'{digest}  {name}')
    (output / 'SHA256SUMS.txt').write_text('\n'.join(sums) + '\n')
    print(f'Staged {a.tag} ({numeric}) in {output}:')
    for line in sums:
        print('  ' + line)


if __name__ == '__main__':
    main()
