"""Offline actual query hook test, including BE guest writes and scope guards."""
from pathlib import Path
import subprocess
s=Path('src/skate3_world_loading.cpp').read_text()
hook=s[s.index('extern "C" REX_FUNC(sub_82476158) {'):s.index('// Compare the engine-built neighbor lists')]
g=Path('generated/skate3_recomp.9.cpp').read_text()
assert 'ctx.lr = 0x8247CDAC;\n\tsub_82476158(ctx, base);' in g
code=r'''
#include "../../src/skate3_sim_extent.h"
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
union Reg { uint64_t u64=0; uint32_t u32; };
struct Context { Reg r3; uint64_t lr; };
#define REX_FUNC(name) void name(Context& ctx, uint8_t* base)
bool building_bam_sim_neighbors=true;
uint32_t expanded_sim_queries=0, calls=0;
bool GuestU32(uint8_t* b,uint32_t p,uint32_t& v) {
 if(p<0x10000||p>=0x10040)return false;
 std::memcpy(&v,b+p,4);v=__builtin_bswap32(v);return true;
}
void __imp__sub_82476158(Context&,uint8_t*) {++calls;}
'''+hook+r'''
int main() {
 uint8_t memory[0x10040]{};
 Context ctx;ctx.r3.u64=0x10000;ctx.lr=0x8247CDAC;
 auto put=[&](unsigned o,float f){uint32_t v;std::memcpy(&v,&f,4);v=__builtin_bswap32(v);std::memcpy(memory+0x10000+o,&v,4);};
 auto get=[&](unsigned o){uint32_t v;assert(GuestU32(memory,0x10000+o,v));float f;std::memcpy(&f,&v,4);return f;};
 auto seed=[&](){put(16,505);put(24,905);put(32,695);put(40,1095);put(20,-8000);put(36,8000);};
 seed();sub_82476158(ctx,memory);
 assert(get(16)==405&&get(24)==805&&get(32)==795&&get(40)==1195);
 assert(get(20)==-8000&&get(36)==8000&&expanded_sim_queries==1&&calls==1);
 sub_82476158(ctx,memory);assert(expanded_sim_queries==1); // No double expansion.
 seed();ctx.lr=0x8247CDF4;sub_82476158(ctx,memory);assert(get(16)==505);
 ctx.lr=0x8247CDAC;building_bam_sim_neighbors=false;
 sub_82476158(ctx,memory);assert(get(16)==505);
 building_bam_sim_neighbors=true;put(16,std::numeric_limits<float>::quiet_NaN());
 sub_82476158(ctx,memory);assert(get(32)==695&&expanded_sim_queries==1);
 assert(ctx.r3.u64==0x10000&&ctx.lr==0x8247CDAC);
 std::cout<<"Actual extent hook scope, endian, inset, Y preservation and repeated-call tests passed.\n";
}
'''
out=Path('out/research');cpp=out/'test_sim_extent.cpp';exe=out/'test_sim_extent.exe';cpp.write_text(code)
subprocess.run(['clang-cl','/nologo','/std:c++20','/EHsc',str(cpp),'/Fo'+str(out/'test_sim_extent.obj'),'/Fe'+str(exe)],check=True)
subprocess.run([str(exe.resolve())],check=True)
