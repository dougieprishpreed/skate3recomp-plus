"""Offline production-helper guard/register test. Never launches the game."""
from pathlib import Path
import re
import subprocess

source = Path('src/skate3_world_loading.cpp').read_text()
guest = Path('generated/skate3_recomp.9.cpp').read_text()
helper = source[source.index('template <typename Context>\nvoid GatherBeforeWorldSchedule'):
                source.index('// The active XSF path')]
caller = re.search(r'uint32_t\(ctx.lr\) != (0x[0-9A-F]+)', helper)[1]
assert re.search(r'ctx.lr = ' + caller + r';\s*sub_82479C30\(ctx, base\);', guest)
hook = source[source.index('extern "C" REX_FUNC(sub_82479C30) {'):]
assert hook.index('GatherBeforeWorldSchedule(ctx, base)') < hook.index('__imp__sub_82479C30(ctx, base)')
code = r'''
#include <cassert>
#include <cstdint>
#include <iostream>
union Register { uint64_t u64 = 0; uint32_t u32; };
struct Context { Register r3; uint64_t lr = 0, stack = 0; };
bool bam = true, skate3_skate2_early_gather = true;
unsigned calls = 0, queued = 0;
#define REXCVAR_GET(name) name
#define REXLOG_INFO(...) ((void)0)
namespace skate3::world_loading { bool IsSkate2Gameplay() { return bam; } }
bool StreamTimingEnabled() { return true; }
unsigned StreamVectorCount(uint8_t*, uint32_t sys, unsigned offset, unsigned stride) {
  assert(sys == 0x20000 && offset == 124 && stride == 32); return queued;
}
void __imp__sub_8247A4B8(Context& ctx, uint8_t*) {
  assert(ctx.r3.u32 == 0x20000 && ctx.stack == 0x50000);
  ++calls; queued = 1; ctx.r3.u64 = 999; ctx.lr = 123; ctx.stack = 0;
}
'''
code += helper
code += r'''
int main() {
  Context ctx; ctx.r3.u64 = 0x20000; ctx.lr = CALLER; ctx.stack = 0x50000;
  GatherBeforeWorldSchedule(ctx, nullptr);
  assert(calls == 1 && queued == 1);
  assert(ctx.r3.u64 == 0x20000 && ctx.lr == CALLER && ctx.stack == 0x50000);
  bam = false; GatherBeforeWorldSchedule(ctx, nullptr); assert(calls == 1);
  bam = true; skate3_skate2_early_gather = false;
  GatherBeforeWorldSchedule(ctx, nullptr); assert(calls == 1);
  skate3_skate2_early_gather = true;
  for (auto other : {0u, unsigned(CALLER + 4), 0x82477A94u}) {
    ctx.lr = other; GatherBeforeWorldSchedule(ctx, nullptr); assert(calls == 1);
  }
  std::cout << "Early gather caller, scope, ordering and register preservation passed.\n";
}
'''.replace('CALLER', caller)
out = Path('out/research')
out.mkdir(parents=True, exist_ok=True)
cpp, exe = out / 'test_early_gather.cpp', out / 'test_early_gather.exe'
cpp.write_text(code)
subprocess.run(['clang-cl', '/nologo', '/std:c++20', '/EHsc', str(cpp),
                '/Fo' + str(out / 'test_early_gather.obj'), '/Fe' + str(exe)], check=True)
subprocess.run([str(exe.resolve())], check=True)
