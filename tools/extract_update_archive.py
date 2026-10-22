"""Unpack a Skate 3 Recompiled+ release ZIP for the in-game updater (Linux without unzip).

Restores the Unix modes and symbolic links the packager stored, and refuses any
entry that would land outside the destination.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import zipfile


def extract(archive: Path, destination: Path) -> int:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (destination / info.filename).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f'unsafe path in archive: {info.filename}')
            mode = info.external_attr >> 16
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link = z.read(info).decode('utf-8')
                resolved = (target.parent / link).resolve()
                if destination not in resolved.parents and resolved != destination:
                    raise ValueError(f'unsafe link in archive: {info.filename}')
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(link, target)
            else:
                with z.open(info) as src, open(target, 'wb') as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                if mode & 0o777:
                    os.chmod(target, mode & 0o777)
            count += 1
    return count


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: extract_update_archive.py <archive.zip> <destination>')
    print(f'extracted {extract(Path(sys.argv[1]), Path(sys.argv[2]))} entries')


if __name__ == '__main__':
    main()
