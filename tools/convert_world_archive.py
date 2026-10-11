"""Build an indexed EB3 from BIG4 without renaming assets or changing payloads.

This prepares an archive for loader experiments. It does not register a world,
convert RenderWare objects, or assert that Skate 2 gameplay is compatible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

from world_archive import big_entries, catalog, eb_entries, path_hash, u32


def align(value, alignment=16):
    return (value + alignment - 1) // alignment * alignment


def convert(source: Path, destination: Path):
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError('output must be a new file, separate from the source')
    with source.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        original = big_entries(data)
        sections = catalog(data)
        indexed = sorted(original, key=lambda entry: path_hash(entry.name))
        directories = sorted({entry.name.rsplit('/', 1)[0] for entry in indexed})
        name_width = max(len(entry.name.rsplit('/', 1)[1].encode('ascii')) for entry in indexed) + 3
        directory_width = align(max(len(d.encode('ascii')) for d in directories) + 1, 4)
        if name_width > 255 or directory_width > 255 or len(directories) > 65535:
            raise ValueError('paths exceed the supported EB3 name geometry')
        names_at = align(48 + len(indexed) * 16 + 32)
        dirs_at = align(names_at + len(indexed) * name_width)
        body_at = u32(data, 12)
        if align(dirs_at + len(directories) * directory_width) > body_at:
            raise ValueError('EB index will not fit before source payload; relocation is unsupported')
        if len(data) > 0xffffffff or any(e.offset % 16 for e in indexed):
            raise ValueError('unsupported archive size or unaligned source entry')
        header = bytearray(body_at)
        metadata = name_width << 24 | directory_width << 16 | len(directories)
        struct.pack_into('>4s7I', header, 0, b'EB\0\3', len(indexed), 0x00100400,
                         names_at, body_at - names_at, metadata, 0, len(data))
        for index, entry in enumerate(indexed):
            directory, name = entry.name.rsplit('/', 1)
            struct.pack_into('>4I', header, 48 + index * 16,
                             entry.offset >> 4, 0, entry.size, path_hash(entry.name))
            pos = names_at + index * name_width
            struct.pack_into('>H', header, pos, directories.index(directory))
            encoded = name.encode('ascii')
            header[pos + 2:pos + 2 + len(encoded)] = encoded
        for index, directory in enumerate(directories):
            encoded = directory.encode('ascii')
            pos = dirs_at + index * directory_width
            header[pos:pos + len(encoded)] = encoded
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as output:
            output.write(header)
            digest = hashlib.sha256()
            for pos in range(body_at, len(data), 8 * 1024 * 1024):
                block = data[pos:pos + 8 * 1024 * 1024]
                digest.update(block)
                output.write(block)
        with destination.open('rb') as converted, mmap.mmap(converted.fileno(), 0, access=mmap.ACCESS_READ) as result:
            parsed = eb_entries(result)
            if {(e.name, e.offset, e.size) for e in parsed} != {(e.name, e.offset, e.size) for e in original}:
                raise ValueError('output index does not round-trip source entries')
            catalog(result)
            output_digest = hashlib.sha256()
            for pos in range(body_at, len(result), 8 * 1024 * 1024):
                output_digest.update(result[pos:pos + 8 * 1024 * 1024])
            if digest.digest() != output_digest.digest():
                raise ValueError('output payload changed')
        return dict(source=str(source.resolve()), output=str(destination.resolve()),
                    entries=len(indexed), directories=directories, sections=len(sections),
                    payload_offset=body_at, payload_sha256=digest.hexdigest(),
                    validation='index, section sizes and unchanged payload verified',
                    gameplay_compatibility='not established')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = json.dumps(convert(args.source, args.output), indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(result + '\n', encoding='utf-8')
    print(result)


if __name__ == '__main__':
    main()
