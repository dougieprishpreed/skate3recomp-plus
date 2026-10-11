#!/usr/bin/env python3
"""Parse Skate 3 global locator RW4/RX2 arenas."""
from __future__ import annotations
import argparse,json,struct
from pathlib import Path

def u32(b,o): return struct.unpack_from('>I',b,o)[0]
def u64(b,o): return struct.unpack_from('>Q',b,o)[0]
def f32(b,o): return struct.unpack_from('>f',b,o)[0]
def cstr(b,o):
 e=b.find(b'\0',o); return b[o:e].decode('latin1','replace') if e>=0 else ''
def parse(path: Path):
 b=path.read_bytes()
 if b[:8] != b'\x89RW4xb2\0': raise ValueError(f'not RW4 locator arena: {path}')
 d={'path':str(path),'size':len(b),'header':{},'dictionary':[]}
 d['header']={'id':u32(b,0x1c),'num_entries':u32(b,0x20),'num_used':u32(b,0x24),'alignment':u32(b,0x28),'dictionary_offset':u32(b,0x30),'manifest_offset':u32(b,0x34),'resource_offset':u32(b,0x44),'resource_size':len(b)}
 do=d['header']['dictionary_offset']
 for i in range(d['header']['num_entries']):
  o=do+i*0x18
  d['dictionary'].append({'index':i,'pointer':u32(b,o),'relocation':u32(b,o+4),'size':u32(b,o+8),'alignment':u32(b,o+12),'type_index':u32(b,o+16),'type_id':u32(b,o+20)})
 for e in d['dictionary']:
  o=e['pointer']; typ=e['type_id']
  if typ==0x00EB0008:
   e['object']={'type':'VERSIONDATA','version':u32(b,o),'variant':u32(b,o+4)}
  elif typ==0x00EB0009:
   gc,rc=u32(b,o),u32(b,o+4); gr=o+0x20; rr=gr+gc*0x80; st=rr+rc*0x70
   groups=[]
   for i in range(gc):
    g=gr+i*0x80; groups.append({'index':i,'translation':[f32(b,g+0x30+j*4) for j in range(4)],'record_count':u32(b,g+0x60),'records_offset':u32(b,g+0x64),'name_offset':u32(b,g+0x68),'parent_offset':u32(b,g+0x6c),'hash':u64(b,g+0x70),'name':cstr(b,o+u32(b,g+0x68)),'parent':cstr(b,o+u32(b,g+0x6c))})
   records=[]
   for i in range(rc):
    r=rr+i*0x70; records.append({'index':i,'transform':[f32(b,r+j*4) for j in range(16)],'name_offset':u32(b,r+0x60),'parent_offset':u32(b,r+0x64),'name':cstr(b,o+u32(b,r+0x60)),'parent':cstr(b,o+u32(b,r+0x64))})
   e['object']={'type':'LOCATIONDESCDATA','group_count':gc,'record_count':rc,'groups':groups,'records':records,'string_table_offset':st}
  elif typ==0x00EB000B:
   e['object']={'type':'TABLEOFCONTENTS','header_words':[u32(b,o+x) for x in range(0,24,4)],'bytes_available':e['size']}
 return d
def main():
 ap=argparse.ArgumentParser();ap.add_argument('paths',nargs='+',type=Path);ap.add_argument('--output',type=Path);a=ap.parse_args(); out={str(p):parse(p) for p in a.paths}; text=json.dumps(out,indent=2)
 if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text,encoding='utf-8');print(a.output)
 else:print(text)
if __name__=='__main__':main()
