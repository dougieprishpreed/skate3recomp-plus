"""Use the actual view direction for NSV world-space area contribution tests.

One register correction per job. No guest memory writes or visibility bypass.
Fail closed if the generated instruction sequence changes; safe to run again.
"""
from pathlib import Path
import sys


def patch(directory):
    definition = 'DEFINE_REX_FUNC(sub_82816A40) {'
    paths = [p for p in directory.glob('skate3_recomp.*.cpp')
             if definition in p.read_text(encoding='utf8')]
    if len(paths) != 1:
        raise RuntimeError('Expected exactly one vectorized cull definition')
    path = paths[0]
    raw = path.read_bytes()
    ending = '\r\n' if b'\r\n' in raw else '\n'
    text = raw.decode('utf8').replace('\r\n', '\n')
    start = text.index(definition)
    end = text.index('\n}\n', start) + 3
    body = text[start:end]
    call = '\tSkate3NsvCullContributionDirection(ctx);\n'
    anchor = ('\t// lvx128 v55,r4,r10\n'
              '\tea = (ctx.r4.u32 + ctx.r10.u32) & ~0xF;\n'
              '\tsimde_mm_store_si128((simde__m128i*)ctx.v55.u8, simde_mm_shuffle_epi8('
              'simde_mm_load_si128((simde__m128i*)REX_RAW_ADDR(ea)), '
              'simde_mm_load_si128((simde__m128i*)VectorMaskL)));\n')
    if body.count(anchor) != 1 or body.count('// vspltw128 v20,v58,0') != 1:
        raise RuntimeError('NSV contribution direction instruction anchor changed')
    include = '#include "native/skate3_native_cull_compat.h"\n'
    if call in body:
        if body.count(call) != 1 or anchor + call not in body or include not in text:
            raise RuntimeError('Unexpected partial NSV direction patch')
    else:
        body = body.replace(anchor, anchor + call)
    sites = [
        ('\tREX_FUNC_PROLOGUE();\n', '\tSkate3NsvCullProjectionBound nsv_projection(base, ctx.r4.u32);\n\tstd::array<float,4> nsv_area{};\n'),
        ('\t// vcmpgefp128 v5,v18,v63\n', '\tnsv_area = nsv_projection.Area(ctx.v55.f32, ctx.v56.f32, ctx.v57.f32, ctx.v58.f32, ctx.v59.f32, ctx.v60.f32, ctx.v108.f32, ctx.v107.f32, ctx.v106.f32);\n'),
        ('\t// vmulfp128 v58,v10,v0\n', '\tSkate3NsvCullProjectionBound::Apply(ctx.v10.f32, nsv_area);\n'),
    ]
    for site, addition in sites:
        if addition in body:
            if body.count(addition) != 1: raise RuntimeError('Duplicate NSV area bound hook')
        elif body.count(site) != 1:
            raise RuntimeError('NSV area bound instruction anchor changed')
        elif 'REX_FUNC_PROLOGUE' in site:
            body = body.replace(site, site + addition)
        else:
            body = body.replace(site, addition + site)
    if text[start:end] == body:
        return
    text = text[:start] + body + text[end:]
    if include not in text:
        text = include + text
    path.write_bytes(text.replace('\n', ending).encode('utf8'))
    print(f'Applied NSV perspective contribution correction in {path.name}')


if __name__ == '__main__':
    patch(Path(sys.argv[1]))
