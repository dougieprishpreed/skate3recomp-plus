"""Exercise the production progress snapshot without guest execution."""
from pathlib import Path
import subprocess
source = Path('src/skate3_world_loading.cpp').read_text()
hook = source[source.index('extern "C" REX_FUNC(sub_8247A4B8) {'):]
snapshot = hook[hook.index('  struct Snapshot {'):hook.index('  const auto began =')]
code = r'''
#include <array>
#include <cassert>
#include <cstdint>
#include <iostream>
#include <map>
std::map<uint32_t, uint32_t> words;
uint32_t StreamWord(uint8_t*, uint32_t p, uint32_t o) {
  auto i = words.find(p + o); return i == words.end() ? ~0u : i->second;
}
uint64_t StreamId(uint8_t*, uint32_t, uint32_t) { return 123; }
uint32_t StreamVectorCount(uint8_t*, uint32_t, uint32_t, uint32_t) { return 0; }
int main() {
  uint8_t* base = nullptr;
  uint32_t sys = 0x20000, monitor = 0x30000, task = 0x100000;
  words[monitor+28]=1; words[monitor+1400]=1;
  words[monitor+384]=0x40000; words[monitor+340]=0;
  words[0x40000+116]=0;
  words[sys+8]=0x50000; words[0x50000+4]=0x60000;
  words[0x60000+414864]=task;
  words[task+32]=0; words[task+48]=0x110000; words[task+68]=0;
  words[task+92]=0; words[task+96]=0;
'''
code += snapshot
code += r'''
  auto initial=read(); assert(initial.valid && initial.waiting);
  words[task+96]=20;
  auto submitted=read(); assert(submitted.valid && submitted!=initial);
  words[task+92]=20;
  auto complete=read(); assert(complete.valid && complete!=submitted);
  words[task+68]=~0u;
  auto eof=read(); assert(eof.valid && eof!=complete);
  words[0x60000+414864]=0;
  auto empty=read(); assert(empty.valid && empty!=eof);
  words[0x60000+414864]=~0u; assert(!read().valid);
  std::cout << "Production snapshot detects internal submission, completion, EOF and teardown.\n";
}
'''
out = Path('out/research')
cpp, exe = out/'test_stream_snapshot.cpp', out/'test_stream_snapshot.exe'
cpp.write_text(code)
subprocess.run(['clang-cl', '/nologo', '/std:c++20', '/EHsc', str(cpp),
                '/Fo'+str(out/'test_stream_snapshot.obj'), '/Fe'+str(exe)], check=True)
subprocess.run([str(exe.resolve())], check=True)
