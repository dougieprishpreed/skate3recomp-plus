"""Correlate bounded binding diagnostics with uncompressed BAM texture exports.

This is an export-marker inventory, not an RX2 decompressor. A missing match
does not mean the asset is absent: compressed exports are not searched.
"""
import argparse
import collections
import json
from pathlib import Path
import re


def analyze(log, world):
    rows = [dict(re.findall(r'(\w+)=([^ ]+)', line))
            for line in log.read_text().splitlines()
            if 'world-texture-binding:' in line]
    missing = [r for r in rows if r['placeholder'] == 'true']
    counts = collections.Counter(r['guid'] for r in missing)
    exports = collections.defaultdict(list)
    # Observed export identity followed by the Texture type tag, then count=1.
    marker = re.compile(rb'(\x00\x00..\x03\xe3\x87[\x0a\x0b])'
                        rb'\xac\x46\x2e\x4a\x00\x00\x00\x01', re.S)
    for path in sorted(world.glob('*.xsf')):
        data = path.read_bytes()
        for match in marker.finditer(data):
            raw = int.from_bytes(match[1], 'big')
            requested = f'{raw & ~1:016X}'
            if requested not in counts:
                continue
            end = data.find(b'\0', match.end(), match.end() + 256)
            if end < 0:
                continue
            name = data[match.end():end].decode('ascii', errors='replace')
            if not name.endswith('.Texture'):
                continue
            exports[requested].append(dict(file=path.name, offset=match.start(),
                exported_guid=f'{raw:016X}', name=name))
    return dict(binding_samples=len(rows), placeholder_samples=len(missing),
                placeholder_guids=len(counts),
                invalid_registry_samples=sum(r['invalid_registry'] == 'true' for r in rows),
                placeholder_without_registry=sum(r['registered'] == '00000000' for r in missing),
                located_guids=len(exports),
                unlocated_guids=sorted(counts.keys() - exports.keys()),
                placeholder_counts=dict(counts.most_common()), exports=dict(exports))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('world', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.log, args.world)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ('exports', 'placeholder_counts', 'unlocated_guids')}, indent=2))
