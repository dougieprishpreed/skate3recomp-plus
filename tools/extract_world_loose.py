"""Extract an indexed world into a new directory, verifying every payload."""
import argparse
import hashlib
import json
import mmap
from pathlib import Path, PurePosixPath
from world_archive import entries, catalog

def extract(source, destination):
    if destination.exists():
        raise ValueError('destination must be a new directory')
    with source.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        records = entries(data)
        catalog(data)
        paths = [PurePosixPath(e.name) for e in records]
        if any(p.is_absolute() or '..' in p.parts or ':' in str(p) for p in paths):
            raise ValueError('unsafe archive path')
        if len({str(p).lower() for p in paths}) != len(paths):
            raise ValueError('duplicate output path')
        destination.mkdir(parents=True)
        report = []
        for e, p in zip(records, paths):
            output = destination.joinpath(*p.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            payload = data[e.offset:e.offset + e.size]
            with output.open('xb') as out:
                out.write(payload)
            digest = hashlib.sha256(payload).hexdigest()
            if hashlib.sha256(output.read_bytes()).hexdigest() != digest:
                raise ValueError(f'payload verification failed: {p}')
            report.append(dict(path=str(p), size=e.size, sha256=digest))
        (destination / 'manifest.json').write_text(json.dumps(report, indent=2))
        print(f'Extracted and verified {len(report)} files into {destination}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    extract(args.source, args.destination)
