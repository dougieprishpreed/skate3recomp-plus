"""Compile actual asset hook with a fake worker ring; never run the game."""
from pathlib import Path
import subprocess
source = Path('src/skate3_world_loading.cpp').read_text()
hook = source[source.index('extern "C" REX_FUNC(sub_82C96E58) {'):
              source.index('// Capture identity before the completion callback')]
guest = Path('generated/skate3_recomp.81.cpp').read_text()
assert 'ctx.lr = 0x82C96334;\n\tsub_82C96E58(ctx, base);' in guest
code = r'''
#include "../../src/skate3_stream_pump.h"
#include <array>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <iostream>
union Register { uint64_t u64 = 0; uint32_t u32; };
struct Context { Register r3; uint64_t lr = 0; };
#define REX_FUNC(name) void name(Context& ctx, uint8_t* base)
#define REXCVAR_GET(name) name
#define REXLOG_INFO(...) ((void)0)
bool bam = true, skate3_skate2_asset_budget = true, blocked = false;
unsigned submitted = 0, completed = 0, done = 0, calls = 0;
namespace skate3::world_loading { bool IsSkate2Gameplay() { return bam; } }
bool StreamTimingEnabled() { return false; }
double StreamMilliseconds(std::chrono::steady_clock::duration d) {
  return std::chrono::duration<double, std::milli>(d).count();
}
uint32_t StreamWord(uint8_t*, uint32_t task, unsigned offset) {
  assert(task == 0x20000);
  switch(offset) {
    case 32: return done << 24;
    case 68: return 0;
    case 92: return completed;
    case 96: return submitted;
  } return ~0u;
}
void __imp__sub_82C96E58(Context& ctx, uint8_t*) {
  assert(ctx.r3.u32 == 0x20000); ++calls;
  if (!blocked && !done) submitted += 16;
  ctx.r3.u64 = 0xBAD;
}
'''
code += hook
code += r'''
void Invoke(uint64_t caller = 0x82C96334) {
  Context ctx; ctx.r3.u64 = 0x20000; ctx.lr = caller;
  sub_82C96E58(ctx, nullptr);
  assert(ctx.r3.u64 == 0xBAD && ctx.lr == caller);
}
void Reset() { submitted = completed = done = calls = 0;
  bam = skate3_skate2_asset_budget = true; blocked = false; }
int main() {
  Reset(); Invoke(); assert(calls == 3 && submitted == 48 && !done);
  Reset(); blocked = true; Invoke(); assert(calls == 2 && !submitted);
  Reset(); done = 1; Invoke(); assert(calls == 1 && !submitted);
  Reset(); submitted = 48; Invoke(); assert(calls == 1 && submitted == 64);
  Reset(); completed = 200; Invoke(); assert(calls == 1); // Invalid ring snapshot.
  Reset(); bam = false; Invoke(); assert(calls == 1);
  Reset(); skate3_skate2_asset_budget = false; Invoke(); assert(calls == 1);
  Reset(); Invoke(0x82C96338); assert(calls == 1);
  std::cout << "Asset budget ring, progress, scope and register tests passed.\n";
}
'''
out = Path('out/research')
cpp, exe = out / 'test_asset_budget.cpp', out / 'test_asset_budget.exe'
cpp.write_text(code)
subprocess.run(['clang-cl', '/nologo', '/std:c++20', '/EHsc', str(cpp),
                '/Fo' + str(out / 'test_asset_budget.obj'), '/Fe' + str(exe)], check=True)
subprocess.run([str(exe.resolve())], check=True)
