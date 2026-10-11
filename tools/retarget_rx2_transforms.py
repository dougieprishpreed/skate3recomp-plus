#!/usr/bin/env python3
"""Retarget a Skate 3 RX2 locator arena with Skate 2 transforms.

This preserves the Skate 3 dictionary, group hashes, record names, TOC, and
binary layout. Only the 4x4 transform payloads are replaced, which makes it a
controlled locator experiment rather than a guessed RX2 recompiler.
"""
from __future__ import annotations
import argparse,json,struct,shutil
from pathlib import Path

def u32(b,o): return struct.unpack_from('>I',b,o)[0]
def put_floats(b,o,vals): struct.pack_into('>16f',b,o,*vals)
def retarget(src_json: Path, templates: list[Path], out: Path):
    source=json.loads(src_json.read_text(encoding='utf-8'))['locations']
    transforms=[[float(x.strip()) for x in r['transform'].split(',')] for r in source]
    if any(len(x)!=16 for x in transforms): raise ValueError('source transform is not 16 floats')
    out.mkdir(parents=True,exist_ok=True); results=[]; cursor=0
    for template in templates:
        b=bytearray(template.read_bytes()); do=u32(b,0x30); changed=0; loc=None
        for i in range(u32(b,0x20)):
            o=do+i*0x18
            if u32(b,o+0x14)==0x00EB0009: loc=(u32(b,o),u32(b,o+8)); break
        if loc is not None:
            obj,size=loc; gc=u32(b,obj); rc=u32(b,obj+4); rr=obj+0x20+gc*0x80
            for i in range(rc):
                put_floats(b,rr+i*0x70,transforms[cursor % len(transforms)]); cursor+=1; changed+=1
        dest=out/template.name; dest.write_bytes(b)
        results.append({'source':str(template),'output':str(dest),'records_retargeted':changed,'size':len(b)})
    (out/'locator_retarget_manifest.json').write_text(json.dumps({'source_locations':len(transforms),'archives':results},indent=2),encoding='utf-8')
    return results
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source-locations',type=Path,required=True); ap.add_argument('--output',type=Path,required=True); ap.add_argument('templates',nargs='+',type=Path); a=ap.parse_args(); print(json.dumps(retarget(a.source_locations,a.templates,a.output),indent=2))
if __name__=='__main__':main()
