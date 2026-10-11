#!/usr/bin/env python3
"""Extract XML-like Skate 2 world Location records from BIG4 payloads."""
from __future__ import annotations
import argparse,json,re
from pathlib import Path

def extract(path: Path):
    b=path.read_bytes()
    text=b.decode('latin1','ignore')
    rows=[]
    pattern=re.compile(r'<Location>\s*<Name>(.*?)</Name>.*?<Transform>(.*?)</Transform>\s*</Location>',re.S)
    for m in pattern.finditer(text):
        rows.append({'name':m.group(1),'transform':m.group(2).strip()})
    return {'source':str(path),'size':len(b),'location_count':len(rows),'locations':rows}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('source',type=Path);ap.add_argument('--output',type=Path);a=ap.parse_args(); text=json.dumps(extract(a.source),indent=2)
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text,encoding='utf-8');print(a.output)
    else:print(text)
if __name__=='__main__':main()
