"""Keep the NSV culling algorithm, but compute its hot loop in host locals.

The region has no guest calls. Every used PPC register is copied in and
committed before the unmodified ABI epilogue. FPSCR operations stay original.
"""
from pathlib import Path
import re,sys


def local_variant(function):
    cut=function.index('\tSkate3NsvCullContributionDirection(ctx);\n')+len('\tSkate3NsvCullContributionDirection(ctx);\n')
    last=function.index('\t// addi r1,r1,672\n')
    body=function[cut:last]
    if re.search(r'\bsub_|REX_CALL|__save|__rest',body):
        raise RuntimeError('Guest call appeared inside local cull region')
    states=set(re.findall(r'ctx\.(\w+)',body))
    allowed={'fpscr','cr0','cr6','ctr','xer'}|{f'r{i}' for i in range(32)}|{f'v{i}' for i in range(128)}|{f'f{i}' for i in range(32)}
    if not states<=allowed:raise RuntimeError('Unknown culler architectural state')
    used=sorted(states-{'fpscr'})
    decl=''.join(f'\tauto nsv_{s} = ctx.{s};\n' for s in used)
    commit=''.join(f'\tctx.{s} = nsv_{s};\n' for s in used)
    body=re.sub(r'ctx\.(\w+)',lambda m:'ctx.fpscr' if m[1]=='fpscr' else 'nsv_'+m[1],body)
    result=function[:cut]+'\t{\n'+decl+body+commit+'\t}\n'+function[last:]
    return result.replace('DEFINE_REX_FUNC(sub_82816A40)', 'static void NsvVectorizedCull(PPCContext& ctx, uint8_t* base)')


def patch(directory):
    definition='DEFINE_REX_FUNC(sub_82816A40) {'
    paths=[p for p in directory.glob('skate3_recomp.*.cpp') if definition in p.read_text(encoding='utf8')]
    if len(paths)!=1:raise RuntimeError('Expected one original vectorized culler')
    path=paths[0];raw=path.read_bytes();ending='\r\n' if b'\r\n' in raw else '\n'
    text=raw.decode('utf8').replace('\r\n','\n')
    start=text.index(definition);end=text.index('\n}\n',start)+3
    function=text[start:end]
    dispatch='\n\tif (skate3::world_loading::IsSkate2Gameplay()) {\n\t\tNsvVectorizedCull(ctx, base);\n\t\treturn;\n\t}\n'
    if '// NSV_LOCAL_CULL_BEGIN' in text:
        if function.count(dispatch)!=1:raise RuntimeError('Partial local cull dispatch')
        expected='// NSV_LOCAL_CULL_BEGIN\n'+local_variant(function.replace(dispatch,''))+'\n\n'
        before=text[text.index('// NSV_LOCAL_CULL_BEGIN'):start]
        if before!=expected:raise RuntimeError('Local cull variant differs from original algorithm')
        return
    if 'nsv_projection.Area(' not in function:raise RuntimeError('Perspective compatibility correction missing')
    variant='// NSV_LOCAL_CULL_BEGIN\n'+local_variant(function)+'\n\n'
    function=function.replace('{','{'+dispatch,1)
    text=text[:start]+variant+function+text[end:]
    path.write_bytes(text.replace('\n',ending).encode('utf8'))
    print(f'Applied NSV vectorized-culling local registers in {path.name}')


if __name__=='__main__':patch(Path(sys.argv[1]))
