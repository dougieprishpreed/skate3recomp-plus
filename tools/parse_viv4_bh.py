#!/usr/bin/env python3
"""Parse Skate 2 Viv4 .bh asset indexes and cross-check BIG4 entries."""
from __future__ import annotations
import argparse,json,struct
from pathlib import Path

def u32(b,o): return struct.unpack_from('>I',b,o)[0]
def big_entries(path):
    from inspect_map_archives import read_big_entries
    return read_big_entries(path)[3]


def parse(bh,big=None):
    b=bh.read_bytes()
    if b[:4]!=b'Viv4' or len(b)<28: raise ValueError(f'not Viv4: {bh}')
    count=u32(b,8); table_size=u32(b,12); data_offset=u32(b,16); declared_stride=u32(b,20)
    stride=16
    if 28 + count * stride > len(b):
        raise ValueError(f'Viv4 table exceeds file: count={count} stride={stride}')
    records=[]
    for i in range(count):
        o=28+i*stride
        records.append({'index':i,'hash':u32(b,o),'offset':u32(b,o+4),'size':u32(b,o+8),'reserved':u32(b,o+12)})
    result={'path':str(bh),'size':len(b),'magic':'Viv4','record_count':count,'table_size':table_size,'data_offset':data_offset,'declared_stride':declared_stride,'actual_stride':stride,'records':records}
    if big:
        entries=big_entries(big); by_key={(e['offset'],e['size']):e for e in entries}
        matched=sum((r['offset'],r['size']) in by_key for r in records)
        result['big_path']=str(big); result['big_entry_count']=len(entries); result['offset_size_matches']=matched
        result['unmatched_records']=[r for r in records if (r['offset'],r['size']) not in by_key][:50]
        result['matched_name_sample']=[dict(r,name=by_key[(r['offset'],r['size'])]['name']) for r in records[:20] if (r['offset'],r['size']) in by_key]
    return result
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('bh',type=Path); ap.add_argument('--big',type=Path); ap.add_argument('--output',type=Path); a=ap.parse_args()
    text=json.dumps(parse(a.bh,a.big),indent=2)
    if a.output: a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(text,encoding='utf-8'); print(a.output)
    else: print(text)
if __name__=='__main__': main()
