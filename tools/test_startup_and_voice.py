"""Exercise production startup hooks and speech gain without launching the game."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function(text, signature):
    start = text.index(signature)
    brace = text.index('{', start)
    depth = 0
    for end in range(brace, len(text)):
        depth += (text[end] == '{') - (text[end] == '}')
        if not depth:
            return text[start:end + 1]
    raise AssertionError(signature)


STARTUP_STUB = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <thread>
int32_t skate3_startup_mode=0;
bool demo=false,probe=false; uint32_t gameplay=0;uint64_t world=0;
int cleared=0,queued=0;uint16_t tapping=0;
int movie_calls=0,title_calls=0,language_calls=0,enter_calls=0;uint32_t transition=0;
#define REXLOG_INFO(...) ((void)0)
#define REXLOG_WARN(...) ((void)0)
#define REXCVAR_GET(n) (n)
int GetAsyncKeyState(int){return 0;}
constexpr int VK_F10=121;
namespace rex::cvar {
template<class T> T Query(const char* n) {
 return T(std::string_view(n)=="skate3_demo_path"?demo:probe);
}}
namespace rex::input {constexpr uint16_t X_INPUT_GAMEPAD_A=0x1000,X_INPUT_GAMEPAD_START=0x10;}
namespace rex::kernel::guest_presence {uint32_t GameplayContextValue(){return gameplay;}}
namespace rex::kernel::xam {
void ClearSyntheticInput(){++cleared;tapping=0;queued=0;}
void QueueSyntheticInput(uint16_t,int){++queued;}
void SetSyntheticAutoTap(uint16_t n,bool on){tapping=on?n:0;}
}
namespace skate3::world_loading {uint64_t RenderWorldIdentity(){return world;}}
union Reg {uint64_t u64;uint32_t u32;};
struct PPCContext {Reg r3{},r4{},r5{};Reg r6{};uint32_t lr=0;};
#define REX_FUNC(n) void n(PPCContext& ctx,uint8_t* base)
void sub_82D0AFA0(PPCContext&,uint8_t*){}
void __imp__sub_82D09998(PPCContext& c,uint8_t*){++enter_calls;c.r4.u32=0;}
void sub_826FDD70(PPCContext&,uint8_t*){++language_calls;}
void sub_82639400(PPCContext&,uint8_t*){}
void sub_826FE1D8(PPCContext&,uint8_t*){++title_calls;}
void sub_826FDE48(PPCContext&,uint8_t*){++movie_calls;}
void sub_826DFB30(PPCContext& c,uint8_t*){transition=c.r4.u32;assert(c.r5.u32==1&&c.r6.u32==0);}
namespace rex::runtime {
struct FunctionDispatcher {int installs=0;template<class T>void SetFunction(uint32_t,T){++installs;}};
}
'''


class StartupAndVoice(unittest.TestCase):
    def compile_run(self, source, *args):
        compiler = (os.environ.get('CXX') or shutil.which('clang++-20')
                    or shutil.which('clang++') or shutil.which('c++'))
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix="Skate3 startup user's ") as tmp:
            work = Path(tmp)
            p = work / 'fixture.cpp'
            p.write_text(source, encoding='utf8')
            binary = work / ('fixture.exe' if os.name == 'nt' else 'fixture')
            subprocess.run([compiler, '-std=c++20', '-O2', str(p), '-o', str(binary)], check=True)
            subprocess.run([str(binary), *map(str, args)], check=True)

    def test_startup_hooks(self):
        text = (ROOT / 'src/skate3_demo_path.cpp').read_text(encoding='utf8')
        # Compile actual hook/helper bodies, including native movie entry and the
        # frozen boot setting; no guest functions or input system are run.
        start = text.index('constexpr uint32_t kFrontEndStatePressStart')
        end = text.index('// Scripted post-gameplay input sequence', start)
        code = STARTUP_STUB.replace('#include <limits>', '#include <limits>\n#include <string_view>')
        code += '\nnamespace skate3::demo_path { namespace {\n' + text[start:end]
        code += '\nvoid StartGameplayInputWorkerIfNeeded(){}\nbool UserRequestedMovieSkip(){return false;}\n}\n'
        for signature in ('void InstallHooks(', 'void UpdateStartup()', 'bool ShouldForceIntroMovieComplete()'):
            code += function(text, signature) + '\n'
        code += '\n}\n'
        code += r'''
int main(int argc,char** argv){
 using namespace skate3::demo_path;
 assert(argc==2);int mode=std::atoi(argv[1]);skate3_startup_mode=mode;
 rex::runtime::FunctionDispatcher d;InstallHooks(&d);
 const bool assist=mode>0;
 assert(d.installs==(assist?4:0));
 assert(g_startup_mode==int(assist)); // migrate removed Skip2 to safe Auto1
 skate3_startup_mode=assist?0:1; // preference frozen until next process
 PPCContext c;
 // Arm from actual native-playing polls, with NO BootFlow callback at all.
 assert(!ShouldForceIntroMovieComplete());
 std::this_thread::sleep_for(std::chrono::milliseconds(120));
 assert(ShouldForceIntroMovieComplete()==assist);
 c.r4.u64=1;Skate3DemoPath_LanguageSelectUpdateHook(c,nullptr);
 assert(queued==int(assist));
 Skate3DemoPath_LanguageSelectUpdateHook(c,nullptr);assert(queued==int(assist));
 c.r4.u64=0;Skate3DemoPath_ShowPressStartModeHook(c,nullptr);
 c.r4.u64=1;Skate3DemoPath_ShowPressStartModeHook(c,nullptr);assert(tapping==0);
 g_title_entered-=std::chrono::seconds(1);
 Skate3DemoPath_ShowPressStartModeHook(c,nullptr);assert(tapping==(assist?0x1010:0));
 world=2;UpdateStartup();world=0;UpdateStartup();assert(tapping==0);
 assert(g_startup_finished==assist);
 gameplay=1;assert(!StartupAutomationEnabled());gameplay=0;
 Skate3DemoPath_ShowPressStartModeHook(c,nullptr);assert(tapping==0);
 assert(!ShouldForceIntroMovieComplete()); // later movies/pause/load unaffected
 std::puts("PASS production startup: Default/Auto, legacy Skip migration, actual-playing movie polls,100ms completion,750ms title delay, no later rearm");
}
'''
        for mode in range(3):
            with self.subTest(mode=mode):
                self.compile_run(code, mode)

    def test_native_voice_gain(self):
        text = (ROOT / 'src/skate3_pedestrian_speech.cpp').read_text(encoding='utf8')
        code = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
double skate3_pedestrian_voice_volume=1;
#define REXCVAR_GET(n) (n)
std::atomic<uint32_t> instance{4096};constexpr uint32_t kSpeechChannel=1;
uint32_t Read(uint8_t* base,uint32_t at){return uint32_t(base[at])<<24|uint32_t(base[at+1])<<16|uint32_t(base[at+2])<<8|base[at+3];}
void write(uint8_t* base,uint32_t at,uint32_t value){for(int i=3;i>=0;--i){base[at+i]=uint8_t(value);value>>=8;}}
namespace skate3::pedestrian_speech {float ScaleVoiceGain(uint8_t*,uint32_t,uint32_t,float);}
'''
        code += function(text, 'float skate3::pedestrian_speech::ScaleVoiceGain(')
        code += r'''
int main(){
 using skate3::pedestrian_speech::ScaleVoiceGain;
 uint8_t memory[8192]={};constexpr uint32_t retail=1024,nsv=4096;
 for(uint32_t inst:{retail,nsv})for(uint32_t channel:{0,1})for(uint32_t bank:{0,1,2,3,4}){
  write(memory,inst+0x428+channel*4,bank);
  bool ped=bank==1||(bank==4&&inst==nsv&&channel==1);
  for(double volume:{0.,.2,1.,2.,-1.,3.}){
   skate3_pedestrian_voice_volume=volume;
   float gain=ScaleVoiceGain(memory,inst,channel,.2f);
   float expected=ped?.2f*float(std::clamp(volume,0.,3.)):.2f;
   assert(std::abs(gain-expected)<1e-7);
   assert(ScaleVoiceGain(memory,inst,channel,0)==0); // native mute retained
   assert(Read(memory,inst+0x428+channel*4)==bank); // no guest writes
  }
 }
 skate3_pedestrian_voice_volume=NAN;assert(ScaleVoiceGain(memory,nsv,1,.2f)==.2f);
 assert(ScaleVoiceGain(memory,nsv,2,.2f)==.2f);
 std::puts("PASS production speech gain: retail living-world and private NSV bank, default unity, live mute/scale/clamp, other dialogue unchanged, no guest writes");
}
'''
        self.compile_run(code)

    def test_codegen_patch_guards(self):
        spec = importlib.util.spec_from_file_location('ped_patch', ROOT / 'cmake/PatchPedestrianVolume.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'skate3_recomp.fixture.cpp'
            original = ('#include "skate3_init.h"\r\nDEFINE_REX_FUNC(sub_82C5CEF0) {\r\n'
                        'ctx.f1.f64 = double(float(ctx.f10.f64 * ctx.f0.f64));\r\n'
                        'loc_82C5CFD8:\r\n}\r\n')
            p.write_bytes(original.encode())
            module.patch(Path(tmp))
            first = p.read_bytes()
            module.patch(Path(tmp))
            self.assertEqual(first, p.read_bytes())
            self.assertEqual(first.count(b'\n'), first.count(b'\r\n'))
            p.write_bytes(original.replace('loc_82C5CFD8:', 'loc_changed:').encode())
            with self.assertRaises(RuntimeError):
                module.patch(Path(tmp))
            self.assertEqual(p.read_bytes(), original.replace('loc_82C5CFD8:', 'loc_changed:').encode())

    def test_first_gameplay_input_cleanup(self):
        text = (ROOT / 'third_party/rexglue-sdk/src/kernel/xam/apps/xgi_app.cpp').read_text(encoding='utf8')
        start = text.index('      static const bool startup_assist =')
        end = text.index('      auto old_value =', start)
        code = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdlib>
int mode=0,clears=0;bool input=false;
namespace rex::cvar {template<class T>T Query(const char*){return T(mode);}}
namespace rex::kernel::xam {void ClearSyntheticInput(){++clears;input=false;}}
void notify(uint32_t user_index,uint32_t context_id,uint32_t context_value){
'''+text[start:end]+r'''
}
int main(int argc,char** argv){
 assert(argc==2);mode=std::atoi(argv[1]);const bool assist=mode>0;
 notify(0,0x8001,0);assert(clears==0); // never arms during intro, loading or pause
 mode=2-mode; // later preference changes cannot change this boot's policy
 input=true;notify(1,0x8001,1);assert(clears==0); // other user
 notify(0,0x8002,1);assert(clears==0); // other context
 notify(0,0x8001,1);assert(clears==int(assist));assert(input==!assist);
 notify(0,0x8001,0);notify(0,0x8001,1);assert(clears==int(assist));
}
'''
        for mode in range(3):
            self.compile_run(code, mode)


if __name__ == '__main__':
    unittest.main()
