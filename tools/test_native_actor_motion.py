"""Run the complete production entity retimer with its real DrawItem layout."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def fixture(scene_text, palette_text):
    start = scene_text.index('void InterpolateDynamicItems(')
    end = scene_text.index("// Publish the frame's 2D", start)
    retimer = scene_text[start:end]
    start = palette_text.index('    std::memcpy(item.bones.data(), pick_rows, size_t(n) * 12 * sizeof(float));',
                               palette_text.index('    hist.frame = g_frames;'))
    end = palette_text.index('    if (served_exact)', start)
    # Actual authoritative-palette commit, including its existing source11.
    commit = palette_text[start:end]
    return r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iterator>
#include <unordered_map>
#include <vector>
#include "skate3_native_scene.h"
using skate3::native_scene::DrawItem;
using skate3::native_scene::FrameScene;
#define REXCVAR_GET(n) (n)
#define REXLOG_INFO(...) ((void)0)
bool skate3_native_render_scene_smooth_entities=true;
bool skate3_native_render_scene_char_track=false;
bool skate3_native_render_scene_lw_identity=true;
bool skate3_native_render_scene_ropa_boxcar=true;
int skate3_native_render_scene_ropa_bias=0;
double skate3_native_render_scene_smooth_camera_filter_ms=50;
double g_latest_cam_tick=0,g_smooth_play=0;
std::unordered_map<uint32_t,uint64_t>g_ropa_last_seq;
constexpr double kMinDynIngestSpacing=0.0035;
struct SkinSampleVert {uint32_t w[4]{},bone[4]{};float p[3]{};};
bool ReadSkinVertGuest(uint8_t*,const DrawItem&,uint32_t,SkinSampleVert*){return false;}
bool BoneSigTick(double){return false;}
void BoneSigAppend(int,uint64_t,double,double,const float*,uint32_t){}
namespace skate3::native_entity {bool ReadEntityWorldRows(uint8_t*,uint32_t,float*){return false;}}
namespace skate3::actor_history {
std::unordered_map<uint32_t,uint32_t>counts;
void Retime(const DrawItem&i,uint64_t,uint32_t n,double,double){counts[i.mesh]=std::max(counts[i.mesh],n);}
}
void serve(DrawItem&item,const float*pick_rows,uint32_t n){
''' + commit + r'''
}
''' + retimer + r'''
int main(int argc,char**argv){
 assert(argc==3);int fps=std::atoi(argv[1]);bool expect_good=std::atoi(argv[2])!=0;
 FrameScene scene;scene.items.resize(7);
 for(int i=0;i<7;++i){
   auto&part=scene.items[i];part={};part.mesh=100+i;part.ctx=1000+i;
   part.skinned=i!=5;part.ropa=i==5;part.char_family=i==6?6:2;
   if(i==6)part.lw_entity=6000;
   part.bones.assign(part.skinned?24:0,0);
   part.draws.push_back({3,0,0,6});
 }
 float previous[7]{};double max_body_garment=0,max_reverse=0;
 for(int frame=0;frame<fps*2;++frame){
   double now=1.+double(frame)/fps;
   // Native player values update60Hz; background traffic updates30Hz.
   float player=float(std::floor((now-1.)*60.+1e-6)/60.*6.);
   float car=float(std::floor((now-1.)*30.+1e-6)/30.*6.);
   g_latest_cam_tick=now;g_smooth_play=now-.03;
   for(int i=0;i<7;++i){
     auto&part=scene.items[i];
     std::fill_n(part.world,16,0.f);part.world[0]=part.world[5]=part.world[10]=part.world[15]=1;
     part.world[12]=player;part.pending=false;part.retained=false;part.caster_bank=false;
     if(part.skinned){
       float rows[24]={1,0,0,i==6?car:player,0,1,0,1,0,0,1,0,
                       1,0,0,i==6?car:player,0,1,0,2,0,0,1,0};
       serve(part,rows,2);assert(part.dbg_src==11);
     }else{part.dbg_src=4;g_ropa_last_seq[part.mesh]=frame+1;}
   }
   InterpolateDynamicItems(nullptr,scene,now);
   for(int i=0;i<7;++i){
     const auto&part=scene.items[i];float x=part.skinned?part.bones[3]:part.world[12];
     assert(part.draws.size()==1);assert(std::isfinite(x));
     if(frame>fps/2)max_reverse=std::max(max_reverse,double(previous[i]-x));
     previous[i]=x;
     if(i<5&&frame>fps/2)max_body_garment=std::max(max_body_garment,double(std::fabs(x-scene.items[5].world[12])));
     if(expect_good&&frame>fps/2){
       assert(player-x<(i==6?.8f:.4f)); // no body frozen at spawn/old ring pose
       assert(x<=player+.01f);
     }
   }
 }
 std::printf("fps=%d body_x=%.3f garment_x=%.3f car_x=%.3f max_body_garment=%.6f reverse=%.6f history=%u\n",
   fps,previous[0],previous[5],previous[6],max_body_garment,max_reverse,skate3::actor_history::counts[100]);
 if(expect_good){assert(max_body_garment<.01);assert(max_reverse<.001);assert(skate3::actor_history::counts[100]>=20);}
 else{assert(previous[0]<.01f);assert(previous[5]>10);assert(max_body_garment>10);}
}
'''


def gap_fixture(scene_text, palette_text):
    return fixture(scene_text, palette_text).split('int main(int argc,char**argv){')[0] + r'''
#include <string>
void scenario(int fps,int kind,bool expect_good){
 skate3_native_render_scene_smooth_entities=false;FrameScene scene;InterpolateDynamicItems(nullptr,scene,0);
 skate3_native_render_scene_smooth_entities=true;scene.world_identity=5;scene.items.resize(1);
 auto& item=scene.items[0];item={};item.mesh=100;item.ctx=1000;
 item.skinned=kind!=1&&kind!=10&&kind!=11;item.ropa=kind==1||kind==11;
 item.char_family=kind==12?6:2;item.lw_entity=kind==12?6000:0;
 item.draws.push_back({3,0,0,6});
 double now=1;float previous=0;bool must_reset=(kind>=2&&kind<=11)||kind>=13;
 float event_raw=0,event_output=0;uint32_t next_history=0;
 for(int f=0;f<fps+8;++f){
  double dt=1./fps;if(f==fps)dt=kind==7?.12:.079289;
  if(kind==14&&f==fps)dt=1./fps; // normal-tick teleport remains a reset
  now+=dt;float x=float((now-1)*24),y=1;
  if(kind==12)x=float(std::floor((now-1)*30+1e-6)/30*24); // traffic updates30Hz
  if(kind==13&&f>=fps-2)item.ctx=1001; // insufficient same-context history
  if(f>=fps){
   if(kind==2)y+=8; // lateral teleport, even across a missing observation
   if(kind==3||kind==14)x+=20; // longitudinal teleport
   if(kind==4)item.ctx=1001;
   if(kind==5)item.lw_entity=7000;
   if(kind==6)scene.world_identity=6;
  }
  if(kind==9)item.ctx=0;
  size_t n=kind==8&&f>=fps?3:2;
  std::fill_n(item.world,16,0.f);item.world[0]=item.world[5]=item.world[10]=item.world[15]=1;
  item.world[12]=x;item.world[13]=y;
  if(kind==11&&f>=fps)item.world[0]=-1;
  item.bones.clear();
  if(item.skinned){item.bones.resize(n*12);for(size_t b=0;b<n;++b){float* p=item.bones.data()+b*12;p[0]=p[5]=p[10]=1;p[3]=x;p[7]=y;}}
  item.dbg_src=11;g_latest_cam_tick=now;g_smooth_play=now-.03;
  skate3::actor_history::counts.clear();InterpolateDynamicItems(nullptr,scene,now);
  float output=item.skinned?item.bones[3]:item.world[12];assert(std::isfinite(output));assert(item.draws.size()==1);
  if(f==fps){event_raw=x;event_output=output;if(must_reset||!expect_good){assert(std::fabs(x-output)<.001);}else{assert(x-output>.5);}}
  if(f==fps+1)next_history=skate3::actor_history::counts[item.mesh];
  if(f>fps&&expect_good&&!must_reset)assert(output>=previous-.001f);
  previous=output;
 }
 assert(must_reset||!expect_good?next_history==1:next_history==24);
 std::printf("fps=%d scenario=%d reset=%d raw=%.6f output=%.6f next_history=%u\n",fps,kind,must_reset||!expect_good,event_raw,event_output,next_history);
}
int main(int argc,char**argv){assert(argc==2);bool fixed=std::atoi(argv[1])!=0;
 for(int fps:{60,144})for(int kind=0;kind<=14;++kind)scenario(fps,kind,fixed);
 std::puts("PASS full retimer: fast skater/garment/vehicle hitch continuity; teleport, context, owner, world, stale, LOD, unknown identity, rigid prop and rotation boundaries");
}
'''


class ActorMotion(unittest.TestCase):
    def test_hitch_continuity_and_discontinuity_boundaries(self):
        scene = (ROOT/'src/skate3_native_scene.cpp').read_text(encoding='utf8')
        palette = (ROOT/'src/native/skate3_native_palette.cpp').read_text(encoding='utf8')
        compiler = os.environ.get('CXX') or shutil.which('clang++') or shutil.which('c++')
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='native-hitch-motion-') as tmp:
            path = Path(tmp)
            source = path/'fixture.cpp'
            source.write_text(gap_fixture(scene, palette), encoding='utf8')
            binary = path/('fixture.exe' if os.name=='nt' else 'fixture')
            subprocess.run([compiler,'-std=c++20','-O2','-I',str(ROOT/'src'),str(source),'-o',str(binary)],check=True)
            subprocess.run([str(binary), '1'], check=True)

    def test_authoritative_player_parts_remain_in_phase_with_garment(self):
        scene = (ROOT/'src/skate3_native_scene.cpp').read_text(encoding='utf8')
        palette = (ROOT/'src/native/skate3_native_palette.cpp').read_text(encoding='utf8')
        compiler = os.environ.get('CXX') or shutil.which('clang++') or shutil.which('c++')
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='native-player-motion-') as tmp:
            path = Path(tmp)
            source = path/'fixture.cpp'
            source.write_text(fixture(scene,palette),encoding='utf8')
            binary = path/('fixture.exe' if os.name=='nt' else 'fixture')
            subprocess.run([compiler,'-std=c++20','-O2','-I',str(ROOT/'src'),str(source),'-o',str(binary)],check=True)
            for fps in (60,144):
                with self.subTest(fps=fps):
                    subprocess.run([str(binary),str(fps),'1'],check=True)


if __name__=='__main__':
    unittest.main()
