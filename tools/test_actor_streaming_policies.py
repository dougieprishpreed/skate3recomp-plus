"""Production actor fallback, DLC audio and bounded look-back tests; no game."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActorPolicies(unittest.TestCase):
    def compile(self, directory, code, extra=()):
        compiler = os.environ.get('CXX') or shutil.which('clang++-20') or shutil.which('clang++') or shutil.which('c++')
        self.assertIsNotNone(compiler)
        source = directory / 'test.cpp'
        source.write_text(code, encoding='utf8')
        binary = directory / ('test.exe' if os.name == 'nt' else 'test')
        subprocess.run([compiler, '-std=c++20', '-O2', '-pthread', '-I', str(directory),
                        '-I', str(ROOT / 'src'), str(source), *map(str, extra), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], cwd=directory, check=True)

    def test_dlc_bed_selection_and_isolation(self):
        code = r'''
#include "native/skate3_native_world_policy.h"
#include <cassert>
#include <cstdio>
int main(){
 using namespace skate3::native_policy;
 for(auto name:{"DLC_School","DLC_Community_Center","DLC_Parkade","DLC_MaloofMoneyCup","DLC_Rob_Fantasy_Plaza"}){
  assert(AmbienceFallback(0,7,true,true,name,42)==42);
  assert(AmbienceFallback(19,7,true,true,name,42)==19);
  for(unsigned layer:{0u,2u,3u,8u,10u,11u})assert(AmbienceFallback(0,layer,true,true,name,42)==0);
  assert(AmbienceFallback(0,7,false,true,name,42)==0);
  assert(AmbienceFallback(0,7,true,false,name,42)==0);
 }
 for(auto name:{"BAM","Downtown","University","DLC_Unknown",""})assert(AmbienceFallback(0,7,true,true,name,42)==0);
 assert(DlcAmbienceBed("DLC_Parkade",42,99)==99);
 assert(DlcAmbienceBed("DLC_Rob_Fantasy_Plaza",42,99)==99);
 for(auto name:{"DLC_School","DLC_Community_Center","DLC_MaloofMoneyCup"})
   assert(DlcAmbienceBed(name,42,99)==42);
 assert(DlcAmbienceBed("DLC_Parkade",42,0)==0); // unavailable requested bed stays native
 puts("PASS selected DLC ambience, nonzero paint/retail/layer/readiness isolation");
}
'''.replace('#include <cassert>', '#include <cassert>\n#include <algorithm>')
        with tempfile.TemporaryDirectory() as temp:
            self.compile(Path(temp), code)

    def test_actual_publish_fallback(self):
        text = (ROOT / 'src/skate3_native_scene.cpp').read_text(encoding='utf8')
        start = text.index('    struct PubTrack {')
        end = text.index('  trace_capture(scene.items.empty()', start)
        body = text[start:end].rsplit('\n  }', 1)[0]
        code = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <unordered_map>
#include <vector>
bool skate3_native_render_scene_dyn_gap_fill=true;
#define REXCVAR_GET(n) (n)
#define REXLOG_INFO(...) ((void)0)
std::atomic<uint64_t> g_dyn_gap{0};
struct DrawItem {uint32_t mesh=5,ctx=0;bool ropa=false,skinned=true,caster_bank=false;uint32_t char_family=2;uint8_t dbg_src=0;std::vector<float> bones=std::vector<float>(12);};
struct FrameScene {uint64_t world_identity=2;std::vector<DrawItem> items;};
namespace skate3::native_entity {
struct CtxInfo {uint32_t entity=0;bool view_live=false;};
std::unordered_map<uint32_t,CtxInfo> live;
bool LookupCtx(uint32_t ctx,CtxInfo* out){auto i=live.find(ctx);if(i==live.end())return false;*out=i->second;return true;}
}
size_t tracked=0;
void build(FrameScene& scene){
'''+body+r'''
 tracked=s_last_pub.size();
}
DrawItem item(uint32_t ctx,float x,bool caster=false){DrawItem i;i.ctx=ctx;i.bones[3]=x;i.caster_bank=caster;i.dbg_src=11;return i;}
int main(){
 using namespace skate3::native_entity;
 live[10]={1,true};live[20]={2,true};
 FrameScene f;f.items={item(10,1),item(20,20)};build(f);
 for(int step=1;step<=4;++step){
  f.items={item(10,float(step+1),true),item(20,float(20+step))};build(f);
  assert(f.items.size()==2); // caster reuse cannot also append gap fill
  assert(f.items[0].dbg_src==11); // preserve authoritative producer, no fallback reclassification
  assert(f.items[0].ctx==10&&f.items[0].bones[3]==(step<=2?1.f:float(step+1)));
  assert(f.items[1].ctx==20&&f.items[1].bones[3]==float(20+step));
 }
 f.items={item(10,10),item(20,30)};build(f);
 for(int step=1;step<=3;++step){f.items={item(20,float(30+step))};build(f);assert(f.items.size()==(step<=2?2:1));for(const auto&i:f.items)assert(i.dbg_src==11);}
 f.items={item(10,40),item(20,40)};build(f);
 live[10]={3,true};f.items={item(10,50,true),item(20,41)};build(f);assert(f.items[0].bones[3]==50);
 f.world_identity=4;f.items={item(10,60,true)};build(f);assert(f.items.size()==1&&f.items[0].bones[3]==60);
 f.items={item(10,70)};build(f);live[10].view_live=false;f.items.clear();build(f);assert(f.items.empty());
 for(int i=0;i<10;++i)build(f);assert(tracked==0);
 puts("PASS actual publish fallback: shared mesh clones,2-real-frame cap, no duplicate, missing observations, owner recycle/despawn, world epoch and bounded retirement");
}
'''
        with tempfile.TemporaryDirectory() as temp:
            self.compile(Path(temp), code)

    def test_actual_recorder_roundtrip(self):
        with tempfile.TemporaryDirectory(prefix="actor history user's ") as temp:
            work = Path(temp)
            def stub(name, text):
                p = work / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding='utf8')
            stub('rex/cvar.h', '''#pragma once
namespace rex::cvar {enum class Lifecycle{kHotReload};}
struct TestCvar {TestCvar lifecycle(rex::cvar::Lifecycle){return {};}};
#define REXCVAR_DEFINE_BOOL(n,d,...) bool n=d;auto flag_##n=TestCvar{}
#define REXCVAR_DECLARE(t,n) extern t n
#define REXCVAR_GET(n) (n)
''')
            stub('rex/logging.h', '#pragma once\n#define REXLOG_WARN(...) ((void)0)\n')
            stub('native/skate3_native_entity.h', '''#pragma once
#include <cstdint>
namespace skate3::native_entity {
struct CtxInfo {uint32_t entity=111,instance=222,cls=4;bool view_live=true;};
inline bool LookupCtx(uint32_t,CtxInfo* out){*out={};return true;}
inline bool ReadEntityWorldRows(uint8_t*,uint32_t,float* out){out[0]=out[5]=out[10]=1;return true;}
inline uint32_t ServeInstancePalette(uint8_t*,uint32_t,float*,uint32_t){return 0;}
}
''')
            stub('skate3_native_scene.h', '''#pragma once
#include <cstdint>
#include <vector>
namespace skate3::native_scene {
struct DrawItem {uint32_t mesh=5,ctx=10,vb_obj=11,ib_obj=12,char_family=2;bool ropa=false,skinned=true,caster_bank=false,pending=false,retained=false;uint8_t dbg_src=1;float world[16]{},char_rows[72]{};std::vector<float> bones=std::vector<float>(24);};
struct FrameScene {uint64_t generation=0,world_identity=2;float cam_pos[3]{},view_proj[16]{};std::vector<DrawItem> items;};
}
''')
            code = r'''
#include "native/skate3_native_actor_history.h"
#include "skate3_native_scene.h"
#include <filesystem>
#include <cassert>
extern bool skate3_actor_history_capture;
bool skate3_native_render_scene_smooth_entities=true,skate3_native_render_scene_smooth_camera=true;
int main(){
 using namespace skate3::actor_history;skate3::native_scene::FrameScene f;f.items.push_back({});
 for(int j=0;j<49;++j){auto part=f.items[0];part.ctx=100+j;part.mesh=1000+j;part.bones[3]=10000.f+j*100;f.items.push_back(part);}
 Request();Begin(nullptr,f,0,0);End(f,0,false);assert(!std::filesystem::exists("logs"));
 skate3_actor_history_capture=true;
 for(int i=0;i<1400;++i){
  if(i==650)f.world_identity=4;
  f.generation=i;f.items[0].bones[3]=float(i);Begin(nullptr,f,double(i)/60,i);
  Retime(f.items[0],99,7,1./60,double(i)/60-.02);f.items[0].bones[3]+=.25f;
  auto fill=f.items[0];fill.ctx=20;fill.mesh=6;fill.dbg_src=12;f.items.push_back(fill);
  if(i==1399)Request();End(f,double(i)/60-.01,true);f.items.pop_back();
 }
 Shutdown();
}
'''
            self.compile(work, code, [ROOT / 'src/native/skate3_native_actor_history.cpp'])
            binaries = list((work / 'logs').glob('actor_history_*.bin'))
            self.assertEqual(len(binaries), 1)
            binary = binaries[0]
            schema = json.loads(binary.with_suffix('.json').read_text())
            data = binary.read_bytes()
            self.assertEqual(data[:8], b'S3ACT219')
            count, frame_size, actor_size, pose_size = struct.unpack_from('<4I', data, 8)
            self.assertEqual(len(data), 24 + count * frame_size)
            self.assertLessEqual(count, 2048)
            self.assertLessEqual(frame_size * 2048, 128 * 1024 * 1024)
            first_time = last_time = None
            for i in range(count):
                at = 24 + i * frame_size
                frame = schema['frame']; actor = schema['actor']; pose = schema['pose']
                world, = struct.unpack_from('<Q', data, at + frame['world'])
                self.assertEqual(world, 4)
                t, = struct.unpack_from('<d', data, at + frame['time'])
                if first_time is None: first_time = t
                last_time = t
                n, = struct.unpack_from('<I', data, at + frame['count'])
                self.assertEqual(n, 48)
                a = at + frame['actors']
                raw, = struct.unpack_from('<f', data, a + actor['raw'] + pose['bones'] + 12)
                final, = struct.unpack_from('<f', data, a + actor['final'] + pose['bones'] + 12)
                self.assertAlmostEqual(final - raw, .25)
                key, = struct.unpack_from('<Q', data, a + actor['history_key'])
                self.assertEqual(key, 99)
                fills=[]
                for part in range(n):
                    loc=a+part*actor_size
                    ctx,=struct.unpack_from('<I',data,loc+actor['ctx'])
                    if ctx==20:fills.append(loc)
                self.assertEqual(len(fills),1)
                raw_present,=struct.unpack_from('<I',data,fills[0]+actor['raw']+pose['present'])
                final_present,=struct.unpack_from('<I',data,fills[0]+actor['final']+pose['present'])
                self.assertEqual((raw_present,final_present),(0,1))
            self.assertLessEqual(last_time - first_time, 8.00001)
            self.assertGreater(last_time - first_time, 7.9)
            parsed = subprocess.run([shutil.which('python') or 'python', str(ROOT / 'tools/analyze_actor_history.py'), str(binary), '--top', '2'], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(parsed.stdout)['parts'], count*48)
            print('PASS actual recorder: disabled does no I/O, prior8 seconds, epoch reset, raw/final/history keys, final-only gap fills/nearest48 cap, bounded storage, asynchronous binary/schema roundtrip')


if __name__ == '__main__':
    unittest.main()
