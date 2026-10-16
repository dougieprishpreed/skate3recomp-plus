"""Fetch hash-locked, relocatable POSIX Python/NumPy installer dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile


def download(url, digest, destination):
    with urllib.request.urlopen(url) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError('Dependency checksum mismatch')
    destination.write_bytes(payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--platform', choices=('linux-amd64', 'macos-arm64'), required=True)
    ap.add_argument('--output', type=Path, required=True)
    a = ap.parse_args()
    out = a.output.resolve()
    if out.exists():
        raise SystemExit('Choose a new runtime folder')
    lock = json.loads(Path(__file__).with_name('runtime-lock.json').read_text())
    entry = lock[a.platform]
    out.mkdir(parents=True)
    archive = out / 'python.tar.gz'
    download(entry['python_url'], entry['python_sha256'], archive)
    with tarfile.open(archive) as t:
        # Python's data filter rejects traversal, external symlinks and devices.
        t.extractall(out, filter='data')
    wheel = out / urllib.parse.unquote(urllib.parse.urlparse(entry['numpy_url']).path.rsplit('/', 1)[1])
    download(entry['numpy_url'], entry['numpy_sha256'], wheel)
    python = out / 'python/bin/python3'
    subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--no-index', '--no-deps',
                    str(wheel)], check=True)
    subprocess.run([str(python), '-I', '-c',
                    'import numpy,ssl,ctypes,multiprocessing; print(numpy.__version__)'], check=True)
    (out / 'runtime-provenance.json').write_text(json.dumps(entry, indent=2) + '\n')
    print(out / 'python')


if __name__ == '__main__':
    main()
