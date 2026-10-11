"""Compile the actual priority hook against fake guest queues; never run the game.

Run from the repository root in the VS developer shell with clang-cl on PATH.
The generated guest call sites are the oracle for the accepted return addresses.
Original selectors are mocked: this validates routing/guards, not asset lifetimes.
"""
from pathlib import Path
import re
import subprocess

source = Path("src/skate3_world_loading.cpp").read_text()
guest = Path("generated/skate3_recomp.9.cpp").read_text()
start = source.index('extern "C" REX_FUNC(sub_82479B78) {')
end = source.index('extern "C" REX_FUNC(sub_82C9D0D8)', start)
hook = source[start:end]
planner = guest[guest.index("DEFINE_REX_FUNC(sub_82479C30)"):
                guest.index("DEFINE_REX_FUNC(sub_8247A4B8)")]
actual_returns = set(re.findall(
    r"ctx\.lr = (0x[0-9A-F]+);\s*sub_82479B78\(ctx, base\);", planner))
guard_returns = set(re.findall(r"caller != (0x[0-9A-F]+)", hook))
assert len(guard_returns) == 2
assert guard_returns <= actual_returns, (guard_returns, actual_returns)

prefix = r'''
#include <cassert>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <map>
#include <thread>
union Register { uint64_t u64 = 0; uint32_t u32; };
struct Context { Register r3, r4, r5; uint64_t lr = 0; };
#define REX_FUNC(name) void name(Context& ctx, uint8_t* base)
#define REXCVAR_GET(name) name
#define REXLOG_INFO(...) ((void)0)
bool skate3_skate2_sim_priority = true, bam = true, load_allowed = true;
namespace skate3::world_loading { bool IsSkate2Gameplay() { return bam; } }
bool StreamTimingEnabled() { return true; }
std::map<uint32_t, uint32_t> words;
uint32_t StreamWord(uint8_t*, uint32_t object, uint32_t offset) {
  const auto it = words.find(object + offset);
  return it == words.end() ? ~0u : it->second;
}
'''
# Exercise the production queue-count, GUID and timing helpers as well.
prefix += source[source.index("uint64_t StreamId("):
                 source.index("struct ReleaseSnapshot")]
prefix += source[source.index("double StreamMilliseconds("):
                 source.index("template <typename Context>\nvoid ProcessWorldReleases")]
prefix += r'''
constexpr uint32_t sys = 0x20000, output = 0x50000;
unsigned pres_calls = 0, sim_unload_calls = 0, sim_load_calls = 0;
void Seed(unsigned loads = 1, unsigned unloads = 1) {
  words.clear();
  words[sys + 124] = 0x40000;
  words[sys + 128] = 0x40000 + loads * 32;
  words[sys + 172] = 0x41000;
  words[sys + 176] = 0x41000 + unloads * 32;
  words[sys + 71460] = 0;
  words[sys + 71464] = 0;
  words[sys + 71456] = 1;
  words[0x40000 + 16] = 0x12345678;
  words[0x40000 + 20] = 0x90abcdef;
  pres_calls = sim_unload_calls = sim_load_calls = 0;
  bam = skate3_skate2_sim_priority = load_allowed = true;
}
extern "C" REX_FUNC(__imp__sub_82479B78) {
  if (ctx.r4.u32 == 0) ++pres_calls;
  else { assert(ctx.r4.u32 == 1); ++sim_unload_calls; words[sys + 176] -= 32; }
  words[ctx.r5.u32 + 4] = 0; // An unload record for the common dispatcher.
  ctx.r3.u64 = 1;
}
extern "C" REX_FUNC(sub_824798C8) {
  assert(ctx.r4.u32 == 1);
  ++sim_load_calls;
  ctx.r3.u64 = load_allowed;
  if (load_allowed) { words[sys + 128] -= 32; words[ctx.r5.u32 + 4] = 1; }
}
extern "C" REX_FUNC(sub_82479A40) { sub_824798C8(ctx, base); }
'''
tests = r'''
bool Invoke(uint32_t caller, uint32_t kind = 0) {
  Context ctx;
  ctx.r3.u64 = sys; ctx.r4.u64 = kind; ctx.r5.u64 = output; ctx.lr = caller;
  sub_82479B78(ctx, nullptr);
  return ctx.r3.u32 != 0;
}
int main() {
  for (const auto caller : {CALLERS}) {
    Seed();
    assert(Invoke(caller) && sim_unload_calls == 1 && pres_calls == 0);
    assert(words[output + 4] == 0);
    assert(Invoke(caller) && sim_load_calls == 1 && pres_calls == 0);
    assert(words[output + 4] == 1); // Dispatcher receives a load, not an unload.
    assert(Invoke(caller) && pres_calls == 1); // Normal work resumes.
    Seed(1, 0); words[sys + 71456] = 0; // Other normal scheduling branch.
    assert(Invoke(caller) && sim_load_calls == 1);
    Seed(); words[sys + 71460] = 1;
    assert(Invoke(caller) && pres_calls == 1 && sim_unload_calls == 0);
    Seed(); words[sys + 71464] = 0x01000000;
    assert(Invoke(caller) && pres_calls == 1 && sim_unload_calls == 0);
    Seed(); skate3_skate2_sim_priority = false;
    assert(Invoke(caller) && pres_calls == 1);
    Seed(); bam = false;
    assert(Invoke(caller) && pres_calls == 1);
    Seed(); words[sys + 128] = 0x40000 + 65 * 32;
    assert(Invoke(caller) && pres_calls == 1);
    Seed();
    assert(Invoke(caller + 4) && pres_calls == 1); // The previous bug's address.
  }
  constexpr auto caller = FIRST_CALLER;
  Seed(1, 0); load_allowed = false; // Original release fence refuses the load.
  assert(!Invoke(caller) && pres_calls == 0 && sim_load_calls == 1);
  std::this_thread::sleep_for(std::chrono::milliseconds(270));
  assert(Invoke(caller) && pres_calls == 1); // Deadline prevents starvation.
  Seed(0, 0); assert(Invoke(caller)); // Request disappears/reset interval.
  Seed(1, 0); assert(Invoke(caller) && sim_load_calls == 1 && pres_calls == 0);
  std::cout << "Actual priority hook routing/guard scenarios passed.\n";
}
'''
tests = tests.replace("{CALLERS}", "{" + ", ".join(sorted(guard_returns)) + "}")
tests = tests.replace("FIRST_CALLER", sorted(guard_returns)[0])
out = Path("out/research")
out.mkdir(parents=True, exist_ok=True)
cpp, exe = out / "test_sim_priority.cpp", out / "test_sim_priority.exe"
cpp.write_text(prefix + hook + tests)
subprocess.run(["clang-cl", "/nologo", "/std:c++20", "/EHsc", str(cpp),
                "/Fo" + str(out / "test_sim_priority.obj"), "/Fe" + str(exe)], check=True)
subprocess.run([str(exe.resolve())], check=True)
print("Accepted caller addresses verified against generated guest calls:",
      ", ".join(sorted(guard_returns)))
