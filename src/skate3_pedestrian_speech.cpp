#include "generated/skate3_init.h"
#include "native/skate3_native_guest_read.h"
#include "skate3_pedestrian_speech.h"
#include "skate3_world_loading.h"
#include <rex/cvar.h>
#include <rex/logging.h>
#include <array>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <fstream>
#include <mutex>
#include <cstdio>
#include <string>
#include <string_view>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

REXCVAR_DEFINE_DOUBLE(skate3_pedestrian_voice_volume, 1.0, "Skate 3",
                      "Pedestrian voice gain in every map (1 = original mix, 0 mute); "
                      "preserves native distance attenuation and line tuning")
    .range(0.0, 3.0)
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DEFINE_BOOL(skate3_skate2_pedestrian_speech, true, "Skate 3",
    "Enable prepared original New San Van pedestrian voices; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_speech_log, false, "Skate 3",
    "Log New San Van pedestrian speech diagnostics (dispatches, voice state, stream queues, "
    "readers and decoded output); off keeps only the startup status lines");
REXCVAR_DEFINE_BOOL(skate3_skate2_voice_records, true, "Skate 3",
    "New San Van: give the Skate 2 voices Skate 3 has no speaker settings for (the security "
    "guards' secg4/secg5, adult_male03's adtm4) the settings of the same kind of Skate 3 voice "
    "(guard secg1, adult male adtm1), so those peds speak in their own Skate 2 voice. Off: "
    "they stay silent; restart required");
REXCVAR_DECLARE(bool, skate3_skate2_livingworld_probe);
namespace {
}  // namespace
namespace {
// Speech diagnostics (builds 141-147) stay in place behind this switch; with
// it off the purely diagnostic hooks only call through.
bool SpeechLog() { return REXCVAR_GET(skate3_skate2_speech_log); }
constexpr uint32_t kManager = 0x830CFDDC;
constexpr uint32_t kRegistry = 0x8306FE20;
// Playback bank IDs index policy; namespaces identify event programs and are
// independent (native cameraman: bank2, namespace4). Use a private namespace,
// checked against every registered event bank before relocation/registration.
constexpr uint32_t kPreferredNamespace = 0x4E53;
// 824A7F38 sets each speech channel's voice chain when 82C5BC40 builds the
// instances: instance 0 (maincast/living world) gets type 2 DistanceBased on
// both channels; instance 2 gets type 1 6Channel (cinematic) on channel 0 and
// type 3 (no player) on channel 1. Original pedestrian voices use instance 2
// channel 1 with the native pedestrian type-2 chain instead.
constexpr uint32_t kSpeechChannel = 1;
constexpr uint32_t kDistanceBased = 2;
std::atomic<uint32_t> event_namespace{0};
// Prepared identities are immutable after Install, before guest execution.
std::unordered_map<uint64_t, uint32_t> voices;
std::unordered_set<uint32_t> events, sample_ids;
uint32_t event_bytes = 0;
constexpr uint32_t kSampleBias = 0x8000;
std::atomic<uint32_t> instance{0};
bool prepared = false;
thread_local bool loading = false;
thread_local uint32_t loading_event = 0;
thread_local std::vector<uint64_t>* census = nullptr;
thread_local bool original_sfx = false;
// The speaking actor's Skate 2 voice ID while original_sfx (0 = unknown).
thread_local uint32_t original_voice = 0;
std::mutex owners_mutex;
// Bridge handle -> voice, and speech object -> voice of its actor.
std::unordered_map<uint32_t, uint32_t> handles, owners;
std::atomic<uint32_t> dispatches{0}, requests{0}, starts{0}, bridges{0};
uint32_t Read(uint8_t* base, uint32_t address) { return REX_LOAD_U32(address); }
struct Scope {
  bool previous = original_sfx;
  uint32_t previous_voice = original_voice;
  explicit Scope(bool value, uint32_t voice = 0) { original_sfx = value; original_voice = voice; }
  ~Scope() { original_sfx = previous; original_voice = previous_voice; }
};
// The engine owns this allocation through manager+0x14 and frees it during
// normal speech shutdown. Preserve its allocator and all existing entries.
bool ReserveSamples(PPCContext& ctx, uint8_t* base, uint32_t manager) {
  const uint32_t old = Read(base,0x8308432C);
  const uint32_t capacity = Read(base,0x83084334), used = Read(base,0x83084338);
  if (!old || Read(base,manager+0x14)!=old || used>capacity ||
      capacity+sample_ids.size()>=0x7FFF) return false;
  const uint32_t first_private=kSampleBias+*std::min_element(sample_ids.begin(),sample_ids.end());
  for(uint32_t i=0;i<used;++i) {
    const uint32_t header=Read(base,old+i*8+4);
    const uint32_t id=REX_LOAD_U16(header);
    if(id>=first_private) {
      REXLOG_ERROR("skate2-speech: private sample range unavailable {:04X}",id); return false;
    }
  }
  const uint32_t desired=capacity+uint32_t(sample_ids.size());
  auto saved=ctx;
  // Same default allocator used by native speech teardown (82C5EE18).
  sub_828AAF88(ctx,base);
  const uint32_t allocator=ctx.r3.u32;
  ctx=saved;
  if(!allocator) return false;
  const uint32_t vtable=Read(base,allocator);
  ctx.r3.u64=allocator; ctx.r4.u64=desired*8;
  ctx.r5.u64=0x820BA90C; ctx.r6.u64=0x01000001;
  REX_CALL_INDIRECT_FUNC(Read(base,vtable+8));
  const uint32_t replacement=ctx.r3.u32;
  ctx=saved;
  if(!replacement) return false;
  std::memset(base+replacement,0,desired*8);
  std::memcpy(base+replacement,base+old,used*8);
  REX_STORE_U32(manager+0x14,replacement);
  REX_STORE_U32(0x8308432C,replacement);
  REX_STORE_U32(0x83084334,desired);
  ctx.r3.u64=allocator; ctx.r4.u64=old; ctx.r5.u64=0;
  REX_CALL_INDIRECT_FUNC(Read(base,vtable+0xC));
  ctx=saved;
  REXLOG_INFO("skate2-speech: sample registry reserved used={} capacity={} -> {}",used,capacity,desired);
  return true;
}
bool Owns(uint32_t sfx) {
  if (!instance.load()) return false;
  std::lock_guard lock(owners_mutex);
  return owners.contains(sfx);
}
// Voice of an owned speech object (0 = not ours).
uint32_t OwnerVoice(uint32_t sfx) {
  if (!instance.load()) return 0;
  std::lock_guard lock(owners_mutex);
  const auto it = owners.find(sfx);
  return it == owners.end() ? 0 : it->second;
}
// Speech streams open "<archive>|<entry>" paths, which the BigFile handler
// 82A83808 resolves only against mounted archives. Retail speech archives are
// mounted by 824A4618 into the manager's list at +0x1BDA4 (node: next, prev,
// record; record: path, holder at +0x100 whose first word is the mount handle).
// The manager destructor 824A4C70 unmounts and frees every listed archive.
constexpr char kSampleArchive[] = "audio/nsv_s2_speech/samples.big";
std::atomic<bool> archive_mounted{false};
// Per-channel speech flags (instance + channel): +0x426 playing, +0x470
// pending start, +0x472 held-start mode, +0x474 held, +0x476 and +0x478
// completion hold. 82C5CC90 updates a playing channel only when not held.
uint32_t ChannelState(uint8_t* base, uint32_t voice_instance, uint32_t channel) {
  uint32_t state=0;
  for(const uint32_t offset : {0x426u,0x470u,0x472u,0x474u,0x476u,0x478u})
    state=(state<<4) | (base[voice_instance+offset+channel] ? 1u : 0u);
  return state;
}
uint32_t MountedSampleArchive(uint8_t* base, uint32_t manager) {
  const uint32_t head = manager + 0x1BDA4;
  uint32_t node = Read(base, head);
  for (uint32_t n = 0; node && node != head && n < 64; node = Read(base, node), ++n) {
    const uint32_t record = Read(base, node + 8);
    if (!record || std::strncmp(reinterpret_cast<const char*>(base + record), kSampleArchive,
                                sizeof(kSampleArchive))) continue;
    const uint32_t holder = Read(base, record + 0x100);
    return holder ? Read(base, holder) : 0;
  }
  return 0;
}
uint32_t GuardedWord(uint8_t* base, uint32_t address) {
  uint32_t value=0;
  if(address<0x10000 || address>0xFFFFFFFB ||
     !skate3::native_scene::GuestTryCopy(&value,base+address,sizeof(value))) return 0xDEADDEAD;
  return __builtin_bswap32(value);
}
uint8_t GuardedByte(uint8_t* base, uint32_t address) {
  uint8_t value=0xEE;
  if(address<0x10000 || !skate3::native_scene::GuestTryCopy(&value,base+address,1)) return 0xEE;
  return value;
}
float GuardedFloat(uint8_t* base, uint32_t address) {
  const uint32_t bits=GuardedWord(base,address);
  float value;
  std::memcpy(&value,&bits,sizeof(value));
  return value;
}
double GuardedDouble(uint8_t* base, uint32_t address) {
  const uint64_t bits=(uint64_t(GuardedWord(base,address))<<32) | GuardedWord(base,address+4);
  double value;
  std::memcpy(&value,&bits,sizeof(value));
  return value;
}
// SndPlayer1 plugin state for a speech channel. The channel's SnP1 module
// (modules[slot]) points at +0x10 to the state's attribute block at +0x30
// (tag 7FF7FFF1, current request handle, position, length), which the
// 82B31EE0 service writes and the voice-frame sampler reads.
uint32_t PlayerState(uint8_t* base, uint32_t voice_instance, uint32_t channel) {
  const uint32_t modules=GuardedWord(base,voice_instance+0x430+channel*4);
  const uint32_t slot=GuardedWord(base,voice_instance+channel*0x2C);
  if(!modules || modules==0xDEADDEAD || slot>=16) return 0;
  const uint32_t attributes=GuardedWord(base,GuardedWord(base,modules+slot*4)+0x10);
  if(attributes<0x10030 || attributes==0xDEADDEAD || GuardedWord(base,attributes)!=0x7FF7FFF1) return 0;
  return attributes-0x30;
}
// Stream players traced in New San Van: [0] original voices (instance 2
// channel 1); [1]/[2] retail instance 0 channels 0/1 (AI skaters), the
// working baseline. Refreshed by the speech update hook.
std::array<std::atomic<uint32_t>,3> traced_players{};
int TracedPlayer(uint32_t state) {
  if(!state) return -1;
  for(int i=0;i<3;++i) if(traced_players[i].load(std::memory_order_relaxed)==state) return i;
  return -1;
}
const char* TracedName(int traced) { return traced==0 ? "original" : traced==1 ? "native0" : "native1"; }
// Stream readers (82EC05C0) of the three 'Spch' stream records.
std::array<std::atomic<uint32_t>,3> traced_readers{};
int TracedIndex(const std::array<std::atomic<uint32_t>,3>& values, uint32_t value) {
  if(!value) return -1;
  for(int i=0;i<3;++i) if(values[i].load(std::memory_order_relaxed)==value) return i;
  return -1;
}
}

void skate3::pedestrian_speech::Install(const std::filesystem::path& folder) {
  if (!REXCVAR_GET(skate3_skate2_pedestrian_speech)) return;
  std::ifstream v(folder / "pedestrian_voices.txt"), e(folder / "speech_events.txt");
  std::string magic;
  uint32_t count = 0;
  std::unordered_map<uint64_t, uint32_t> parsed_voices;
  std::unordered_set<uint32_t> parsed_events;
  if (!(v >> magic >> count) || magic != "NSV_PED_VOICES_V1" || count != 38) return;
  for (uint32_t i=0;i<count;++i) {
    uint64_t key; uint32_t voice;
    if (!(v >> std::hex >> key >> std::dec >> voice) || !key ||
        (voice > 100 && voice != 104) || !parsed_voices.emplace(key,voice).second) return;
  }
  if (v >> magic) return;
  if (!(e >> magic >> count) || magic != "NSV_SPEECH_EVENTS_V1" || count != 47) return;
  for (uint32_t i=0;i<count;++i) {
    uint32_t event;
    if (!(e >> std::hex >> event) || event < 0x2000 || event >= 0x2080 ||
        !parsed_events.insert(event).second) return;
  }
  if (e >> magic) return;
  std::ifstream samples(folder / "speech_samples.txt");
  uint32_t size=0;
  std::unordered_set<uint32_t> parsed_samples;
  if(!(samples >> magic >> size >> count) || magic!="NSV_SPEECH_SAMPLES_V1" ||
     size<24 || size>1048576 || !count || count>8192) {
    REXLOG_ERROR("skate2-speech: compatible sample tables missing; rerun preparation"); return;
  }
  for(uint32_t i=0;i<count;++i) {
    uint32_t id;
    if(!(samples >> std::hex >> id) || id>=kSampleBias || !parsed_samples.insert(id).second) return;
  }
  if(samples >> magic) return;
  for(const auto* name : {"livingworldhdr.big","livingworldsth.big","samples.big"}) {
    std::ifstream file(folder / "loose/data/audio/nsv_s2_speech" / name,std::ios::binary);
    std::array<char,4> signature{};
    if(!file.read(signature.data(),4) || std::memcmp(signature.data(),"EB\0\3",4)) {
      REXLOG_ERROR("skate2-speech: incompatible sample table {}; rerun preparation",name); return;
    }
  }
  sample_ids=std::move(parsed_samples); event_bytes=size;
  voices = std::move(parsed_voices); events = std::move(parsed_events); prepared = true;
  REXLOG_INFO("skate2-speech: prepared voices={} shared_events={} (namespace assigned at registration)", voices.size(), events.size());
}

// The first two instances already contain the four native banks. The engine
// allocates, updates and destroys the third instance too; reserve its empty
// bank slots rather than replacing native banks or growing guest structures.
extern "C" REX_FUNC(sub_82487B98) {
  __imp__sub_82487B98(ctx, base);
  if (!prepared || instance.load()) return;
  const uint32_t manager = Read(base,kManager);
  if (!manager) return;
  const uint32_t target = Read(base,manager+0x10);
  if (!target || base[target+0x58] || base[target+0x124]) {
    REXLOG_ERROR("skate2-speech: third speech instance is unavailable"); return;
  }
  // 82C5C318 stores the channel type at +0x1F0 and the built patch at +0x438.
  if (Read(base,target+0x1F0+kSpeechChannel*4)!=kDistanceBased ||
      !Read(base,target+0x438+kSpeechChannel*4)) {
    REXLOG_ERROR("skate2-speech: pedestrian voice channel was not built; original voices remain silent"); return;
  }
  bool free = false;
  std::unordered_set<uint32_t> occupied;
  for (uint32_t i=0;i<8;++i) {
    const uint32_t bank = Read(base,kRegistry+i*8);
    if (!bank) { free=true; continue; }
    const uint32_t ns=uint32_t(base[bank+8]) | (uint32_t(base[bank+9])<<8);
    const uint32_t id=Read(base,kRegistry+i*8+4);
    occupied.insert(ns);
    REXLOG_INFO("skate2-speech: existing registry slot={} bank={} namespace={:04X}",i,id,ns);
    if(id==4) { REXLOG_ERROR("skate2-speech: playback bank 4 is occupied"); return; }
  }
  if (!free) { REXLOG_ERROR("skate2-speech: event registry is full"); return; }
  uint32_t selected=kPreferredNamespace;
  while(occupied.contains(selected)) ++selected; // At most eight occupied IDs.
  event_namespace.store(selected);
  auto call = ctx;
  const uint32_t frame = (ctx.r1.u32-512)&~15u;
  REX_STORE_U32(frame,ctx.r1.u32);
  constexpr char prefix[] = "data/audio/nsv_s2_speech/livingworld";
  std::memcpy(base+frame+128,prefix,sizeof(prefix));
  std::memcpy(base+frame+256,kSampleArchive,sizeof(kSampleArchive));
  call.r1.u64=frame; call.r3.u64=target; call.r4.u64=4;
  call.r5.u64=frame+128; call.r6.u64=frame+256;
  // The bank's stream paths are data\<this path>|<entry>.dat; mount it exactly
  // as 824A4488 mounts the retail archives, before any request exists.
  auto mount=call;
  mount.r3.u64=manager; mount.r4.u64=frame+256;
  sub_824A4618(mount,base);
  const uint32_t archive=MountedSampleArchive(base,manager);
  if(!archive) {
    REXLOG_ERROR("skate2-speech: sample archive mount failed; original voices remain silent"); return;
  }
  archive_mounted.store(true);
  REXLOG_INFO("skate2-speech: sample archive mounted handle={:08X}",archive);
  if(!ReserveSamples(call,base,manager)) {
    REXLOG_ERROR("skate2-speech: unable to reserve original sample registry"); return;
  }
  loading_event=0;
  loading=true;
  sub_82C5BD60(call,base);
  loading=false;
  const uint32_t evt=Read(base,target+0x60);
  bool registered=false;
  for (uint32_t i=0;i<8;++i)
    registered |= evt && Read(base,kRegistry+i*8)==evt && Read(base,kRegistry+i*8+4)==4;
  const uint32_t table=Read(base,target+0x1FC), count=Read(base,target+0x200);
  bool samples_registered=table && count==sample_ids.size();
  for(uint32_t i=0;samples_registered && i<count;++i)
    samples_registered=Read(base,table+i*16+12)!=0xFFFFFFFFu;
  if (base[target+0x58] && Read(base,target+0x5C)==4 && registered && samples_registered) {
    instance.store(target);
    REXLOG_INFO("skate2-speech: original bank loaded instance=2 bank=4 namespace={:04X} samples={}",selected,count);
  } else REXLOG_ERROR("skate2-speech: bank registration failed; original voices remain silent");
}

// Relocate the unregistered in-memory header before the native registry sees
// it. Installed data stays immutable, including older prepared namespace4 files.
extern "C" REX_FUNC(sub_82EACA58) {
  if(loading && ctx.r4.u32==4) {
    loading_event=ctx.r3.u32;
    const uint32_t selected=event_namespace.load();
    base[ctx.r3.u32+8]=uint8_t(selected);
    base[ctx.r3.u32+9]=uint8_t(selected>>8);
  }
  __imp__sub_82EACA58(ctx,base);
}

// Sample descriptors live in their event bank. Relocate only the four-byte
// descriptor passed by the original bank; never mutate shared native programs.
// 82972380 consumes exactly these four bytes (ID, argument index, lookup mode).
extern "C" REX_FUNC(sub_82972380) {
  const uint32_t target=instance.load();
  const uint32_t evt=target ? Read(base,target+0x60) : 0;
  const uint32_t descriptor=ctx.r3.u32;
  if(evt && descriptor>=evt && descriptor-evt<=event_bytes-4 &&
     sample_ids.contains(REX_LOAD_U16(descriptor))) {
    const uint32_t saved_stack=ctx.r1.u32;
    const uint32_t frame=(saved_stack-128)&~15u;
    REX_STORE_U32(frame,saved_stack);
    REX_STORE_U32(frame+64,Read(base,descriptor)+(kSampleBias<<16));
    const uint32_t result=ctx.r5.u32, source_id=REX_LOAD_U16(descriptor);
    ctx.r1.u64=frame; ctx.r3.u64=frame+64;
    __imp__sub_82972380(ctx,base);
    ctx.r1.u64=saved_stack;
    if(SpeechLog()) {
      static std::atomic<uint32_t> selections{0};
      const auto n=++selections;
      if(n<=12 || !(n%128))
        REXLOG_INFO("skate2-speech: sample selection={} source={:04X} resolved={} sample={:08X}",
                    n,source_id,ctx.r3.u32!=0,Read(base,result));
    }
    return;
  }
  __imp__sub_82972380(ctx,base);
}

// Numeric events are enough. Registering the original named callbacks would
// overwrite native names/CSI bindings despite assigning a separate namespace.
extern "C" REX_FUNC(sub_82EACC30) {
  if (loading && ctx.r3.u32==loading_event) return;
  __imp__sub_82EACC30(ctx,base);
}
extern "C" REX_FUNC(sub_82C5C1C0) {
  if (ctx.r3.u32==instance.load()) {
    instance.store(0);
    std::lock_guard lock(owners_mutex); handles.clear(); owners.clear();
  }
  __imp__sub_82C5C1C0(ctx,base);
}

// The bridge emits one packed record per model lookup, in the same order.
// Substitute only prepared model identities, retaining the flag at bit 14.
// Voice IDs stay NONE in Attrib so missing/disabled speech is safely silent.
// Per-voice speaker records (log 354/355 trace). Each speaker's info gets its
// settings from the audio manager's per-voice table (manager + 0x1BE78, 104
// u64 record keys; sub_824F91B0: info +84 voice -> key -> record class
// D3B2C1F36DA3B433 -> info +88..+132). +116 picks the route: set = the
// living-world request (sub_824ABA18), 0 = the cast/skater family
// (sub_824AC560), where living-world lines never play. sub_824A9778 fills the
// table at boot from the records under 'default' (first layout word = voice).
// Skate 3 has records for 84 voices but none for 44 (adtm4), 78 (secg4) or 79
// (secg5): the guards and adult_male03 asked for lines (logs 354-355: shove
// lines 206C/206D) on the wrong route and stayed silent; every voice with a
// record spoke. Give each the record of the same kind of voice; the speaker
// keeps its own voice number, so its lines still come from Skate 2's bank.
namespace {
constexpr uint32_t kVoiceTable = 0x1BE78;
struct VoiceRecordBorrow {
  uint32_t voice, from;
  const char* what;
};
constexpr VoiceRecordBorrow kVoiceRecordBorrows[] = {
    {44, 41, "adtm4 <- adtm1"}, {78, 75, "secg4 <- secg1"}, {79, 75, "secg5 <- secg1"}};
}  // namespace
extern "C" REX_FUNC(sub_824A9778) {  // fill the per-voice record table (audio manager r3)
  const uint32_t manager=ctx.r3.u32;
  __imp__sub_824A9778(ctx,base);
  if(!prepared || !REXCVAR_GET(skate3_skate2_voice_records)) return;
  std::string done;
  for(const auto& b : kVoiceRecordBorrows) {
    const uint32_t slot=manager+kVoiceTable+b.voice*8, from=manager+kVoiceTable+b.from*8;
    const uint32_t hi=Read(base,from), lo=Read(base,from+4);
    if((Read(base,slot) || Read(base,slot+4)) || !(hi || lo)) continue;  // own record, or none to borrow
    REX_STORE_U32(slot,hi);
    REX_STORE_U32(slot+4,lo);
    done+=std::string(done.empty() ? "" : ", ")+std::to_string(b.voice)+" ("+b.what+")";
  }
  REXLOG_INFO("skate2-speech: speaker records for Skate 2 voices Skate 3 lacks: {}",done.empty() ? "none needed" : done);
}
extern "C" REX_FUNC(sub_8269A6E8) {
  if (census) census->push_back(ctx.r4.u64);
  __imp__sub_8269A6E8(ctx,base);
}
extern "C" REX_FUNC(sub_827A3978) {
  const bool active=instance.load() && skate3::world_loading::OriginalPedestriansActive();
  const bool probe=prepared && REXCVAR_GET(skate3_skate2_livingworld_probe) && SpeechLog();
  if (!active && !probe) {
    __imp__sub_827A3978(ctx,base);
    if (prepared) { std::lock_guard lock(owners_mutex); handles.clear(); }
    return;
  }
  const uint32_t output=ctx.r3.u32;
  std::vector<uint64_t> models;
  models.reserve(30);
  auto* previous=census; census=&models;
  __imp__sub_827A3978(ctx,base);
  census=previous;
  std::unordered_map<uint32_t, uint32_t> current;
  if (probe && ctx.r3.u32<=30 && ctx.r3.u32==models.size()) {
    uint32_t imported=0;
    for(const auto key:models) imported+=voices.contains(key);
    const bool bam=skate3::world_loading::OriginalPedestriansActive();
    static std::array<std::atomic<uint32_t>,2> snapshots{};
    const auto n=++snapshots[bam ? 1 : 0];
    if(n<=4 || !(n%1024))
      REXLOG_INFO("skate2-speech: pedestrian identities world={} snapshot={} nearby={} imported={} other={}",
                  bam ? "BAM" : "retail",n,models.size(),imported,models.size()-imported);
  }
  if (active && ctx.r3.u32<=30 && ctx.r3.u32==models.size() && SpeechLog()) {
    // Log 351: no guard (voice 78) or jock (54-56) ever asked for a line,
    // while 15 other voices did. Name each nearby model once: voiced or not.
    static std::mutex seen_mutex;
    static std::unordered_set<uint64_t> seen;
    std::lock_guard seen_lock(seen_mutex);
    for(uint32_t i=0;i<ctx.r3.u32;++i) {
      if(seen.size()>=96 || !seen.insert(models[i]).second) continue;
      const auto found=voices.find(models[i]);
      if(found==voices.end())
        REXLOG_INFO("skate2-speech: nearby ped model {:016X}: no prepared Skate 2 voice (speaks with its own, none)",
                    models[i]);
      else
        REXLOG_INFO("skate2-speech: nearby ped model {:016X}: Skate 2 voice {}",models[i],found->second);
    }
  }
  if (active && ctx.r3.u32<=30 && ctx.r3.u32==models.size()) {
    for(uint32_t i=0;i<ctx.r3.u32;++i) {
      auto found=voices.find(models[i]);
      uint32_t voice=found==voices.end() ? 0 : found->second;
      // Model group keys (the variants' parents) in case a guard or jock is
      // reported by its group: the variants' own Skate 2 voices (78 for all
      // three guard models; jocks 54-56, joc2 has the chase lines).
      if(!voice) {
        switch(models[i]) {
          case 0xD5BF47AD432BCAECull: case 0x271BBCF7D7B1A37Cull: voice=78; break;  // security_street / _guard
          case 0xCE9A4D3E7A57ED03ull: voice=55; break;                               // jock
          default: break;
        }
      }
      if(!voice || voice==104) continue;
      const uint32_t row=output+i*20;
      REX_STORE_U32(row,(Read(base,row)&~(0x7Fu<<7))|(voice<<7));
      current[Read(base,row+12)]=voice;
    }
  }
  if (!current.empty()) {
    const auto n=++bridges;
    if(SpeechLog() && (n<=4 || !(n%1024)))
      REXLOG_INFO("skate2-speech: bridge={} original_speakers={}",n,current.size());
  }
  std::lock_guard lock(owners_mutex); handles=std::move(current);
}
namespace {
// Speech path trace (skate3_skate2_speech_log, New San Van). Log 353: voices
// 44 and 78 never reached the dispatcher while every other voice did. Each
// step a line passes is logged once per (step, voice, event): the speaker
// object's activation (sub_824D9248), its speak methods (824D9370, 824D9908,
// 824D92B0, 824D9AD8, 824D9DB8), the request gates (sub_824ABA18 /
// sub_824AC560) and whether they reached the dispatch switch sub_824ABD90.
bool TraceSpeech() {
  return SpeechLog() && instance.load() && skate3::world_loading::OriginalPedestriansActive();
}
void TraceStep(const char* step, uint32_t voice, uint32_t value, const std::string& detail) {
  static std::mutex mutex;
  static std::unordered_set<std::string> seen;
  const std::string key=std::string(step)+"/"+std::to_string(voice)+"/"+std::to_string(value);
  std::lock_guard lock(mutex);
  if(seen.size()>=400 || !seen.insert(key).second) return;
  REXLOG_INFO("skate2-speech: trace {} voice {}: {}",step,voice ? std::to_string(voice) : std::string("none"),detail);
}
thread_local bool t_reached_switch=false;
}  // namespace
extern "C" REX_FUNC(sub_824D9248) {
  const uint32_t object=ctx.r3.u32;
  __imp__sub_824D9248(ctx,base);
  const uint32_t handle=Read(base,object+0x2C);
  uint32_t voice=0;
  {
    std::lock_guard lock(owners_mutex);
    owners.erase(object);
    if(const auto it=handles.find(handle); it!=handles.end()) owners[object]=voice=it->second;
  }
  if(TraceSpeech()) {
    char text[96];
    std::snprintf(text,sizeof(text),"speaker object %08X activated (handle %08X, +48 %08X)",object,handle,
                  Read(base,object+0x30));
    TraceStep("activate",voice,0,text);
  }
}
// Ownership survives map changes until native teardown/reset. Never classify
// a cancellation solely from the currently displayed world.
#define NSV_SPEECH_SCOPE(address) \
extern "C" REX_FUNC(sub_##address) { \
  const uint32_t voice=OwnerVoice(ctx.r3.u32); \
  if(TraceSpeech()) { \
    char text[96]; \
    std::snprintf(text,sizeof(text),"speaker %08X method " #address " r4 %08X r10 %08X", \
                  ctx.r3.u32,ctx.r4.u32,ctx.r10.u32); \
    TraceStep(#address,voice,ctx.r4.u32&0xFFFF,text); \
  } \
  Scope scope(voice!=0,voice); __imp__sub_##address(ctx,base); \
}
NSV_SPEECH_SCOPE(824D9370)
NSV_SPEECH_SCOPE(824D9908)
NSV_SPEECH_SCOPE(824D92B0)
NSV_SPEECH_SCOPE(824D9AD8)
NSV_SPEECH_SCOPE(824D9DB8)
#undef NSV_SPEECH_SCOPE
extern "C" REX_FUNC(sub_824ABD90) {  // living-world event switch (r4 event) -> dispatcher
  t_reached_switch=true;
  __imp__sub_824ABD90(ctx,base);
}
extern "C" REX_FUNC(sub_824ABA18) {  // living-world line request gates (event r6)
  if(!TraceSpeech()) { __imp__sub_824ABA18(ctx,base); return; }
  const uint32_t event=ctx.r6.u32&0xFFFF, r4=ctx.r4.u32, r5=ctx.r5.u32, caller=uint32_t(ctx.lr);
  const bool saved=t_reached_switch;
  t_reached_switch=false;
  __imp__sub_824ABA18(ctx,base);
  const bool reached=t_reached_switch;
  t_reached_switch=saved;
  char text[128];
  std::snprintf(text,sizeof(text),"request %04X (r4 %08X r5 %08X from %08X): %s",event,r4,r5,caller,
                reached ? "passed the gates" : "DROPPED by the request gates");
  TraceStep(reached ? "request-ok" : "request-dropped",original_sfx ? original_voice : 0,event,text);
}
extern "C" REX_FUNC(sub_824AC560) {  // second request family (event r4 below 294)
  if(TraceSpeech()) {
    char text[96];
    std::snprintf(text,sizeof(text),"request family 2: event %04X r5 %08X from %08X",ctx.r4.u32&0xFFFF,
                  ctx.r5.u32,uint32_t(ctx.lr));
    TraceStep("request2",original_sfx ? original_voice : 0,ctx.r4.u32&0xFFFF,text);
  }
  __imp__sub_824AC560(ctx,base);
}
extern "C" REX_FUNC(sub_824D91F0) {
  const uint32_t object=ctx.r3.u32;
  const uint32_t voice=OwnerVoice(object);
  Scope scope(voice!=0,voice); __imp__sub_824D91F0(ctx,base);
  std::lock_guard lock(owners_mutex); owners.erase(object);
}
namespace {
// Original living-world speech events (Skate 2 IDs; the 47 shared ones keep
// the same ID in Skate 3) and the Skate 3-only ones, for the speech log.
const char* EventName(uint32_t event) {
  switch(event) {
    case 0x2000: return "601_Warn_Gen"; case 0x2001: return "602_Warn_NSZ";
    case 0x2002: return "603_Chase_Start"; case 0x2003: return "604_hot_pursuit";
    case 0x2004: return "605_chase_terminate"; case 0x2005: return "606_catch";
    case 0x2006: return "607_chase_join"; case 0x2007: return "609_lunge"; case 0x2008: return "610_block";
    case 0x2009: return "611_cool_down"; case 0x200A: return "101_pos"; case 0x200B: return "103_repeat";
    case 0x200C: return "104_spec_slam"; case 0x200D: return "105_spec_chase"; case 0x200E: return "201_grunt";
    case 0x200F: return "202_impact_react"; case 0x2010: return "204_gasp"; case 0x2011: return "205_spec_collision";
    case 0x2012: return "501_warn"; case 0x2013: return "805_conv_cell"; case 0x2014: return "806_cls_interact";
    case 0x2015: return "807_cls_react"; case 0x2016: return "1901_shout"; case 0x2017: return "4402_cell_greet";
    case 0x2018: return "4405_cell_bye"; case 0x2019: return "109_help"; case 0x201A: return "110_spec_litter";
    case 0x201B: return "111_litter"; case 0x201C: return "203_concern"; case 0x2020: return "108_flee";
    case 0x2023: return "502_warn_nsz"; case 0x2024: return "130_collide"; case 0x2025: return "131_collide_object";
    case 0x2037: return "206_pan_handle"; case 0x2038: return "207_random_bum"; case 0x2055: return "102_Radio";
    case 0x2062: return "210_CollTeam"; case 0x2063: return "338_gesture"; case 0x206A: return "330_TzerStart";
    case 0x206B: return "331_TzerDone"; case 0x206C: return "335_ShoveBrd"; case 0x206D: return "336_ShoveSft";
    case 0x206F: return "339_Leapfrog"; case 0x2070: return "340_HOM"; case 0x2075: return "344_HitReact";
    case 0x2077: return "126_Outrun";
    default: return "";
  }
}
// Skate 2 recorded no generic warning (501_warn) for its security guards
// (voices 75-79, secg1-5); they warn with 601_Warn_Gen, which Skate 3 has no
// event for (livingworldspeech.big sample names: 501 exists for every other
// pedestrian voice, 601/602 only for 75-79). Skate 3 asks every ped for 501,
// so the guards stayed silent (log 350). Same program bank, same namespace.
// Skate 3's guards shove the skater with 335_ShoveBrd / 336_ShoveSft (206C /
// 206D, logs 354-355); Skate 2's guard voices have no shove lines, their line
// for catching the skater is 606_catch.
uint32_t Skate2EventFor(uint32_t voice, uint32_t event) {
  if(voice>=75 && voice<=79 && event==0x2012) return 0x2000;
  if(voice>=75 && voice<=79 && (event==0x206C || event==0x206D)) return 0x2005;
  return event;
}
// skate3_skate2_speech_log: first request of each (voice, event) pair.
void LogVoiceEvent(uint32_t voice, uint32_t requested, uint32_t sent, bool played) {
  if(!SpeechLog()) return;
  static std::mutex mutex;
  static std::unordered_set<uint64_t> seen;
  std::lock_guard lock(mutex);
  if(seen.size()>=256 || !seen.insert((uint64_t(voice)<<32)|requested).second) return;
  if(!played) {
    REXLOG_INFO("skate2-speech: voice {} asked for {:04X} {}: not a Skate 2 event, silent",voice,requested,
                EventName(requested));
  } else if(sent!=requested) {
    REXLOG_INFO("skate2-speech: voice {} asked for {:04X} {}: plays Skate 2's {:04X} {} for this voice",voice,
                requested,EventName(requested),sent,EventName(sent));
  } else {
    REXLOG_INFO("skate2-speech: voice {} asked for {:04X} {}: plays",voice,requested,EventName(requested));
  }
}
}  // namespace
extern "C" REX_FUNC(sub_829717A0) {
  if(SpeechLog() && instance.load() && skate3::world_loading::OriginalPedestriansActive() &&
     (!original_sfx || (ctx.r4.u32&0xFFFF)!=1)) {
    // Log 352: the guards (voice 78, nearby and matched) never asked for a
    // bank-1 line. Every New San Van request outside the bridged path, once
    // per (bank, event, caller, voice): bank-1 lines for a speaker the bridge
    // never matched (Skate 3's bank with an empty voice = silence), and lines
    // on any other bank (never bridged), with the speaker words r6..r9 and
    // the Skate 2 voice the bridge gave any of them.
    static std::mutex mutex;
    static std::unordered_set<uint64_t> seen;
    const uint32_t event=ctx.r4.u32>>16, bank=ctx.r4.u32&0xFFFF, caller=uint32_t(ctx.lr);
    uint32_t voice=original_sfx ? original_voice : 0;
    if(!voice) {
      std::lock_guard owners_lock(owners_mutex);
      for(const uint32_t word : {ctx.r6.u32, ctx.r7.u32, ctx.r8.u32, ctx.r9.u32}) {
        if(const auto it=handles.find(word); it!=handles.end()) { voice=it->second; break; }
        if(const auto it=owners.find(word); it!=owners.end()) { voice=it->second; break; }
      }
    }
    std::lock_guard lock(mutex);
    const uint64_t id=(uint64_t(event)<<48)|(uint64_t(bank&0xFF)<<40)|(uint64_t(voice&0xFF)<<32)|caller;
    if(seen.size()<192 && seen.insert(id).second) {
      if(bank==1)
        REXLOG_INFO("skate2-speech: a ped WITHOUT a Skate 2 voice asked for {:04X} {} (from {:08X}, speaker words "
                    "{:08X} {:08X} {:08X} {:08X}{}): silent",event,EventName(event),caller,ctx.r6.u32,ctx.r7.u32,
                    ctx.r8.u32,ctx.r9.u32,voice ? " = the bridge's voice " + std::to_string(voice) : std::string());
      else
        REXLOG_INFO("skate2-speech: line {:04X} {} on bank {} (not the living-world bank) from {:08X}, speaker "
                    "{}, words {:08X} {:08X} {:08X} {:08X}",event,EventName(event),bank,caller,
                    voice ? "Skate 2 voice " + std::to_string(voice) : std::string("without a Skate 2 voice"),
                    ctx.r6.u32,ctx.r7.u32,ctx.r8.u32,ctx.r9.u32);
    }
  }
  if(original_sfx && (ctx.r4.u32&0xFFFF)==1) {
    const uint32_t requested=ctx.r4.u32>>16;
    const uint32_t event=Skate2EventFor(original_voice,requested);
    if(!instance.load() || !skate3::world_loading::OriginalPedestriansActive() ||
        (event==requested && !events.contains(event))) {
      if(instance.load() && skate3::world_loading::OriginalPedestriansActive())
        LogVoiceEvent(original_voice,requested,event,false);
      ctx.r3.u64=0; return;
    }
    LogVoiceEvent(original_voice,requested,event,true);
    ctx.r4.u64=(event<<16)|event_namespace.load();
    const auto n=++dispatches;
    if(SpeechLog() && (n<=12 || !(n%128)))
      REXLOG_INFO("skate2-speech: dispatch={} event={:04X}",n,ctx.r4.u32>>16);
  }
  __imp__sub_829717A0(ctx,base);
}
// Native SFX uses instance 0 for cancellation and channel arbitration. Route
// both operations with the same owner as event dispatch, on the pedestrian
// voice channel: arbitration 824A62F0 for an instance other than 0 tests only
// channel 0 (busy = playing +0x426 and not pending +0x470).
extern "C" REX_FUNC(sub_824A73F0) {
  if(original_sfx && instance.load() && ctx.r4.u32==0) ctx.r4.u64=2;
  __imp__sub_824A73F0(ctx,base);
}
extern "C" REX_FUNC(sub_824A62F0) {
  const uint32_t target=instance.load();
  if(original_sfx && target && ctx.r4.u32==2) {
    const bool busy=base[target+0x426+kSpeechChannel] && !base[target+0x470+kSpeechChannel];
    ctx.r3.u64=!busy; return;
  }
  __imp__sub_824A62F0(ctx,base);
}
extern "C" REX_FUNC(sub_82C5E1D8) {
  const uint32_t target=instance.load(), manager=Read(base,kManager);
  const uint32_t requested_instance=ctx.r3.u32, requested_channel=ctx.r4.u32;
  if(original_sfx && target && manager &&
     (ctx.r3.u32==Read(base,manager+8) || ctx.r3.u32==target)) {
    ctx.r3.u64=target; ctx.r4.u64=kSpeechChannel;
  }
  // Name every routine that stops a playing original line (bounded).
  const uint32_t channel=ctx.r4.u32, caller=uint32_t(ctx.lr);
  const bool traced=SpeechLog() && target && ctx.r3.u32==target && channel<2 &&
                    base[target+0x426+channel];
  __imp__sub_82C5E1D8(ctx,base);
  if(traced) {
    static std::atomic<uint32_t> cancels{0};
    const auto n=++cancels;
    if(n<=96 || !(n%64))
      REXLOG_INFO("skate2-speech: cancel n={} channel={} caller={:08X} requested={}/{} original_sfx={}",
                  n,channel,caller,requested_instance==target ? "speech" : "native",
                  requested_channel,original_sfx);
  }
}
// Instance 2 channel 0 is the cinematic 6Channel player and no native bank
// lives in instance 2, so natively it never starts from a bank queue. Leave
// queued original requests to the pedestrian channel; channel 0 still runs
// its own completion handling whenever it is playing.
extern "C" REX_FUNC(sub_82C5CC90) {
  const uint32_t target=instance.load();
  if(target && ctx.r3.u32==target && ctx.r4.u32!=kSpeechChannel && ctx.r4.u32<2 &&
     !base[target+0x426+ctx.r4.u32]) return;
  if(!SpeechLog()) { __imp__sub_82C5CC90(ctx,base); return; }
  // Original channel, and retail instance 0 (AI skaters) in New San Van.
  const uint32_t voice_instance=ctx.r3.u32, channel=ctx.r4.u32, manager=Read(base,kManager);
  const bool original=target && voice_instance==target && channel==kSpeechChannel;
  const bool native=!original && target && manager && channel<2 &&
                    voice_instance==Read(base,manager+8) &&
                    skate3::world_loading::OriginalPedestriansActive();
  // Flag nibbles: playing pending held-mode held hold476 hold478. A change seen
  // on entry happened outside this update (cancellation or another writer).
  static uint32_t last[3]{};  // Single speech update thread.
  static std::atomic<uint32_t> changes[2]{};
  const uint32_t index=original ? 2 : channel;
  const auto report=[&](const char* where) {
    const uint32_t state=ChannelState(base,voice_instance,channel);
    if(state==last[index]) return;
    last[index]=state;
    if(++changes[native]<=(native ? 160u : 240u))
      REXLOG_INFO("skate2-speech: {} state {:06X} channel={} at {}",
                  native ? "native voice" : "voice",state,channel,where);
  };
  if(original || native) report("update entry");
  __imp__sub_82C5CC90(ctx,base);
  if(original || native) report("update exit");
  if(original) {
    traced_players[0].store(PlayerState(base,voice_instance,channel),std::memory_order_relaxed);
    // The three 'Spch' stream records (manager+4 -> +4, 0x20 each): stream at
    // +0x14, its reader at stream+8. Traced only in New San Van.
    const uint32_t records=GuardedWord(base,GuardedWord(base,manager+4)+4);
    for(uint32_t i=0;i<3;++i) {
      const uint32_t stream=GuardedWord(base,records+i*0x20+0x14);
      const uint32_t reader=stream && stream!=0xDEADDEAD ? GuardedWord(base,stream+8) : 0;
      traced_readers[i].store(skate3::world_loading::OriginalPedestriansActive() && reader!=0xDEADDEAD ? reader : 0,
                              std::memory_order_relaxed);
    }
  }
  else if(manager && channel<2 && voice_instance==Read(base,manager+8))
    traced_players[1+channel].store(native ? PlayerState(base,voice_instance,channel) : 0,
                                    std::memory_order_relaxed);
}
extern "C" REX_FUNC(sub_82C5F338) {
  if(SpeechLog() && Read(base,ctx.r3.u32+0x10)==4 && instance.load()) {
    const auto n=++requests;
    if(n<=12 || !(n%128)) REXLOG_INFO("skate2-speech: sample request={} bank=4",n);
  }
  __imp__sub_82C5F338(ctx,base);
}

// The playback bank ID is private, but its policy/configuration is the native
// living-world category. Keep instance 2 for lifetime/completion bookkeeping.
extern "C" REX_FUNC(sub_824A84B8) {
  if(instance.load() && ctx.r4.u32==2 && ctx.r5.u32==4) {
    if(!skate3::world_loading::OriginalPedestriansActive()) { ctx.r3.u64=0; return; }
    Scope scope(true);
    ctx.r5.u64=1;
    // r6 = channel, r7 = line ID: 824A6B80 copies that line's speech tuning
    // into the living-world record (bank 1, channel), 0x44 bytes at
    // manager+0x1BB08 (+0 start delay, +0x10/+0x14/+0x18 level tuning).
    const uint32_t manager=ctx.r3.u32, channel=ctx.r6.u32, line=ctx.r7.u32&0xFFFF;
    __imp__sub_824A84B8(ctx,base);
    static std::atomic<uint32_t> logged{0};
    if(SpeechLog() && channel<2 && ++logged<=32) {
      const uint32_t record=manager+0x1BB08+(2+channel)*0x44;
      REXLOG_INFO("skate2-speech: speaker policy channel={} line={:04X} speaker={:08X} delay={:.2f} "
                  "tuning={} {:.1f} {:.1f}",channel,line,ctx.r3.u32,GuardedFloat(base,record),
                  int32_t(GuardedWord(base,record+0x10)),GuardedFloat(base,record+0x14),
                  GuardedFloat(base,record+0x18));
    }
    return;
  }
  __imp__sub_824A84B8(ctx,base);
}
// Drain original queues on the audio thread when leaving BAM. The instance
// remains registered for a return visit; native speech instances are untouched.
extern "C" REX_FUNC(sub_82C5F0D8) {
  const uint32_t target=instance.load();
  if(target && !skate3::world_loading::OriginalPedestriansActive()) {
    REX_STORE_U32(target+0x6C,0);
    for(uint32_t channel=0;channel<2;++channel) {
      if(!base[target+0x426+channel]) continue;
      auto call=ctx;
      call.r3.u64=target; call.r4.u64=channel;
      __imp__sub_82C5E1D8(call,base);
    }
  }
  __imp__sub_82C5F0D8(ctx,base);
}

// Bank IDs index native policy arrays sized for the four retail banks. Keep
// sample lookup at bank 4, translating only the policy calls to living-world.
extern "C" REX_FUNC(sub_824823E0) {
  if(instance.load() && ctx.r4.u32==4) ctx.r4.u64=1;
  __imp__sub_824823E0(ctx,base);
}
// Only 82C5BC40 calls this slot (instance index, channel), before any bank
// loads. Build the native pedestrian DistanceBased chain on instance 2's empty
// channel 1 when prepared original voices exist; no native chain changes.
extern "C" REX_FUNC(sub_824A7F38) {
  if(prepared && ctx.r4.u32==2 && ctx.r5.u32==kSpeechChannel) { ctx.r3.u64=kDistanceBased; return; }
  __imp__sub_824A7F38(ctx,base);
}

extern "C" REX_FUNC(sub_824823C0) {
  if(instance.load() && ctx.r4.u32==4) ctx.r4.u64=1;
  __imp__sub_824823C0(ctx,base);
}

// 82C5CEF0 plays at speaker+8/32768 * speaker+0xC, or silence when the
// channel's +0x484 mute byte is set. The speaker is stored at +0x460+channel*4.
namespace {
struct VoiceLevel { uint32_t speaker=0; int32_t level=-1; float gain=0; uint8_t muted=0; };
VoiceLevel ReadVoiceLevel(uint8_t* base, uint32_t voice_instance, uint32_t channel) {
  VoiceLevel result;
  if(channel>=2) return result;
  result.speaker=Read(base,voice_instance+0x460+channel*4);
  result.muted=base[voice_instance+0x484+channel];
  if(!result.speaker) return result;
  result.level=int32_t(Read(base,result.speaker+8));
  const uint32_t bits=Read(base,result.speaker+0xC);
  std::memcpy(&result.gain,&bits,sizeof(result.gain));
  return result;
}
}
extern "C" REX_FUNC(sub_82C5DC80) {
  const uint32_t target=instance.load(), manager=Read(base,kManager);
  const bool original=target && ctx.r3.u32==target;
  // Retail living-world/maincast speech in New San Van, if any, as a baseline.
  const bool native=!original && manager && ctx.r3.u32==Read(base,manager+8) &&
                    skate3::world_loading::OriginalPedestriansActive();
  const uint32_t voice_instance=ctx.r3.u32, request=ctx.r8.u32, channel=ctx.r9.u32;
  const uint32_t sample=original || native ? Read(base,request) : 0;
  const uint32_t bank=native ? Read(base,request+0x10) : 4;
  // Held-start mode (+0x472) schedules the stream 1000 s ahead until 82C5E908
  // releases it; only native special lines on other channels use it.
  if(original && channel==kSpeechChannel && base[target+0x472+channel]) {
    static std::atomic<uint32_t> cleared{0};
    if(SpeechLog() && ++cleared<=16)
      REXLOG_INFO("skate2-speech: cleared held-start mode on pedestrian channel");
    base[target+0x472+channel]=0;
  }
  __imp__sub_82C5DC80(ctx,base);
  if(!SpeechLog() || (!original && !native)) return;
  static std::atomic<uint32_t> native_starts{0};
  const auto n=original ? ++starts : ++native_starts;
  if(original ? (n<=12 || !(n%128)) : n<=16) {
    const auto voice=ReadVoiceLevel(base,voice_instance,channel);
    REXLOG_INFO("skate2-speech: {} start={} bank={} sample={:08X} accepted={} channel={} level={} gain={:.3f} muted={} state={:06X}",
                original ? "playback" : "native playback",n,bank,sample,ctx.r3.u32!=0,channel,
                voice.level,voice.gain,voice.muted,channel<2 ? ChannelState(base,voice_instance,channel) : 0);
  }
}

// Per-frame voice update while playing. Pending +0x470 clears once the stream
// player reports it has started; speaker+0x4C is set while audibly playing.
// The native body clobbers r3/r4, so take the arguments first (build142/143
// read them afterwards and never matched). Pending +0x470 clears when the
// SndPlayer1 state float (player+0x10 -> +4) equals the start token that
// 82C5DC80 stored at +0x440+channel*8. The speech manager's 'Spch' stream type
// (manager+4, 82B47910) owns three 0x20-byte stream records at +4.
extern "C" REX_FUNC(sub_82C5CEF0) {
  const uint32_t voice_instance=ctx.r3.u32, channel=ctx.r4.u32;
  __imp__sub_82C5CEF0(ctx,base);
  if(!SpeechLog()) return;
  const uint32_t target=instance.load(), manager=Read(base,kManager);
  const bool original=target && voice_instance==target && channel==kSpeechChannel;
  // Retail instance-0 speech in New San Van (AI skaters) as a working baseline.
  const bool native=!original && manager && voice_instance==Read(base,manager+8) && channel<2 &&
                    skate3::world_loading::OriginalPedestriansActive();
  if(!original && !native) return;
  static std::atomic<uint32_t> frames[2]{}, logged[2]{};
  const auto n=++frames[native];
  if(n%8!=1 || ++logged[native]>(native ? 120u : 240u)) return;
  const auto voice=ReadVoiceLevel(base,voice_instance,channel);
  const uint32_t modules=GuardedWord(base,voice_instance+0x430+channel*4);
  const uint32_t slot=GuardedWord(base,voice_instance+channel*0x2C);
  const uint32_t player=slot<16 ? GuardedWord(base,modules+slot*4) : 0xDEADDEAD;
  const uint32_t state=GuardedWord(base,player+0x10);
  std::array<uint32_t,8> words{};
  for(uint32_t i=0;i<words.size();++i) words[i]=GuardedWord(base,state+i*4);
  const uint32_t spch=GuardedWord(base,manager+4), records=GuardedWord(base,spch+4);
  std::array<uint32_t,3> streams{};
  for(uint32_t i=0;i<streams.size();++i) streams[i]=GuardedWord(base,records+i*0x20+0x18);
  // Speaker fields 82C5CEF0 sends to the chain's other modules each frame
  // (+0x10/+0x14/+0x18-+0x20, panner +0x24-+0x2C, +0x50-+0x58).
  std::array<uint32_t,11> speaker{};
  for(uint32_t i=0;i<8;++i) speaker[i]=voice.speaker ? GuardedWord(base,voice.speaker+0x10+i*4) : 0;
  for(uint32_t i=0;i<3;++i) speaker[8+i]=voice.speaker ? GuardedWord(base,voice.speaker+0x50+i*4) : 0;
  REXLOG_INFO("skate2-speech: {} frame={} channel={} playing={} pending={} audible={} level={} gain={:.3f} muted={} "
              "token={:08X} player={:08X} state={:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} spch={:08X} {:08X} {:08X} "
              "spk={:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X}",
              native ? "native voice" : "voice",n,channel,base[voice_instance+0x426+channel],
              base[voice_instance+0x470+channel],voice.speaker ? base[voice.speaker+0x4C] : 0,
              voice.level,voice.gain,voice.muted,GuardedWord(base,voice_instance+0x440+channel*8),player,
              words[0],words[1],words[2],words[3],words[4],words[5],words[6],words[7],
              streams[0],streams[1],streams[2],speaker[0],speaker[1],speaker[2],speaker[3],speaker[4],
              speaker[5],speaker[6],speaker[7],speaker[8],speaker[9],speaker[10]);
}

// BigFile open: proves whether "<sample archive>|<entry>" resolves.
extern "C" REX_FUNC(sub_82A83808) {
  const uint32_t path=ctx.r4.u32;
  std::string name;
  bool native=false;
  if(SpeechLog() && archive_mounted.load(std::memory_order_relaxed) && path>=0x10000 &&
     path<0xFFFF0000) {
    const char* text=reinterpret_cast<const char*>(base+path);
    const std::string_view view(text,strnlen(text,300));
    // Only archive-qualified stream paths; the loose evt/hdr/sth loads miss
    // this handler by design and continue to the loose-file device. Retail
    // speech opens in New San Van are the timing baseline.
    if(view.find('|')!=std::string_view::npos) {
      if(view.find("nsv_s2_speech")!=std::string_view::npos) name=view;
      else if(view.find("speech.big|")!=std::string_view::npos &&
              skate3::world_loading::OriginalPedestriansActive()) { name=view; native=true; }
    }
  }
  __imp__sub_82A83808(ctx,base);
  if(name.empty()) return;
  if(native) {
    static std::atomic<uint32_t> native_opens{0};
    const auto n=++native_opens;
    if(n<=24) REXLOG_INFO("skate2-speech: native stream open n={} result={:08X} path='{}'",n,ctx.r3.u32,name);
    return;
  }
  static std::atomic<uint32_t> opens{0}, failures{0};
  const auto n=++opens;
  const bool failed=ctx.r3.u32==0;
  if(n<=16 || !(n%128) || (failed && ++failures<=32))
    REXLOG_INFO("skate2-speech: stream open n={} result={:08X} path='{}'",n,ctx.r3.u32,name);
}

// Stream player (SndPlayer1) internals for the traced players: where original
// requests wait, and whether played requests decode to silence.
// State: +0x08 system ([+8]+8 = time), +0x0C voice (+0x47 state), +0x34
// current request handle, +0x60 requests (0x50 each), +0x71+i*0x10 decode
// ring flags (20), u16 +0x1D0 = offset of the request records (0x30 each: +0
// start time, +8 decoder, +0x10 rate, +0x14 length, +0x28 setup error 0xFF,
// +0x2E state 1 queued/2 ready/3 playing/4 done, +0x2F channels), +0x1D3 fill,
// +0x1D4 service, +0x1D5 current, +0x1D6 slots, +0x1D9/+0x1DA ring write/read.
// Request (0x50): +0x24 'Spch' stream record, +0x28 stream reader, +0x2C reader
// request, +0x48 codec index, +0x4C block format. Reader (82EC0A10): +0x54
// state, +0x6C consumed, +0x74 write, +0x7C current request (+4 state), +0x90
// open file, +0x190 file handle, +0x198 file position (64-bit).
namespace {
struct QueueView {
  std::array<uint8_t,7> index{};  // fill service current slots ring-write ring-read voice-state
  std::array<uint8_t,8> states{};
  std::array<uint8_t,20> ring{};
  uint32_t focus=0, decoder=0, length=0, error=0, pool=0, reader=0, reader_request=0;
  uint32_t codec=0, format=0, reader_state=0, reader_current=0, reader_current_state=0, file=0, blocks=0;
  // Decode-ring flags cycle with every block while a request plays; they are
  // printed with each view but do not by themselves trigger one.
  auto Key() const {
    return std::tie(index,states,focus,decoder,length,error,pool,reader,reader_request,codec,format,
                    reader_state,reader_current,reader_current_state,file,blocks);
  }
  bool operator==(const QueueView& other) const { return Key()==other.Key(); }
};
uint32_t RequestRecords(uint8_t* base, uint32_t state) { return state+(GuardedWord(base,state+0x1D0)>>16); }
QueueView ReadQueue(uint8_t* base, uint32_t state) {
  QueueView v;
  v.index={GuardedByte(base,state+0x1D3),GuardedByte(base,state+0x1D4),GuardedByte(base,state+0x1D5),
           GuardedByte(base,state+0x1D6),GuardedByte(base,state+0x1D9),GuardedByte(base,state+0x1DA),
           GuardedByte(base,GuardedWord(base,state+0xC)+0x47)};
  const uint32_t slots=v.index[3]<=v.states.size() ? v.index[3] : 0;
  const uint32_t records=RequestRecords(base,state);
  for(uint32_t i=0;i<slots;++i) v.states[i]=GuardedByte(base,records+i*0x30+0x2E);
  for(uint32_t i=0;i<v.ring.size();++i) v.ring[i]=GuardedByte(base,state+0x71+i*0x10);
  // Focus on the first request being set up from the service index, else current.
  v.focus=slots ? v.index[2]%slots : 0;
  for(uint32_t k=0;k<slots;++k) {
    const uint32_t i=(v.index[1]+k)%slots;
    if(v.states[i]==1 || v.states[i]==2) { v.focus=i; break; }
  }
  const uint32_t record=records+v.focus*0x30, request=GuardedWord(base,state+0x60)+v.focus*0x50;
  v.decoder=GuardedWord(base,record+8); v.length=GuardedWord(base,record+0x14);
  v.error=GuardedWord(base,record+0x28);
  // request+0x28 is the 'Spch' record's stream (+8 reader, +0x10 ready blocks).
  const uint32_t stream=GuardedWord(base,request+0x28);
  v.pool=GuardedWord(base,request+0x24);
  if(stream && stream!=0xDEADDEAD) { v.reader=GuardedWord(base,stream+8); v.blocks=GuardedWord(base,stream+0x10); }
  v.reader_request=GuardedWord(base,request+0x2C);
  v.codec=GuardedByte(base,request+0x48); v.format=GuardedByte(base,request+0x4C);
  if(v.reader && v.reader!=0xDEADDEAD) {
    v.reader_state=GuardedWord(base,v.reader+0x54); v.reader_current=GuardedWord(base,v.reader+0x7C);
    if(v.reader_current && v.reader_current!=0xDEADDEAD) v.reader_current_state=GuardedWord(base,v.reader_current+4);
    v.file=GuardedWord(base,v.reader+0x190);
  }
  return v;
}
std::string Digits(const uint8_t* values, size_t count) {
  std::string text;
  for(size_t i=0;i<count;++i) text.push_back(values[i]<10 ? char('0'+values[i]) : values[i]==0xEE ? '?' : '+');
  return text;
}
void LogQueue(uint8_t* base, int traced, uint32_t state, const QueueView& v, const char* why, uint32_t n) {
  // Reader positions move while data streams; printed, not part of the change test.
  uint32_t consumed=0, write=0, position=0;
  std::string file;
  if(v.reader && v.reader!=0xDEADDEAD) {
    consumed=GuardedWord(base,v.reader+0x6C); write=GuardedWord(base,v.reader+0x74);
    position=GuardedWord(base,v.reader+0x19C);
    char name[96]{};
    if(skate3::native_scene::GuestTryCopy(name,base+v.reader+0x90,sizeof(name)-1)) {
      const std::string_view view(name,strnlen(name,sizeof(name)-1));
      const auto bar=view.rfind('|');
      file=std::string(bar==std::string_view::npos ? view : view.substr(bar+1));
    }
  }
  const uint32_t record=RequestRecords(base,state)+v.focus*0x30;
  REXLOG_INFO("skate2-speech: queue {} n={} {} handle={:.0f} fill/svc/cur/slots={}/{}/{}/{} states={} ring={} ring_w/r={}/{} "
              "voice={} focus={} decoder={:08X} len={} err={:08X} rate={:.0f} ch={} start={:.3f} now={:.3f} codec={} fmt={} "
              "spch={:08X} reader={:08X} rstate={} rreq={:08X} rcur={:08X}/{} fh={:08X} pos={:X} cons={:X} wr={:X} blocks={} file='{}'",
              TracedName(traced),n,why,GuardedFloat(base,state+0x34),v.index[0],v.index[1],v.index[2],v.index[3],
              Digits(v.states.data(),v.index[3]<=v.states.size() ? v.index[3] : 0),Digits(v.ring.data(),v.ring.size()),
              v.index[4],v.index[5],v.index[6],v.focus,v.decoder,v.length,v.error,GuardedFloat(base,record+0x10),
              GuardedByte(base,record+0x2F),GuardedDouble(base,record),GuardedDouble(base,GuardedWord(base,state+8)+8),
              v.codec,v.format,v.pool,v.reader,v.reader_state,v.reader_request,v.reader_current,v.reader_current_state,
              v.file,position,consumed,write,v.blocks,file);
}
}
// Service (registered at state+0x4C): retires finished requests and calls
// 82B33D40 to set up a queued one once the decode ring's write slot is free.
extern "C" REX_FUNC(sub_82B31EE0) {
  const uint32_t state=ctx.r3.u32;
  const int traced=SpeechLog() ? TracedPlayer(state) : -1;
  __imp__sub_82B31EE0(ctx,base);
  if(traced<0) return;
  static std::mutex mutex;
  static std::array<QueueView,3> last{};
  static std::array<uint32_t,3> calls{}, logged{};
  const QueueView view=ReadQueue(base,state);
  std::lock_guard lock(mutex);
  const uint32_t n=++calls[traced];
  const bool changed=!(view==last[traced]);
  // While a request waits in setup, repeat the view every 256 services.
  const bool waiting=view.states[view.focus%view.states.size()]==1;
  if(!changed && !(waiting && n%256==0)) return;
  last[traced]=view;
  if(++logged[traced]<=(traced==0 ? 400u : 200u)) LogQueue(base,traced,state,view,changed ? "change" : "waiting",n);
}
// Request setup: false leaves the request queued (state 1) for another try.
extern "C" REX_FUNC(sub_82B33D40) {
  const uint32_t state=ctx.r3.u32, index=ctx.r4.u32;
  const int traced=SpeechLog() ? TracedPlayer(state) : -1;
  __imp__sub_82B33D40(ctx,base);
  if(traced<0) return;
  const bool ready=(ctx.r3.u32&0xFF)!=0;
  const uint32_t record=RequestRecords(base,state)+index*0x30;
  const uint32_t request=GuardedWord(base,state+0x60)+index*0x50;
  const uint32_t error=GuardedWord(base,record+0x28);
  static std::mutex mutex;
  static std::array<uint64_t,3> last{~0ull,~0ull,~0ull};
  static std::array<uint32_t,3> repeats{}, logged{};
  const uint64_t key=(uint64_t(index&0xFF)<<40) | (uint64_t(ready)<<32) | error;
  std::lock_guard lock(mutex);
  if(key==last[traced]) { if(++repeats[traced]%512) return; }
  else repeats[traced]=0;
  last[traced]=key;
  if(++logged[traced]<=(traced==0 ? 200u : 100u))
    REXLOG_INFO("skate2-speech: prepare {} index={} ready={} err={:08X} decoder={:08X} codec={} type={} fmt={} ch={} "
                "rate={:.0f} len={} spch={:08X} repeats={}",
                TracedName(traced),index,ready,error,GuardedWord(base,record+8),GuardedByte(base,request+0x48),
                GuardedByte(base,request+0x49),GuardedByte(base,request+0x4C),GuardedByte(base,record+0x2F),
                GuardedFloat(base,record+0x10),GuardedWord(base,record+0x14),GuardedWord(base,request+0x24),
                repeats[traced]);
}
// Decoder instance creation (type descriptor: +0x14 FourCC, e.g. EXm0).
extern "C" REX_FUNC(sub_82B3CD38) {
  const uint32_t type=ctx.r3.u32, channels=ctx.r4.u32, caller=uint32_t(ctx.lr);
  __imp__sub_82B3CD38(ctx,base);
  if(ctx.r3.u32 || !instance.load() || !SpeechLog()) return;
  static std::atomic<uint32_t> failures{0};
  const auto n=++failures;
  if(n<=64 || !(n%256))
    REXLOG_INFO("skate2-speech: decoder create failed n={} type={:08X} fourcc={:08X} channels={} caller={:08X} nsv={}",
                n,type,GuardedWord(base,type+0x14),channels,caller,
                skate3::world_loading::OriginalPedestriansActive());
}
// Process: decodes into the output buffer, then swaps it to out+0x1C with the
// sample count at out+0x30 (floats, channel stride at buffer+0xE).
extern "C" REX_FUNC(sub_82B34278) {
  const uint32_t state=ctx.r3.u32, out=ctx.r4.u32;
  const int traced=SpeechLog() ? TracedPlayer(state) : -1;
  __imp__sub_82B34278(ctx,base);
  if(traced<0) return;
  struct Output { uint32_t handle=0, frames=0, silent=0; uint64_t samples=0; float peak=0; double energy=0; };
  static std::mutex mutex;
  static std::array<Output,3> outputs{};
  static std::array<uint32_t,3> logged{};
  const uint32_t count=GuardedWord(base,out+0x30), handle=GuardedWord(base,state+0x34);
  std::lock_guard lock(mutex);
  Output& o=outputs[traced];
  const auto report=[&](const char* why) {
    if(!o.frames || ++logged[traced]>(traced==0 ? 160u : 80u)) return;
    float id;
    std::memcpy(&id,&o.handle,sizeof(id));
    REXLOG_INFO("skate2-speech: output {} handle={:.0f} frames={} silent_frames={} samples={} peak={:.5f} rms={:.6f} ({})",
                TracedName(traced),id,o.frames,o.silent,o.samples,o.peak,
                o.samples ? std::sqrt(o.energy/double(o.samples)) : 0.0,why);
  };
  if(!count || count>4096) {
    if(o.frames) { report("stopped"); o=Output{}; o.handle=handle; }
    return;
  }
  if(handle!=o.handle) { report("next request"); o=Output{}; o.handle=handle; }
  const uint32_t data=GuardedWord(base,GuardedWord(base,out+0x1C)+4);
  std::array<uint32_t,1024> raw;
  const uint32_t n=std::min<uint32_t>(count,uint32_t(raw.size()));
  if(data==0xDEADDEAD || !skate3::native_scene::GuestTryCopy(raw.data(),base+data,n*4)) return;
  float peak=0;
  double energy=0;
  for(uint32_t i=0;i<n;++i) {
    const uint32_t bits=__builtin_bswap32(raw[i]);
    float x;
    std::memcpy(&x,&bits,sizeof(x));
    if(!std::isfinite(x)) continue;
    peak=std::max(peak,std::fabs(x)); energy+=double(x)*x;
  }
  ++o.frames; o.samples+=n; o.energy+=energy; o.peak=std::max(o.peak,peak);
  if(peak==0) ++o.silent;
  if(o.frames%512==0) report("progress");
}

// Stream reader (82EC05C0) for the traced players: R+0x04 flags, +0x54 thread
// state, +0x70/+0x74 parse window, +0x7C current request (0x148 record: +0
// handle, +4 state, +0x10 mode, +0x14 path, +0x120 offset, +0x12C end of file),
// +0x90 open path, +0x190 file handle, +0x198 file position, +0x1A0 async
// operation, +0x208 file size. Its stream list (+0x38) holds the consumer
// (+0x10 ready blocks). A request reads only after its file is open; requests
// for the open path skip the close/open.
namespace {
std::string GuestBaseName(uint8_t* base, uint32_t address) {
  char text[128]{};
  if(address<0x10000 || !skate3::native_scene::GuestTryCopy(text,base+address,sizeof(text)-1)) return "?";
  const std::string_view view(text,strnlen(text,sizeof(text)-1));
  const auto bar=view.find_last_of("|\\/");
  return std::string(bar==std::string_view::npos ? view : view.substr(bar+1));
}
const char* ReaderName(int index) { return index==0 ? "spch0" : index==1 ? "spch1" : "spch2"; }
bool LogReader(int traced) {
  static std::array<std::atomic<uint32_t>,3> lines{};
  return ++lines[traced]<=(traced==0 ? 400u : 160u);
}
uint32_t ReaderBlocks(uint8_t* base, uint32_t reader) {
  const uint32_t stream=GuardedWord(base,reader+0x38);
  return stream && stream!=reader+0x38 ? GuardedWord(base,stream+0x10) : 0xDEADDEAD;
}
}
extern "C" REX_FUNC(sub_82EC00D8) {
  const uint32_t reader=ctx.r3.u32;
  const int traced=SpeechLog() ? TracedIndex(traced_readers,reader) : -1;
  if(traced>=0 && LogReader(traced)) {
    const uint32_t request=GuardedWord(base,reader+0x7C);
    const std::string path=request ? GuestBaseName(base,request+0x14) : "", open=GuestBaseName(base,reader+0x90);
    REXLOG_INFO("skate2-speech: reader {} kick request={:08X} handle={:08X} state={} mode={} offset={:X} file='{}' "
                "open='{}' same={} fh={:08X} flags={:X} thread={} window={:X}-{:X} blocks={}",
                ReaderName(traced),request,request ? GuardedWord(base,request) : 0,
                request ? GuardedWord(base,request+4) : 0,request ? GuardedWord(base,request+0x10) : 0,
                request ? GuardedWord(base,request+0x124) : 0,path,open,request && path==open,
                GuardedWord(base,reader+0x190),GuardedWord(base,reader+4),GuardedWord(base,reader+0x54),
                GuardedWord(base,reader+0x70),GuardedWord(base,reader+0x74),ReaderBlocks(base,reader));
  }
  __imp__sub_82EC00D8(ctx,base);
}
// Open completion: a null handle fails the request (82EBFB40).
extern "C" REX_FUNC(sub_82EBFE90) {
  const uint32_t reader=SpeechLog() ? GuardedWord(base,ctx.r3.u32+0x18) : 0;
  const int traced=SpeechLog() ? TracedIndex(traced_readers,reader) : -1;
  __imp__sub_82EBFE90(ctx,base);
  if(traced>=0 && LogReader(traced))
    REXLOG_INFO("skate2-speech: reader {} open done fh={:08X} size={:X} file='{}' thread={}",ReaderName(traced),
                GuardedWord(base,reader+0x190),GuardedWord(base,reader+0x20C),GuestBaseName(base,reader+0x90),
                GuardedWord(base,reader+0x54));
}
extern "C" REX_FUNC(sub_82A87738) {
  const int traced=SpeechLog() ? TracedIndex(traced_readers,ctx.r3.u32-0x1A0) : -1;
  if(traced>=0 && LogReader(traced))
    REXLOG_INFO("skate2-speech: reader {} close fh={:08X}",ReaderName(traced),ctx.r4.u32);
  __imp__sub_82A87738(ctx,base);
}
extern "C" REX_FUNC(sub_82A87820) {
  const int traced=SpeechLog() ? TracedIndex(traced_readers,ctx.r3.u32-0x1A0) : -1;
  if(traced>=0 && LogReader(traced))
    REXLOG_INFO("skate2-speech: reader {} read fh={:08X} buffer={:08X} position={:X} size={:X}",ReaderName(traced),
                ctx.r4.u32,ctx.r5.u32,ctx.r6.u64,ctx.r7.u32);
  __imp__sub_82A87820(ctx,base);
}
// Read completion: advances the window and parses blocks (82EBFBC0).
extern "C" REX_FUNC(sub_82EBFFA0) {
  const uint32_t reader=ctx.r3.u32;
  const int traced=SpeechLog() ? TracedIndex(traced_readers,reader) : -1;
  const uint32_t before=traced>=0 ? GuardedWord(base,reader+0x74) : 0;
  __imp__sub_82EBFFA0(ctx,base);
  if(traced<0 || !LogReader(traced)) return;
  const uint32_t request=GuardedWord(base,reader+0x7C), start=GuardedWord(base,reader+0x70);
  REXLOG_INFO("skate2-speech: reader {} read done bytes={} position={:X} eof={} request_state={} window={:X}-{:X} "
              "head={:08X} {:08X} blocks={} thread={}",ReaderName(traced),int32_t(GuardedWord(base,reader+0x74)-before),
              GuardedWord(base,reader+0x19C),request ? GuardedByte(base,request+0x12C) : 0xEE,
              request ? GuardedWord(base,request+4) : 0,start,GuardedWord(base,reader+0x74),
              GuardedWord(base,start),GuardedWord(base,start+4),ReaderBlocks(base,reader),GuardedWord(base,reader+0x54));
}
extern "C" REX_FUNC(sub_82EBFBC0) {
  const uint32_t reader=ctx.r3.u32;
  const int traced=SpeechLog() ? TracedIndex(traced_readers,reader) : -1;
  __imp__sub_82EBFBC0(ctx,base);
  if(traced>=0 && LogReader(traced))
    REXLOG_INFO("skate2-speech: reader {} parse result={} window={:X}-{:X} blocks={}",ReaderName(traced),ctx.r3.u32,
                GuardedWord(base,reader+0x70),GuardedWord(base,reader+0x74),ReaderBlocks(base,reader));
}
extern "C" REX_FUNC(sub_82EBFB40) {
  const int traced=SpeechLog() ? TracedIndex(traced_readers,ctx.r3.u32) : -1;
  if(traced>=0 && LogReader(traced))
    REXLOG_INFO("skate2-speech: reader {} fail request={:08X} handle={:08X}",ReaderName(traced),ctx.r4.u32,
                GuardedWord(base,ctx.r4.u32));
  __imp__sub_82EBFB40(ctx,base);
}


float skate3::pedestrian_speech::ScaleVoiceGain(uint8_t* base, uint32_t voice_instance,
                                               uint32_t channel, float gain) {
  if (channel >= 2) return gain;
  // 82C5DC80 copies request+0x10 into (266+channel)*4 on every
  // accepted start. 82C5CEF0 holds the original audio lock at this call.
  const uint32_t bank = Read(base, voice_instance + 0x428 + channel * 4);
  const bool pedestrian = bank == 1 || (bank == 4 &&
      voice_instance == instance.load() && channel == kSpeechChannel);
  if (!pedestrian) return gain;
  const double volume = REXCVAR_GET(skate3_pedestrian_voice_volume);
  if (!std::isfinite(volume)) return gain;
  return gain * float(std::clamp(volume, 0.0, 3.0));
}
