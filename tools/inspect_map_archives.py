#!/usr/bin/env python3
"""Inspect Skate 2 BIG/BH and Skate 3 EB world archives.

This is deliberately an inspection tool first. It refuses to claim conversion
support until the EB section relationships are understood and validated against
known-good Skate 3 archives.
"""
from __future__ import annotations

import argparse
import json
import mmap
import struct
from collections import Counter
from pathlib import Path
from typing import BinaryIO, Iterable

CHUNK = 8 * 1024 * 1024
EB_MARKERS = (b"MMAP", b"ATOC", b"SFIL", b"EB\x00\x03")
PATH_EXTENSIONS = (".xsf", ".xmm", ".xst", ".xsm", ".xss", ".loc", ".xml", ".txt")


def be_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def scan_markers(path: Path, markers: Iterable[bytes]) -> dict[str, list[int]]:
    found = {m.decode("latin1"): [] for m in markers}
    overlap = max(len(m) for m in markers) - 1
    base = 0
    tail = b""
    with path.open("rb") as stream:
        while True:
            block = stream.read(CHUNK)
            if not block:
                break
            data = tail + block
            data_base = base - len(tail)
            for marker in markers:
                pos = 0
                while True:
                    hit = data.find(marker, pos)
                    if hit < 0:
                        break
                    found[marker.decode("latin1")].append(data_base + hit)
                    pos = hit + 1
            tail = data[-overlap:] if overlap else b""
            base += len(block)
    return found


def extract_ascii_strings(path: Path, minimum: int = 5, limit: int = 5000) -> list[str]:
    values: list[str] = []
    current = bytearray()
    with path.open("rb") as stream:
        while True:
            block = stream.read(CHUNK)
            if not block:
                break
            for value in block:
                if 32 <= value < 127:
                    current.append(value)
                else:
                    if len(current) >= minimum:
                        values.append(current.decode("ascii", "replace"))
                        if len(values) >= limit:
                            return values
                    current.clear()
    if len(current) >= minimum and len(values) < limit:
        values.append(current.decode("ascii", "replace"))
    return values


def parse_big(path: Path) -> dict:
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic not in (b"BIG4", b"BIGF"):
        return {"path": str(path), "format": "unknown", "magic": magic.decode("latin1")}
    magic, count, table_end, entries = read_big_entries(path)
    return {"path": str(path), "size": path.stat().st_size, "magic": magic.decode(),
            "format": "BIG4/BIGF", "entry_count": count, "table_end": table_end,
            "entries": entries, "invalid_entries": 0,
            "interesting_entries": [e for e in entries if any(s in e["name"].lower() for s in ("world", "bam", "proxy", "dist_"))]}


def parse_bh(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(24)
    result = {
        "path": str(path),
        "size": path.stat().st_size,
        "magic": header[:4].decode("latin1", "replace"),
        "header_fields_be": [be_u32(header, o) for o in range(4, 24, 4)]
        if len(header) >= 24
        else [],
    }
    if len(header) >= 24 and result["magic"] == "Viv4":
        result["entry_count_candidate"] = be_u32(header, 8)
        result["table_size_candidate"] = be_u32(header, 12)
        result["data_offset_candidate"] = be_u32(header, 16)
    return result


def parse_eb(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(32)
    magic = header[:4].decode("latin1", "replace")
    result = {
        "path": str(path),
        "size": path.stat().st_size,
        "magic": magic,
        "format": "EB3" if header[:4] == b"EB\x00\x03" else "unknown",
        "header_fields_be": [be_u32(header, o) for o in range(4, 32, 4)]
        if len(header) >= 32
        else [],
    }
    if result["format"] != "EB3":
        return result
    markers = scan_markers(path, EB_MARKERS)
    result["marker_offsets"] = markers
    strings = extract_ascii_strings(path)
    result["path_like_strings"] = [
        s for s in strings if "/" in s or any(s.lower().endswith(ext) for ext in PATH_EXTENSIONS)
    ][:1000]
    result["asset_extension_counts"] = dict(
        sorted(
            Counter(
                Path(s.replace("\\", "/")).suffix.lower()
                for s in strings
                if any(s.lower().endswith(ext) for ext in PATH_EXTENSIONS)
            ).items()
        )
    )
    return result


def inspect_tree(root: Path) -> dict:
    files = [p for p in root.rglob("*") if p.is_file()]
    return {
        "root": str(root),
        "file_count": len(files),
        "total_bytes": sum(p.stat().st_size for p in files),
        "big": [parse_big(p) for p in sorted(files) if p.suffix.lower() == ".big"],
        "bh": [parse_bh(p) for p in sorted(files) if p.suffix.lower() == ".bh"],
        "eb": [parse_eb(p) for p in sorted(files) if p.suffix.lower() == ".big"],
    }


def read_big_entries(path: Path) -> tuple[bytes, int, int, list[dict]]:
    from world_archive import big_entries
    from dataclasses import asdict
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        records = [dict(asdict(entry), index=i) for i, entry in enumerate(big_entries(data))]
        return data[:4], be_u32(data, 8), be_u32(data, 12), records


def converted_outer_name(name: str, map_name: str) -> str:
    normalized = name.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    for old in ("BAM", "PROXY"):
        if basename.startswith(old + "_"):
            basename = map_name + basename[len(old):]
            break
    return basename


def build_eb_from_big(
    source: Path,
    template: Path,
    destination: Path,
    map_name: str,
    asset_namespace: str | None = None,
    path_name: str | None = None,
    header_name_offset: int | None = None,
    header_metadata: int | None = None,
    target_slot_width: int | None = None,
) -> dict:
    raise ValueError(
        "Legacy outer repacking is disabled: it emitted a zero EB3 index. "
        "Use convert_world_archive.py with original BIG4 input; asset renaming is unsupported."
    )


def convert_map(
    skate2_root: Path,
    skate3_template: Path,
    output: Path,
    map_name: str,
    replace_downtown: bool = False,
    replace_blackbox: bool = False,
    target_template: Path | None = None,
) -> dict:
    content = skate2_root
    output.mkdir(parents=True, exist_ok=True)
    converted = []
    if replace_blackbox:
        map_name = "DIST_BlackBoxPark"
        target = target_template or skate3_template.parent / "worldDIST_BlackBoxPark.big"
        jobs = [
            (
                "worldbam.big",
                "worldDIST_BlackBoxPark.big",
                "DIST_BlackBoxPark",
                "DIST_BlackBoxPark",
                target,
                464,
                489422849,
                29,
            ),
        ]
    elif replace_downtown:
        map_name = "DIST_DownTown"
        jobs = [
            (
                "worldbam.big",
                "worldDIST_DownTown.big",
                "DIST_DownTown",
                "DIST_DownTown",
                skate3_template,
                11088,
                455606273,
                27,
            ),
            (
                "worldproxy.big",
                "proxyDowntown_100_Proxy.big",
                "Downtown_100_Proxy",
                "Downtown_100_Proxy",
                skate3_template,
                4224,
                556597249,
                33,
            ),
        ]
    else:
        jobs = [
            ("worldbam.big", f"world{map_name}.big", map_name, map_name, skate3_template, None, None, None),
            ("worldproxy.big", f"world{map_name}_Proxy.big", map_name, map_name, skate3_template, None, None, None),
        ]
    for source_name, destination_name, namespace, path_name, template, name_offset, metadata, slot_width in jobs:
        source = content / source_name
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = output / destination_name
        converted.append(
            build_eb_from_big(
                source,
                template,
                destination,
                map_name,
                asset_namespace=namespace,
                path_name=path_name,
                header_name_offset=name_offset,
                header_metadata=metadata,
                target_slot_width=slot_width,
            )
        )
    manifest = {
        "schema": 1,
        "experimental": True,
        "warning": "Container repack only; guest map descriptors and asset schemas are not converted.",
        "map_name": map_name,
        "archives": converted,
    }
    (output / "conversion_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--skate3-root", type=Path, required=True)
    inspect.add_argument("--skate2-root", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    convert = sub.add_parser("convert")
    convert.add_argument("--skate2-root", type=Path, required=True)
    convert.add_argument("--skate3-template", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--map-name", default="DIST_NewSanVan")
    convert.add_argument(
        "--replace-downtown",
        action="store_true",
        help="emit worldDIST_DownTown.big and proxyDowntown_100_Proxy.big",
    )
    convert.add_argument(
        "--replace-blackbox",
        action="store_true",
        help="emit a single worldDIST_BlackBoxPark.big test archive",
    )
    convert.add_argument(
        "--target-template",
        type=Path,
        help="matching Skate 3 EB3 template for the selected replacement mode",
    )
    args = parser.parse_args()

    if args.command == "convert":
        print(
            json.dumps(
                convert_map(
                    args.skate2_root,
                    args.skate3_template,
                    args.output,
                    args.map_name,
                    args.replace_downtown,
                    args.replace_blackbox,
                    args.target_template,
                ),
                indent=2,
            )
        )
        return 0

    skate3 = inspect_tree(args.skate3_root)
    skate2 = inspect_tree(args.skate2_root)
    skate3["eb"] = [r for r in skate3["eb"] if r["format"] == "EB3"]
    skate2["eb"] = []
    report = {
        "schema": 1,
        "purpose": "format reconnaissance; no conversion performed",
        "skate3": skate3,
        "skate2": skate2,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
