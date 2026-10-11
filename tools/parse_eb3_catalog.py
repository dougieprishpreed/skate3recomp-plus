#!/usr/bin/env python3
"""Inspect indexed streaming sections in BIG4 and EB3 archives.

Physical padding between files is not part of a section's record layout.
This reader supersedes the old global-marker scan and MMAP+32/+64 inference.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from world_archive import inspect


def parse_archive(path):
    return inspect(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {str(p): parse_archive(p) for p in args.paths}
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(args.output)
    else:
        print(text)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
