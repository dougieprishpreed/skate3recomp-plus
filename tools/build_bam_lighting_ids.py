"""Inventory original BAM lighting Texture exports for the render ID bridge.

Only metadata is written; source assets are never modified. Export markers are
uncompressed RX2 Texture records, not a general compressed RX2 parser.
"""
import argparse
import collections
import json
from pathlib import Path
import re

MARKER = re.compile(rb"(.{8})\xac\x46\x2e\x4a\x00\x00\x00\x01", re.S)
LIGHTING = re.compile(r"(?:layerpage|chromo)_\[0x[0-9a-f]+\]\[0x[0-9a-f]+\]\[0x[0-9a-f]+\]_[A-Za-z0-9_]+\.Texture$")
FLAG = 1 << 62

def exports(data):
    for m in MARKER.finditer(data):
        if m.start() % 4:
            continue
        end = data.find(b"\0", m.end(), m.end() + 256)
        if end < 0:
            continue
        try:
            name = data[m.end():end].decode("ascii")
        except UnicodeDecodeError:
            continue
        if name.endswith(".Texture") and all(32 <= ord(c) < 127 for c in name):
            yield int.from_bytes(m[1], "big"), name, m.start()

def inventory(world):
    all_names = collections.defaultdict(set)
    lighting = collections.defaultdict(list)
    for path in sorted(world.glob("*.xsf")):
        for guid, name, offset in exports(path.read_bytes()):
            all_names[guid].add(name)
            if LIGHTING.fullmatch(name):
                lighting[guid].append(dict(file=path.name, offset=offset, name=name))
    if not lighting:
        raise ValueError("No original lighting exports found")
    # Material requests already lose bit62 in Skate3's record lookup. Prove
    # that this does not alias distinct source texture families in BAM.
    keys = {}
    for guid in all_names:
        family = guid & ~1
        key = family & ~FLAG
        if key in keys and keys[key] != family:
            raise ValueError(f"Texture record collision: {guid:016X}")
        keys[key] = family
    for guid, rows in lighting.items():
        if any(not LIGHTING.fullmatch(n) for n in all_names[guid]):
            raise ValueError(f"Lighting identity shared with non-lighting export: {guid:016X}")
        if guid ^ 1 in all_names and all_names[guid] != all_names[guid ^ 1]:
            raise ValueError(f"Variant names differ: {guid:016X}")
    return lighting, all_names

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("world", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    lighting, all_names = inventory(args.world)
    ids = sorted(lighting)
    args.output.write_text("BAM_LIGHTING_TEXTURE_IDS_V1\n" + str(len(ids)) + "\n" +
                           "".join(f"{x:016X}\n" for x in ids))
    summary = dict(texture_ids=len(all_names), lighting_ids=len(ids),
                   lighting_pairs=sum(x % 2 == 0 and x + 1 in lighting for x in ids),
                   even_with_bit62=sum(x % 2 == 0 and bool(x & FLAG) for x in ids),
                   source_files=len(list(args.world.glob("*.xsf"))))
    args.report.write_text(json.dumps(dict(summary=summary, exports={f"{k:016X}": v for k,v in lighting.items()}), indent=2) + "\n")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
