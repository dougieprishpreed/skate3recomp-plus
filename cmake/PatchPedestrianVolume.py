"""Scale the computed speech gain without changing native policy/state/locks.

Fail closed on changed generated instructions; repeat application is a no-op.
"""
from pathlib import Path
import sys


def patch(directory):
    definition = 'DEFINE_REX_FUNC(sub_82C5CEF0) {'
    paths = [p for p in directory.glob('skate3_recomp.*.cpp')
             if definition in p.read_text(encoding='utf8')]
    if len(paths) != 1:
        raise RuntimeError('Expected one speech-update definition')
    path = paths[0]
    raw = path.read_bytes()
    ending = '\r\n' if b'\r\n' in raw else '\n'
    text = raw.decode('utf8').replace('\r\n', '\n')
    start = text.index(definition)
    end = text.index('\n}\n', start) + 3
    body = text[start:end]
    anchor = 'loc_82C5CFD8:\n'
    call = ('\tctx.f1.f64 = double(skate3::pedestrian_speech::ScaleVoiceGain(\n'
            '\t\tbase, ctx.r31.u32, ctx.r24.u32, float(ctx.f1.f64)));\n')
    include = '#include "skate3_pedestrian_speech.h"\n'
    if body.count(anchor) != 1 or body[:body.index(anchor)].count('ctx.f1.f64 = double(float(ctx.f10.f64 * ctx.f0.f64));') != 1:
        raise RuntimeError('Speech gain/mute calculation changed')
    if call in body:
        if body.count(call) != 1 or anchor + call not in body or include not in text:
            raise RuntimeError('Unexpected partial speech-volume patch')
        return
    body = body.replace(anchor, anchor + call)
    text = text[:start] + body + text[end:]
    if include not in text:
        text = include + text
    path.write_bytes(text.replace('\n', ending).encode('utf8'))
    print(f'Applied pedestrian voice volume in {path.name}')


if __name__ == '__main__':
    patch(Path(sys.argv[1]))
