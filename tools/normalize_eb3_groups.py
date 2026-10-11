#!/usr/bin/env python3
"""Normalize source world streaming group IDs to a Skate 3 target family."""
from __future__ import annotations
import argparse, json, struct
from pathlib import Path

def u32(b, o): return struct.unpack_from('>I', b, o)[0]
def put32(b, o, v): struct.pack_into('>I', b, o, v)
def normalize(source: Path, target: Path, output: Path) -> dict:
    src=bytearray(source.read_bytes()); tb=target.read_bytes()
    target_body=u32(tb,12)+u32(tb,16)
    target_groups=[]; q=target_body
    while True:
        q=tb.find(b'MMAP',q)
        if q<0: break
        target_groups.append(u32(tb,q+12)); q+=4
    if len(target_groups)!=3: raise ValueError(f'expected 3 target MMAP groups, found {len(target_groups)}')
    source_body=u32(src,12) if src[:4] in (b'BIG4',b'BIGF') else u32(src,12)+u32(src,16)
    mm=[]; q=source_body
    while True:
        q=src.find(b'MMAP',q)
        if q<0 or q>=len(src): break
        mm.append(q); q+=4
    if len(mm)!=3: raise ValueError(f'expected 3 source MMAP groups, found {len(mm)}')
    old_groups=[u32(src,o+12) for o in mm]
    mapping=dict(zip(old_groups,target_groups))
    changed=[]
    for i,m in enumerate(mm):
        put32(src,m+12,target_groups[i]); changed.append({'offset':m+12,'old':old_groups[i],'new':target_groups[i],'kind':'MMAP'})
        # Skate 2 keeps ATOC 64 bytes after MMAP; update its group word.
        a=m+64
        if src[a:a+4]==b'ATOC':
            put32(src,a+12,target_groups[i]); changed.append({'offset':a+12,'old':old_groups[i],'new':target_groups[i],'kind':'ATOC'})
    q=source_body
    while True:
        q=src.find(b'SFIL',q)
        if q<0: break
        old=u32(src,q+12)
        if old in mapping:
            put32(src,q+12,mapping[old]); changed.append({'offset':q+12,'old':old,'new':mapping[old],'kind':'SFIL'})
        q+=4
    output.parent.mkdir(parents=True,exist_ok=True); output.write_bytes(src)
    return {'source':str(source),'target_template':str(target),'output':str(output),'source_groups':[hex(x) for x in old_groups],'target_groups':[hex(x) for x in target_groups],'patched_fields':len(changed),'patched_sample':changed[:20],'size':len(src)}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source',type=Path,required=True);ap.add_argument('--target-template',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args(); print(json.dumps(normalize(a.source,a.target_template,a.output),indent=2))
if __name__=='__main__':main()
