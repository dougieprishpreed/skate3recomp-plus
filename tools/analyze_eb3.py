#!/usr/bin/env python3
"""Forensic EB3/BIG4 layout analysis; no conversion is performed."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
from collections import Counter
from pathlib import Path

MARKERS = (b"MMAP", b"ATOC", b"SFIL")

def u32(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]

def marker_offsets(b: bytes) -> dict[str, list[int]]:
    return {m.decode(): [x.start() for x in re.finditer(re.escape(m), b)] for m in MARKERS}

def ascii_runs(b: bytes, start: int, end: int, minimum: int = 3) -> list[tuple[int, str]]:
    out = []
    begin = None
    for i in range(start, end):
        c = b[i]
        if 32 <= c < 127:
            if begin is None:
                begin = i
        elif begin is not None:
            if i - begin >= minimum:
                out.append((begin, b[begin:i].decode("ascii", "replace")))
            begin = None
    if begin is not None and end - begin >= minimum:
        out.append((begin, b[begin:end].decode("ascii", "replace")))
    return out

def parse_eb3(path: Path) -> dict:
    b = path.read_bytes()
    if len(b) < 32 or b[:4] != b"EB\0\3":
        raise ValueError(f"not EB3: {path}")
    fields = [u32(b, o) for o in range(4, 32, 4)]
    count, family, table_off, table_size, metadata, reserved, file_size = fields
    markers = marker_offsets(b)
    table_end = table_off + table_size
    runs = ascii_runs(b, table_off, min(table_end, len(b)))
    names = [s for _, s in runs if "/" not in s or s.startswith("data/")]
    return {
        "path": str(path),
        "size": len(b),
        "sha256": hashlib.sha256(b).hexdigest(),
        "header_fields_be": fields,
        "header": {
            "record_count": count,
            "family_or_alignment": family,
            "table_offset": table_off,
            "table_size": table_size,
            "table_end": table_end,
            "metadata": metadata,
            "reserved": reserved,
            "declared_file_size": file_size,
            "declared_size_matches": file_size == len(b),
        },
        "markers": markers,
        "marker_first": {k: (v[0] if v else None) for k, v in markers.items()},
        "table_runs": runs,
        "table_string_count": len(runs),
        "table_path_strings": [s for _, s in runs if s.startswith("data/")],
        "table_suffixes": dict(Counter(Path(s).suffix.lower() for _, s in runs)),
        "body_offset_candidates": sorted(set([table_end] + [x for values in markers.values() for x in values if x > table_end]))[:20],
        "prefix_nonzero": sum(x != 0 for x in b[32:table_off]),
        "table_nonzero": sum(x != 0 for x in b[table_off:table_end]),
        "body_sha256": hashlib.sha256(b[table_end:]).hexdigest(),
    }

def parse_big(path: Path) -> dict:
    from inspect_map_archives import read_big_entries
    magic, count, table_end, entries = read_big_entries(path)
    b = path.read_bytes()
    return {"path": str(path), "size": len(b), "sha256": hashlib.sha256(b).hexdigest(),
            "entry_count": count, "table_end": table_end, "parsed_entries": len(entries),
            "entries": entries, "body_sha256": hashlib.sha256(b[table_end:]).hexdigest()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    result = {}
    for path in args.paths:
        result[str(path)] = parse_big(path) if path.read_bytes()[:4] in (b"BIG4", b"BIGF") else parse_eb3(path)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(args.output)
    else:
        print(text)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
