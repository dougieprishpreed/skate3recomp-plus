// Pedestrian behaviour diagnostics (skate3_ped_behaviour_log, default off).
//
// Research (HANDOFF round 256): in New San Van a ped that is hit warns but
// never chases, and street security guards, whose records carry the taser
// ("tazr" hand prop, chase record "street"), never taze. Skate 3's ped AI
// graph (miscload.big data/state/livingworldentities/pedestrian/aigraph):
//   hit -> want "angrychase" -> ChaseChasee; when a chase path ends short of
//   the target, sub_82E31D58 sets bit 0x08 of [[ped+0x1708]+0xCD0];
//   CannotReachChaseTarget (sub_826AD640) reads that bit and the ped enters
//   ChaseTargetUnreachable: it stops and warns, then, if
//   CanTazeUnreachableTargets (sub_826AD660: [ped+0x1650] set and byte
//   [[ped+0x1654]+9]), it draws the taser (DrawTazer enter sub_826A8258 plays
//   "TazerInto", its update attaches "tazr").
// New San Van's walkable areas leave out roads, so a skater on the street is
// usually unreachable. EB001A world payloads (benches, ATMs, fountains: the
// interaction points) are relocated on load by sub_82964790.
//
// These hooks only read guest memory and log: one line per change per ped,
// and one per loaded interaction payload. No behaviour changes.
#include "generated/skate3_init.h"
#include "native/skate3_native_guest_read.h"
#include "skate3_world_loading.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <iterator>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <rex/cvar.h>
#include <rex/logging.h>

REXCVAR_DEFINE_BOOL(skate3_ped_taser_draw_fix, true, "Skate 3",
                    "Pedestrians: finish a taser draw that has stalled for a second (the "
                    "taser never appears in the ped's hand and the draw is retried every few "
                    "seconds). Applies on every map");
REXCVAR_DEFINE_BOOL(skate3_ped_taser_arm_fix, true, "Skate 3",
                    "Pedestrians: a ped that drew its taser aims it a second after the draw even "
                    "when another animation keeps its body busy (the taser lady lifted her hand, "
                    "never aimed and put the taser away). Applies on every map");
REXCVAR_DEFINE_BOOL(skate3_ped_hand_prop_slots, true, "Skate 3",
                    "Pedestrians: keep 2 of the living world's 49 movable-object slots free for what "
                    "peds hold (coffee, phones, newspapers, the taser). Skate 3 reserves them only in "
                    "some game states; elsewhere busy streets give all 49 to movable street props and "
                    "peds hold nothing. Costs at most 2 movable street props nearby. Every map")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DEFINE_BOOL(skate3_skate2_ped_waypoints, true, "Skate 3",
                    "New San Van peds: let Skate 2's ped types use the city's benches, bus shelters, "
                    "ATMs, vending machines, fountains, bins and newspaper boxes (their lists named "
                    "renamed copies of those points, which the city never uses). False = they ignore "
                    "them");
REXCVAR_DEFINE_BOOL(skate3_skate2_ped_moods, true, "Skate 3",
                    "New San Van peds: give Skate 2's ped types their reactions (noticing you, "
                    "warnings, anger, chases, guards). Skate 3 lists which ped types react to what "
                    "once at startup, before New San Van's ped types are loaded, so without this "
                    "they ignore everything. False = leave them unreactive");
REXCVAR_DEFINE_BOOL(skate3_ped_behaviour_log, false, "Skate 3",
                    "Diagnostics: log pedestrian chase, warning and taser decisions (one line "
                    "per change per ped) and interaction points (benches, ATMs, fountains) as "
                    "they load. Off by default.")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);

std::string WaypointSummary();  // defined with the interaction hooks below
std::string NoticeSummary();    // defined with the interaction hooks below
std::string HandPropSummary();  // defined with the hand prop hooks below

namespace {

bool Read32(uint8_t* base, uint32_t address, uint32_t& value) {
  if (address < 0x10000 || address > 0xfffffffb ||
      !skate3::native_scene::GuestTryCopy(&value, base + address, sizeof(value))) {
    return false;
  }
  value = __builtin_bswap32(value);
  return true;
}

bool Read8(uint8_t* base, uint32_t address, uint8_t& value) {
  return address >= 0x10000 && address != 0xffffffffu &&
         skate3::native_scene::GuestTryCopy(&value, base + address, 1);
}

bool Enabled() { return REXCVAR_GET(skate3_ped_behaviour_log); }

// Call counters, reported every 15 s while the log is on: they prove which
// hooks the game actually reaches (log 338: Downtown peds chased and tazed,
// yet only DrawTazer and the waypoint/payload hooks ever logged).
enum Hook : uint32_t {
  kHookChasePath, kHookCannotReach, kHookCanTaze, kHookWantCheck, kHookWantSet,
  kHookDrawTaser, kHookPayload, kHookWaypoint, kHookCount
};
constexpr const char* kHookNames[kHookCount] = {
    "chase-path-end", "cannot-reach", "can-taze", "want-check",
    "want-set",       "draw-taser",   "payload",  "waypoint"};
std::atomic<uint32_t> g_calls[kHookCount];
std::atomic<int64_t> g_next_summary_us{0};
// Per-ped mood updates (sub_82E37F68), index 0 = Skate 3 maps, 1 = New San Van.
std::atomic<uint32_t> g_mood_updates[2], g_mood_with_events[2], g_mood_events[2];
std::atomic<uint32_t> g_known[2];  // entries of the ped's knowledge list [board+0x270..0x274)
std::atomic<uint32_t> g_messages[2];  // ped AI message handler sub_82E38FB8 calls
std::atomic<uint32_t> g_message_samples[2];
std::atomic<uint32_t> g_knowledge_resets[2];  // sub_82E349B8: knowledge list cleared
std::atomic<uint32_t> g_messages_with_sender[2];  // message +0x4C -> +0x20 set (handler needs it)
std::atomic<uint32_t> g_classify[2][9];       // sub_82E38400 results 0..7, 8 = other
std::atomic<uint32_t> g_event_samples{0};
// Knowledge pipeline (log 343: NSV class 4/5 messages all carry a sender, yet
// no ped ever knows anyone). Handler sender lookups (sub_82965630 from
// 82E39064 "IActor" and 82E390D8 "IChasee"; a missing IChasee drops the
// message), knowledge-add path sub_82E42868 by caller, and the add itself
// sub_82E42690(board, handle): a handle word -1 or a negative second word is
// rejected before anything is stored.
std::atomic<uint32_t> g_sender_actor[2][2], g_sender_chasee[2][2];  // [map][found]
std::atomic<uint32_t> g_knowledge_adds[2][2];                       // [map][handle valid]
std::atomic<uint32_t> g_knowledge_scans[2];                         // sub_82E41B98 calls
std::mutex g_add_path_mutex;
std::unordered_map<uint32_t, uint32_t> g_add_path[2];               // caller lr -> calls
// Log 344: NSV senders resolve (IActor/IChasee found) but nothing is added.
// After the sender block the handler switches on the class: 4 = light hit
// (only when [[this+0x167C]+0x8C] == 7 and the +0x764 object's byte +0xA is
// clear: 82453298 at 82E3949C, reaction 82E32768 at 82E395A0), 5 = impact
// (byte +9 clear, then vault thresholds; 82453298 at 82E39810 once past them,
// 826C2C70 at 82E39960 deeper in). Per map: how far class 4/5 messages get.
enum HandlerStep : uint32_t {
  kStep4Gates, kStep4Reaction, kStep5Thresholds, kStep5Deep, kStepCount
};
constexpr const char* kStepNames[kStepCount] = {"class4 past gates", "class4 reaction",
                                                "class5 past thresholds", "class5 deeper"};
std::atomic<uint32_t> g_handler_steps[2][kStepCount];
std::atomic<uint32_t> g_class4_state[2][17];  // [[this+0x167C]+0x8C] for class 4 messages (16 = other)
thread_local int t_last_class = -1;            // classifier result of the message being handled
// Sit / ATM plugin start (DisableCollisionsWithBehaviourSource enter, vtable
// 82309A60 slot 12 = sub_826A7378) and ExitPlugin enter (vtable 8230A258 slot
// 12 = sub_8269F1B8): whether a ped that passes the transfer checks enters.
std::atomic<uint32_t> g_plugin_starts[2], g_plugin_exits[2];
// Log 345: in New San Van no ped ever has a mood event (Skate 3 maps: ~800
// updates with events per 15 s), so knowledge, aggro and chases never start.
// Events reach a ped only through the virtual pushers sub_82E3D198 (stimulus
// record: +0x10 type, +0x30/+0x38 handles) and sub_82E3DA40, both into the
// wrapper sub_82E41060(board, event), which drops the event unless a global
// system check passes (vtable +44 of the object at [0x830854C8]), the board's
// byte +3278 bit 0x08 is clear and the filter sub_82E41400 returns 0; then
// sub_82E414D8 stores it. Per map: pusher calls by caller (the dispatcher) and
// event type, and how many events get past each gate.
std::atomic<uint32_t> g_pusher_calls[2][2];  // [map][0 = 82E3D198, 1 = 82E3DA40]
std::unordered_map<uint32_t, uint32_t> g_pusher_callers[2];  // caller lr -> calls (g_add_path_mutex)
std::unordered_map<uint32_t, uint32_t> g_pusher_types[2];    // 82E3D198 event +0x10 -> calls
std::atomic<uint32_t> g_wrap_entries[2], g_wrap_blocked_bit[2], g_wrap_filtered[2], g_wrap_stored[2];
// Log 346: NSV pushers run (thousands of events / 15 s, same callers 82E3CC58,
// 82BFE940, 82E3CDC8 as Skate 3) and pass the filter, but none is stored
// (Skate 3: ~1%). After the filter the wrapper asks the mood manager
// ([0x830854C8], vtable +16) with the ped key u64 [[[board+3108]+2020]+224]
// (type-10 events skip that), then the perception test sub_82E27240 (core
// sub_82E26F38), then sub_82E41750 before the store. Log 347: NSV events never
// reached the perception test (fixed by RegisterNsvPedMoods below); the
// perception/slot counters were removed after that log.
std::unordered_map<uint64_t, uint32_t> g_ped_keys[2];  // ped key -> wrapper entries (g_add_path_mutex)
std::atomic<bool> g_manager_logged{false};

std::string KnowledgeSummary() {
  std::string text;
  for (int map = 0; map < 2; ++map) {
    auto take = [](std::atomic<uint32_t>& a) { return std::to_string(a.exchange(0, std::memory_order_relaxed)); };
    text += std::string(map ? "; nsv" : "s3") + " sender IActor found/missing=" +
            take(g_sender_actor[map][1]) + "/" + take(g_sender_actor[map][0]) +
            " IChasee found/missing=" + take(g_sender_chasee[map][1]) + "/" +
            take(g_sender_chasee[map][0]) + " knowledge scans=" + take(g_knowledge_scans[map]) +
            " adds valid/invalid handle=" + take(g_knowledge_adds[map][1]) + "/" +
            take(g_knowledge_adds[map][0]) + " add path callers=";
    std::lock_guard<std::mutex> lock(g_add_path_mutex);
    bool first = true;
    for (const auto& [lr, calls] : g_add_path[map]) {
      char entry[32];
      std::snprintf(entry, sizeof(entry), "%s%08X:%u", first ? "" : ",", lr, calls);
      text += entry;
      first = false;
    }
    if (first) text += "none";
    g_add_path[map].clear();
    for (uint32_t s = 0; s < kStepCount; ++s) {
      text += std::string(" ") + kStepNames[s] + "=" + take(g_handler_steps[map][s]);
    }
    text += " class4 ped states=";
    bool any = false;
    for (int s = 0; s < 17; ++s) {
      const uint32_t n = g_class4_state[map][s].exchange(0, std::memory_order_relaxed);
      if (!n) continue;
      text += std::string(any ? "," : "") + (s == 16 ? "other" : std::to_string(s)) + ":" + std::to_string(n);
      any = true;
    }
    if (!any) text += "none";
    text += " plugin starts/exits=" + take(g_plugin_starts[map]) + "/" + take(g_plugin_exits[map]);
    text += " mood pushers 82E3D198/82E3DA40=" + take(g_pusher_calls[map][0]) + "/" +
            take(g_pusher_calls[map][1]) + " pusher callers=";
    auto histogram = [&text](std::unordered_map<uint32_t, uint32_t>& counts) {
      bool none = true;
      for (const auto& [key, calls] : counts) {
        char entry[32];
        std::snprintf(entry, sizeof(entry), "%s%08X:%u", none ? "" : ",", key, calls);
        text += entry;
        none = false;
      }
      if (none) text += "none";
      counts.clear();
    };
    histogram(g_pusher_callers[map]);
    text += " event types=";
    histogram(g_pusher_types[map]);
    text += " push wrapper entries=" + take(g_wrap_entries[map]) + " blocked bit=" +
            take(g_wrap_blocked_bit[map]) + " reached filter=" + take(g_wrap_filtered[map]) +
            " stored=" + take(g_wrap_stored[map]);
    text += " ped keys=";
    bool none = true;
    for (const auto& [key, calls] : g_ped_keys[map]) {
      char entry[40];
      std::snprintf(entry, sizeof(entry), "%s%016llX:%u", none ? "" : ",",
                    static_cast<unsigned long long>(key), calls);
      text += entry;
      none = false;
    }
    if (none) text += "none";
    g_ped_keys[map].clear();
  }
  return text;
}

// Mood update counts for the 15 s summary.
std::string MoodSummary() {
  std::string text;
  for (int map = 0; map < 2; ++map) {
    text += std::string(map ? "; nsv" : "s3") + " mood updates=" +
            std::to_string(g_mood_updates[map].exchange(0, std::memory_order_relaxed)) +
            " with events=" +
            std::to_string(g_mood_with_events[map].exchange(0, std::memory_order_relaxed)) +
            " events=" + std::to_string(g_mood_events[map].exchange(0, std::memory_order_relaxed)) +
            " known entities summed=" +
            std::to_string(g_known[map].exchange(0, std::memory_order_relaxed)) +
            " messages=" + std::to_string(g_messages[map].exchange(0, std::memory_order_relaxed)) +
            " with sender=" +
            std::to_string(g_messages_with_sender[map].exchange(0, std::memory_order_relaxed)) +
            " knowledge resets=" +
            std::to_string(g_knowledge_resets[map].exchange(0, std::memory_order_relaxed)) +
            " message classes(0..7,other)=";
    for (int c = 0; c < 9; ++c) {
      text += (c ? "," : "") + std::to_string(g_classify[map][c].exchange(0, std::memory_order_relaxed));
    }
  }
  return text;
}

void Count(Hook hook) {
  g_calls[hook].fetch_add(1, std::memory_order_relaxed);
  const int64_t now = std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
  int64_t next = g_next_summary_us.load(std::memory_order_relaxed);
  if (now < next || !g_next_summary_us.compare_exchange_strong(next, now + 15'000'000)) return;
  if (next == 0) return;  // first call only arms the timer
  std::string counts;
  for (uint32_t i = 0; i < kHookCount; ++i) {
    counts += std::string(i ? ", " : "") + kHookNames[i] + "=" +
              std::to_string(g_calls[i].exchange(0, std::memory_order_relaxed));
  }
  REXLOG_INFO("ped-ai: hook calls in the last 15 s: {}; {}", counts, MoodSummary());
  REXLOG_INFO("ped-ai: interaction checks yes/no in the last 15 s: {}", WaypointSummary());
  REXLOG_INFO("ped-ai: interaction points noticed in the last 15 s: {}", NoticeSummary());
  REXLOG_INFO("ped-ai: knowledge pipeline in the last 15 s: {}", KnowledgeSummary());
  REXLOG_INFO("ped-ai: in the last 15 s: {}", HandPropSummary());
}

const char* MapTag() { return skate3::world_loading::IsSkate2Gameplay() ? "nsv" : "s3"; }

enum Kind : uint32_t {
  kPathEnd = 1,
  kCannotReach = 2,
  kCanTaze = 3,
  kWaypointAvailable = 4,
  kInFrontOfWaypoint = 5,
  kTazeFacing = 6,
  kTazeInRange = 7,
  kTazeDrawn = 8,
  kWantBase = 16,  // + want index
};

// Peds that drew a taser this session (DrawTazer enter); the fire-condition
// logs below are limited to them.
std::mutex g_taser_mutex;
std::unordered_map<uint32_t, int> g_taser_peds;
bool IsTaserPed(uint32_t ped) {
  std::lock_guard<std::mutex> lock(g_taser_mutex);
  return g_taser_peds.count(ped) != 0;
}

// Want indices: the order of the name table at 820C9080 ("none" ...
// "nearbyskatertrick"); they equal eMoodResult values (mood results
// skatercollisionchase = 1, skatercollisionwarn = 2, joinchase_* = 8).
constexpr const char* kWantNames[] = {
    "none",        "angrychase",    "warn",  "taunt",   "returngreet",
    "greet",       "startconversation", "alertto", "joinchase", "throwhandprop",
    "flee",        "startle",       "taze",  "nearbycollisionreaction", "slamreaction",
    "startspectate", "nearbyskatertrick"};

std::mutex g_mutex;
std::unordered_map<uint64_t, uint32_t> g_last;  // (ped, kind) -> last logged value

// True when this value differs from the last one logged for (ped, kind).
// quiet_zero: a first sighting with value 0 is recorded but not reported
// (every ped evaluates its want conditions as soon as it spawns).
bool Changed(uint32_t ped, Kind kind, uint32_t value, bool quiet_zero = false) {
  std::lock_guard<std::mutex> lock(g_mutex);
  if (g_last.size() > 8192) g_last.clear();  // peds come and go; bounded
  const auto [it, inserted] = g_last.try_emplace((uint64_t(ped) << 8) | kind, value);
  if (inserted) return !(quiet_zero && value == 0);
  if (it->second == value) return false;
  it->second = value;
  return true;
}

}  // namespace

// A chase path finished; statuses 2 and 3 set or clear the unreachable bit.
extern "C" REX_FUNC(sub_82E31D58) {
  const uint32_t self = ctx.r3.u32;
  const uint32_t status = ctx.r4.u32;
  __imp__sub_82E31D58(ctx, base);
  if (!Enabled()) return;
  Count(kHookChasePath);
  if (status != 2 && status != 3) return;
  uint32_t ped = 0, chase = 0;
  uint8_t flags = 0;
  if (!Read32(base, self + 8, ped) || !ped || !Read32(base, ped + 0x1708, chase) || !chase ||
      !Read8(base, chase + 0xCD0, flags)) {
    return;
  }
  const uint32_t unreachable = (flags >> 3) & 1u;
  if (Changed(ped, kPathEnd, (status << 1) | unreachable)) {
    REXLOG_INFO("ped-ai[{}]: ped {:08X} chase path ended (status {}): target {}", MapTag(), ped,
                status, unreachable ? "UNREACHABLE" : "reached or still reachable");
  }
}

// AI graph condition HasSpecificWantThatNeedsToBeAddressed (want="..."):
// the condition's +0x1C is the want index; the ped's wants are 12-byte
// entries at [ped+0x1708] + (index + 0x23) * 12, top bit = needs addressing.
extern "C" REX_FUNC(sub_826ACDE8) {
  const uint32_t self = ctx.r3.u32;
  const uint32_t context = ctx.r4.u32;
  __imp__sub_826ACDE8(ctx, base);
  if (!Enabled()) return;
  Count(kHookWantCheck);
  uint32_t ped = 0, want = 0;
  if (!Read32(base, context + 4, ped) || !ped || !Read32(base, self + 0x1C, want) || want > 63) {
    return;
  }
  const uint32_t result = ctx.r3.u32 & 1u;
  if (Changed(ped, Kind(kWantBase + want), result, /*quiet_zero=*/true)) {
    const char* name = want < std::size(kWantNames) ? kWantNames[want] : "?";
    REXLOG_INFO("ped-ai[{}]: ped {:08X} want {} ({}) {}", MapTag(), ped, name, want,
                result ? "ACTIVE" : "cleared");
  }
}

// AI graph condition CannotReachChaseTarget.
extern "C" REX_FUNC(sub_826AD640) {
  const uint32_t context = ctx.r4.u32;
  __imp__sub_826AD640(ctx, base);
  if (!Enabled()) return;
  Count(kHookCannotReach);
  uint32_t ped = 0;
  if (!Read32(base, context + 4, ped) || !ped) return;
  const uint32_t result = ctx.r3.u32 & 1u;
  if (Changed(ped, kCannotReach, result)) {
    REXLOG_INFO("ped-ai[{}]: ped {:08X} CannotReachChaseTarget = {}", MapTag(), ped,
                result ? "yes (stops and warns)" : "no");
  }
}

// AI graph condition CanTazeUnreachableTargets (security guards).
extern "C" REX_FUNC(sub_826AD660) {
  const uint32_t context = ctx.r4.u32;
  __imp__sub_826AD660(ctx, base);
  if (!Enabled()) return;
  Count(kHookCanTaze);
  uint32_t ped = 0, owner = 0, record = 0;
  if (!Read32(base, context + 4, ped) || !ped) return;
  Read32(base, ped + 0x1650, owner);
  Read32(base, ped + 0x1654, record);
  const uint32_t result = ctx.r3.u32 & 0xFFu;
  if (Changed(ped, kCanTaze, result)) {
    REXLOG_INFO("ped-ai[{}]: ped {:08X} CanTazeUnreachableTargets = {} (owner {:08X}, record {:08X})",
                MapTag(), ped, result ? "yes" : "no", owner, record);
  }
}

// Taser draw fix (skate3_ped_taser_draw_fix). DrawTazer enter starts the
// ped's AI timer 0x25 at 0.133 s (sub_82E40940(board, 0x25, f1)); its update
// (sub_826A8318) attaches "tazr" once sub_82E40A80(board, 0x25) returns <= 0.
// The timer only counts down in the ped's AI update (82E37F68 ->
// 82E40AF0(board, dt)); for a ped whose update stalls it never reaches 0
// and the taser never appears (logs 338/341/342: one Downtown ped drew 9 of
// 9 times without a taser; draws that work attach 0.13 s after the start).
// After a second the timer reads as expired for that one call site (the
// update's return address 826A834C), so the game's own code attaches it.
namespace {
std::mutex g_draw_mutex;
std::unordered_map<uint32_t, int64_t> g_draw_started_us;  // AI state ([ped+0x1708]) -> start
int64_t SteadyUs() {
  return std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}
}  // namespace

extern "C" REX_FUNC(sub_82E40A80) {
  const uint32_t board = ctx.r3.u32;
  const bool draw_check = uint32_t(ctx.lr) == 0x826A834Cu && ctx.r4.u32 == 0x25;
  __imp__sub_82E40A80(ctx, base);
  if (!draw_check || ctx.f1.f64 <= 0.0 || !REXCVAR_GET(skate3_ped_taser_draw_fix)) return;
  int64_t started = 0;
  {
    std::lock_guard<std::mutex> lock(g_draw_mutex);
    const auto it = g_draw_started_us.find(board);
    if (it == g_draw_started_us.end()) return;
    started = it->second;
  }
  if (SteadyUs() - started < 1'000'000) return;
  ctx.f1.f64 = 0.0;
  {
    std::lock_guard<std::mutex> lock(g_draw_mutex);
    g_draw_started_us.erase(board);
  }
  if (Enabled()) {
    REXLOG_INFO("ped-ai[{}]: state {:08X} taser draw stalled for 1 s; finishing it", MapTag(),
                board);
  }
}

// AI graph behaviour DrawTazer: enter (plays TazerInto).
extern "C" REX_FUNC(sub_826A8258) {
  const uint32_t context = ctx.r4.u32;
  __imp__sub_826A8258(ctx, base);
  if (REXCVAR_GET(skate3_ped_taser_draw_fix)) {
    uint32_t draw_ped = 0, draw_board = 0;
    if (Read32(base, context + 4, draw_ped) && draw_ped &&
        Read32(base, draw_ped + 0x1708, draw_board) && draw_board) {
      std::lock_guard<std::mutex> lock(g_draw_mutex);
      if (g_draw_started_us.size() > 256) g_draw_started_us.clear();
      g_draw_started_us[draw_board] = SteadyUs();
    }
  }
  if (!Enabled()) return;
  Count(kHookDrawTaser);
  // Enter stores byte [behaviour state + 8] = ([ped+0x1720] == 0); the update
  // (sub_826A8318) attaches "tazr" after TazerInto only while that byte is
  // set. Only the want="taze" route drops a held prop first (DropHandProp);
  // the ChaseTargetUnreachable route does not. The user's failing Downtown
  // ped (log 340) held nothing visible and was not a guard, so log the slot
  // and the attach flag rather than assume a held prop is the cause.
  uint32_t ped = 0, board = 0, state = 0, held = 0;
  uint8_t will_attach = 0;
  Read32(base, context + 4, ped);
  Read32(base, context + 8, state);
  if (ped) {
    Read32(base, ped + 0x1708, board);
    Read32(base, ped + 0x1720, held);
  }
  if (state) Read8(base, state + 8, will_attach);
  if (ped) {
    std::lock_guard<std::mutex> lock(g_taser_mutex);
    if (g_taser_peds.size() > 256) g_taser_peds.clear();
    g_taser_peds[ped] = 1;
  }
  REXLOG_INFO("ped-ai[{}]: ped {:08X} (state {:08X}) draws its taser; hand holds {:08X}, taser {}",
              MapTag(), ped, board, held,
              will_attach ? "will attach" : "WILL NOT attach (hand not empty)");
}

// Per-ped AI update (sub_82E40208 -> here, f1 = dt): turns the queued mood
// events at [board+0x410 .. +0x414) (0x70 bytes each) into wants
// (82E418E8, then 82E41B98). Counts updates and events per map; samples a
// few event entries in New San Van, where no want is ever set (log 340).
extern "C" REX_FUNC(sub_82E37F68) {
  const uint32_t self = ctx.r3.u32;
  if (Enabled()) {
    uint32_t board = 0, first = 0, last = 0;
    if (Read32(base, self + 0x16E4, board) && board && Read32(base, board + 0x410, first) &&
        Read32(base, board + 0x414, last) && last >= first && last - first < 0x70 * 256) {
      const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
      const uint32_t events = (last - first) / 0x70;
      g_mood_updates[map].fetch_add(1, std::memory_order_relaxed);
      uint32_t known_first = 0, known_last = 0;
      if (Read32(base, board + 0x270, known_first) && Read32(base, board + 0x274, known_last) &&
          known_last >= known_first && known_last - known_first < 0x50 * 1024) {
        g_known[map].fetch_add((known_last - known_first) / 0x50, std::memory_order_relaxed);
      }
      if (events) {
        g_mood_with_events[map].fetch_add(1, std::memory_order_relaxed);
        g_mood_events[map].fetch_add(events, std::memory_order_relaxed);
        if (g_event_samples.fetch_add(1, std::memory_order_relaxed) < 60) {
          uint32_t w[12] = {};
          for (uint32_t i = 0; i < 12; ++i) Read32(base, first + i * 4, w[i]);
          REXLOG_INFO("ped-ai[{}]: state {:08X} has {} mood event(s); first: {:08X} {:08X} {:08X} "
                      "{:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X}",
                      MapTag(), board, events, w[0], w[1], w[2], w[3], w[4], w[5], w[6], w[7],
                      w[8], w[9], w[10], w[11]);
        }
      }
    }
  }
  __imp__sub_82E37F68(ctx, base);
}

// Ped AI message handler (virtual; this = r3, AI owner = r3 - 0x88, message
// = r4): it asks the owner's vfunc +0xA4 for a state and only handles a
// message when that state is 4 or 5 and message byte +0x50 is clear; it then
// looks up the sender's components (sub_82965630 types 82586478, 8258E6B8)
// and records knowledge/mood events. Log 341: NSV peds' knowledge and event
// lists stay empty. Counts calls per map; samples the message words.
extern "C" REX_FUNC(sub_82E38FB8) {
  const uint32_t self = ctx.r3.u32;
  const uint32_t message = ctx.r4.u32;
  if (Enabled()) {
    const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
    g_messages[map].fetch_add(1, std::memory_order_relaxed);
    uint32_t body = 0, sender_entity = 0;
    if (Read32(base, message + 0x4C, body) && body && Read32(base, body + 0x20, sender_entity) &&
        sender_entity) {
      g_messages_with_sender[map].fetch_add(1, std::memory_order_relaxed);
    }
    if (g_message_samples[map].fetch_add(1, std::memory_order_relaxed) < 30) {
      uint32_t vtable = 0, sender = 0, owner_vtable = 0, state_fn = 0;
      uint8_t handled_flag = 0;
      Read32(base, message, vtable);
      Read32(base, message + 0x4C, sender);
      Read8(base, message + 0x50, handled_flag);
      Read32(base, self - 0x88, owner_vtable);
      if (owner_vtable) Read32(base, owner_vtable + 0xA4, state_fn);
      REXLOG_INFO("ped-ai[{}]: ped AI {:08X} message {:08X} (vtable {:08X}) sender {:08X} flag {} "
                  "(owner state function {:08X})",
                  MapTag(), self, message, vtable, sender, handled_flag, state_fn);
    }
  }
  if (!Enabled()) {
    __imp__sub_82E38FB8(ctx, base);
    return;
  }
  uint32_t state_object = 0, ped_state = 0xFFFFFFFFu;
  if (Read32(base, self + 0x167C, state_object) && state_object) Read32(base, state_object + 0x8C, ped_state);
  t_last_class = -1;
  __imp__sub_82E38FB8(ctx, base);
  if (t_last_class == 4) {
    const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
    g_class4_state[map][ped_state < 16 ? ped_state : 16].fetch_add(1, std::memory_order_relaxed);
  }
}

namespace {
void CountStep(HandlerStep step) {
  g_handler_steps[skate3::world_loading::IsSkate2Gameplay() ? 1 : 0][step].fetch_add(
      1, std::memory_order_relaxed);
}
}  // namespace

extern "C" REX_FUNC(sub_82453298) {
  const uint32_t lr = uint32_t(ctx.lr);
  __imp__sub_82453298(ctx, base);
  if (!Enabled()) return;
  if (lr == 0x82E394A0u) CountStep(kStep4Gates);
  else if (lr == 0x82E39814u) CountStep(kStep5Thresholds);
}

extern "C" REX_FUNC(sub_826C2C70) {
  const uint32_t lr = uint32_t(ctx.lr);
  __imp__sub_826C2C70(ctx, base);
  if (lr == 0x82E39964u && Enabled()) CountStep(kStep5Deep);
}

extern "C" REX_FUNC(sub_82E32768) {
  const uint32_t lr = uint32_t(ctx.lr);
  __imp__sub_82E32768(ctx, base);
  if (lr == 0x82E395A4u && Enabled()) CountStep(kStep4Reaction);
}

extern "C" REX_FUNC(sub_826A7378) {  // DisableCollisionsWithBehaviourSource enter (plugin start)
  if (Enabled()) {
    g_plugin_starts[skate3::world_loading::IsSkate2Gameplay() ? 1 : 0].fetch_add(
        1, std::memory_order_relaxed);
  }
  __imp__sub_826A7378(ctx, base);
}

extern "C" REX_FUNC(sub_8269F1B8) {  // ExitPlugin enter
  if (Enabled()) {
    g_plugin_exits[skate3::world_loading::IsSkate2Gameplay() ? 1 : 0].fetch_add(
        1, std::memory_order_relaxed);
  }
  __imp__sub_8269F1B8(ctx, base);
}

// Owner message classification (ped AI owner vtable +0xA4, sub_82E38400;
// r3 = owner, r4 = message): the handler above records knowledge and mood
// events only when this returns 4 or 5. Histogram per map.
extern "C" REX_FUNC(sub_82E38400) {
  __imp__sub_82E38400(ctx, base);
  if (!Enabled()) return;
  const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
  const uint32_t result = ctx.r3.u32;
  t_last_class = int(result);
  g_classify[map][result < 8 ? result : 8].fetch_add(1, std::memory_order_relaxed);
}

// Knowledge reset (vtable 8232BF98 slot 23): clears [board+0x270..0x274)
// (end = begin). Counted per map: NSV knowledge lists are always empty.
extern "C" REX_FUNC(sub_82E349B8) {
  if (Enabled()) {
    const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
    g_knowledge_resets[map].fetch_add(1, std::memory_order_relaxed);
  }
  __imp__sub_82E349B8(ctx, base);
}

// Fire conditions of pedestrian_wanttotaze.xml (DrawTazer -> TazeEntity needs
// all four within the draw's 10 s timer, else EndTaze, UnsetWant and the ped
// draws again later: log 344, one Downtown lady drew 5 times in 30 s without
// reaching TazerOut): IsFacingWantTarget (angle 20, distance 10; vtable
// 8230AF7C slot 12 = sub_826AA1B8), HasLineOfSightToTazeTarget (bit 0x10 of
// [[ped+0x1708]+0xCD0], sub_826AD620; set by the box range test in
// sub_82E23D68 from the taser ray), HasDrawnTazer (sub_826AD2F8), plus the
// 1.5 s TazerWaitTime. One line per change per taser ped.
namespace {
void LogTazeCondition(uint8_t* base, uint32_t context, uint32_t result, Kind kind, const char* what) {
  uint32_t ped = 0;
  if (!Enabled() || !Read32(base, context + 4, ped) || !ped || !IsTaserPed(ped)) return;
  if (Changed(ped, kind, result & 1u)) {
    REXLOG_INFO("ped-ai[{}]: taser ped {:08X} {} = {}", MapTag(), ped, what, (result & 1u) ? "yes" : "NO");
  }
}
}  // namespace

extern "C" REX_FUNC(sub_826AA1B8) {  // IsFacingWantTarget
  const uint32_t context = ctx.r4.u32;
  __imp__sub_826AA1B8(ctx, base);
  LogTazeCondition(base, context, ctx.r3.u32, kTazeFacing, "facing its target (within 20 deg, 10 m)");
}

extern "C" REX_FUNC(sub_826AD620) {  // HasLineOfSightToTazeTarget
  const uint32_t context = ctx.r4.u32;
  __imp__sub_826AD620(ctx, base);
  LogTazeCondition(base, context, ctx.r3.u32, kTazeInRange, "target in taze range (bit 0x10)");
}

extern "C" REX_FUNC(sub_826AD2F8) {  // HasDrawnTazer
  const uint32_t context = ctx.r4.u32;
  __imp__sub_826AD2F8(ctx, base);
  LogTazeCondition(base, context, ctx.r3.u32, kTazeDrawn, "taser drawn");
}

// TazerOut (sub_826A85A8, r4 = context, [context+4] = ped): the ped puts the
// taser away (sets [[ped+0x1708]+0xCC0] = 3). Log 343: Downtown taser peds
// drew, attached the taser 0.13 s later and drew again every few seconds
// without firing. Firing needs HasLineOfSightToTazeTarget = bit 0x10 of
// [[ped+0x1708]+0xCD0] (sub_826AD620), set by sub_82E23D68 from the taser ray
// (see LogTaserRays below: the ray must come back untouched). Logs the combat
// flags and the time since the draw at each put-away.
extern "C" REX_FUNC(sub_826A85A8) {
  const uint32_t context = ctx.r4.u32;
  if (Enabled()) {
    uint32_t ped = 0, board = 0, taser_state = 0;
    uint8_t flags = 0;
    if (Read32(base, context + 4, ped) && ped && Read32(base, ped + 0x1708, board) && board) {
      Read8(base, board + 0xCD0, flags);
      Read32(base, board + 0xCC0, taser_state);
      int64_t since_ms = -1;
      {
        std::lock_guard<std::mutex> lock(g_draw_mutex);
        const auto it = g_draw_started_us.find(board);
        if (it != g_draw_started_us.end()) since_ms = (SteadyUs() - it->second) / 1000;
      }
      REXLOG_INFO("ped-ai[{}]: ped {:08X} puts its taser away {} ms after drawing: in taze range {}, "
                  "drawn {}, target unreachable {} (flags {:02X}, taser state {})",
                  MapTag(), ped, since_ms, (flags >> 4) & 1, (flags >> 6) & 1, (flags >> 3) & 1, flags,
                  taser_state);
    }
  }
  __imp__sub_826A85A8(ctx, base);
}

// The taser ray (HasLineOfSightToTazeTarget). The ped ray manager keeps two
// batches of 128-byte entries ([mgr+23776] gathering, [mgr+23780] reading;
// count +10256, entries from +16, raycaster object at +0) and sub_82E238C0
// swaps them every frame. sub_82E23B28 gathers each ped's requests (ped vtable
// +136, sub_82E363F0) and casts them through the raycaster (vtable +28:
// start +16, end +32, filter -1, mode 3, radius +92; returned byte -> +116),
// then kicks the batch (+8). Next frame sub_82E23C50 syncs (+12) and copies
// each result (+32(i)): hit point -> +48, normal -> +64, result words
// +100/+104 -> +84/+88, |hit - start| -> +80. Type 5 is the taser ray, queued
// while the taser is drawn (board +0xCD0 bit 0x40) in taze state 2: start =
// ped position ([[ped+5892]+19552]) + 0.5 * ped vtable+20 vector, end = the
// target's vtable+36 point, radius 0. sub_82E23D68 then sets bit 0x10 only if
// every component of |hit - end| <= FLT_EPSILON (0x82063A44): the ray must
// come back untouched. Log 349: the lady drew, never got bit 0x10 and put the
// taser away; she fires about once in ten sessions while retail fires
// reliably. One line per taser ray per ped every 0.5 s (start, end, hit,
// miss = largest |hit - end| component, result words) and the raycaster's
// functions once.
namespace {
float GuestF32(uint8_t* base, uint32_t address) {
  uint32_t bits = 0;
  Read32(base, address, bits);
  float value;
  std::memcpy(&value, &bits, 4);
  return value;
}

void LogTaserRays(PPCContext& ctx, uint8_t* base) {
  const uint32_t manager = ctx.r3.u32;
  uint32_t batch = 0, count = 0, caster = 0;
  if (!Read32(base, manager + 23780, batch) || !batch || !Read32(base, batch + 10256, count) || count > 80) return;
  Read32(base, batch, caster);
  static std::atomic<bool> caster_logged{false};
  static std::mutex mutex;
  static std::unordered_map<uint32_t, int64_t> last_us;
  static int lines = 0;
  // Rays from the first refused cast on were not cast in their frame.
  uint32_t first_refusal = count;
  for (uint32_t i = 0; i < count && first_refusal == count; ++i) {
    uint8_t ok = 1;
    Read8(base, batch + 16 + 128 * i + 116, ok);
    if (!ok) first_refusal = i;
  }
  for (uint32_t i = 0; i < count; ++i) {
    const uint32_t e = batch + 16 + 128 * i;
    uint32_t type = 0, handle = 0, w84 = 0, w88 = 0;
    uint8_t cast_ok = 0;
    if (!Read32(base, e + 96, type) || type != 5) continue;
    Read32(base, e + 100, handle);
    Read8(base, e + 116, cast_ok);
    Read32(base, e + 84, w84);
    Read32(base, e + 88, w88);
    float s[3], t[3], h[3], n[3];
    for (int k = 0; k < 3; ++k) {
      s[k] = GuestF32(base, e + 16 + 4 * k);
      t[k] = GuestF32(base, e + 32 + 4 * k);
      h[k] = GuestF32(base, e + 48 + 4 * k);
      n[k] = GuestF32(base, e + 64 + 4 * k);
    }
    float miss = 0.0f;
    for (int k = 0; k < 3; ++k) miss = std::max(miss, std::fabs(h[k] - t[k]));
    const float length = std::sqrt((t[0] - s[0]) * (t[0] - s[0]) + (t[1] - s[1]) * (t[1] - s[1]) +
                                   (t[2] - s[2]) * (t[2] - s[2]));
    const float hit_dist = GuestF32(base, e + 80);
    const int64_t now = SteadyUs();
    std::lock_guard<std::mutex> lock(mutex);
    if (!caster_logged.exchange(true) && caster) {
      uint32_t vt = 0, f4 = 0, f8 = 0, f12 = 0, f28 = 0, f32 = 0;
      Read32(base, caster, vt);
      Read32(base, vt + 4, f4);
      Read32(base, vt + 8, f8);
      Read32(base, vt + 12, f12);
      Read32(base, vt + 28, f28);
      Read32(base, vt + 32, f32);
      REXLOG_INFO("ped-ai: ped raycaster {:08X} vtable {:08X}: begin {:08X} kick {:08X} sync {:08X} cast {:08X} "
                  "result {:08X}", caster, vt, f4, f8, f12, f28, f32);
    }
    auto& last = last_us[handle];
    if (lines >= 600 || now - last < 500000) continue;
    last = now;
    ++lines;
    REXLOG_INFO("ped-ai[{}]: taser ray {:08X}: from ({:.3f}, {:.3f}, {:.3f}) to ({:.3f}, {:.3f}, {:.3f}) {:.2f} m; "
                "came back at ({:.3f}, {:.3f}, {:.3f}) normal ({:.2f}, {:.2f}, {:.2f}), hit distance {:.3f}, "
                "miss {:.6f} -> {} (ray {} of {}, {}, words {:08X} {:08X})",
                MapTag(), handle, s[0], s[1], s[2], t[0], t[1], t[2], length, h[0], h[1], h[2], n[0], n[1], n[2],
                hit_dist, miss, miss <= 1.1920929e-7f && cast_ok ? "CLEAR" : "blocked", i, count,
                i < first_refusal ? "cast" : "NOT CAST (raycaster full)", w84, w88);
  }
}
}  // namespace

extern "C" REX_FUNC(sub_82E23D68) {  // taser / ground ray results
  if (Enabled()) LogTaserRays(ctx, base);
  __imp__sub_82E23D68(ctx, base);
}

// Gather and cast (sub_82E23B28, r3 = ray manager, r4 = ped list of 8-byte
// handles). Each batch's raycaster comes from the collision service
// [0x8308547C] vtable +60 (16, 7, 1) in sub_82E237B0; the loop stops asking
// peds for rays at the first cast the raycaster refuses (returned byte 0), so
// peds late in the list then get no rays that frame, the taser ray included.
// Every 15 s: frames, peds in the list (max), rays queued (max), frames with
// a refused cast and the index of the first refusal (= the raycaster's
// capacity if constant), and taser rays cast.
namespace {
struct RayGatherStats {
  uint64_t frames = 0, refused_frames = 0, taser_rays = 0, taser_refused = 0;
  uint32_t max_peds = 0, max_rays = 0, first_refusal_min = ~0u, first_refusal_max = 0;
  int64_t since_us = 0;
} g_ray_stats;
std::mutex g_ray_stats_mutex;
}  // namespace

extern "C" REX_FUNC(sub_82E23B28) {
  const uint32_t manager = ctx.r3.u32, list = ctx.r4.u32;
  __imp__sub_82E23B28(ctx, base);
  if (!Enabled()) return;
  uint32_t batch = 0, count = 0, begin = 0, end = 0;
  if (!Read32(base, manager + 23776, batch) || !batch || !Read32(base, batch + 10256, count) || count > 80) return;
  Read32(base, list, begin);
  Read32(base, list + 4, end);
  const uint32_t peds = end >= begin ? (end - begin) / 8 : 0;
  // Rays from the first refusal on were not cast this frame (their +116 byte
  // can still hold an older frame's result flag).
  uint32_t first_refusal = ~0u, taser = 0, taser_refused = 0;
  for (uint32_t i = 0; i < count && first_refusal == ~0u; ++i) {
    uint8_t cast_ok = 0;
    Read8(base, batch + 16 + 128 * i + 116, cast_ok);
    if (!cast_ok) first_refusal = i;
  }
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t type = 0;
    Read32(base, batch + 16 + 128 * i + 96, type);
    if (type == 5) {
      ++taser;
      if (i >= first_refusal) ++taser_refused;
    }
  }
  const int64_t now = SteadyUs();
  std::lock_guard<std::mutex> lock(g_ray_stats_mutex);
  auto& s = g_ray_stats;
  if (s.since_us == 0) s.since_us = now;
  ++s.frames;
  s.max_peds = std::max(s.max_peds, peds);
  s.max_rays = std::max(s.max_rays, count);
  s.taser_rays += taser;
  s.taser_refused += taser_refused;
  if (first_refusal != ~0u) {
    ++s.refused_frames;
    s.first_refusal_min = std::min(s.first_refusal_min, first_refusal);
    s.first_refusal_max = std::max(s.first_refusal_max, first_refusal);
  }
  if (now - s.since_us >= 15000000) {
    REXLOG_INFO("ped-ai[{}]: ped rays in the last 15 s: {} frames, peds in list max {}, rays queued max {}, frames "
                "with a refused cast {} (first refusal at ray {}..{}), taser rays {} ({} refused)",
                MapTag(), s.frames, s.max_peds, s.max_rays, s.refused_frames,
                s.first_refusal_min == ~0u ? 0 : s.first_refusal_min, s.first_refusal_max, s.taser_rays,
                s.taser_refused);
    s = RayGatherStats{};
    s.since_us = now;
  }
}

// DrawTazer update (vtable slot 13): while byte [behaviour state + 8] is set
// it waits for TazerInto (sub_82E40A80(board, 0x25) <= 0) and then attaches
// "tazr" (sub_82E3DDA0) and clears the byte. Logs the first waiting update
// per ped and each attach, to see why the Downtown lady never gets her taser.
extern "C" REX_FUNC(sub_826A8318) {
  const uint32_t context = ctx.r4.u32;
  uint32_t ped = 0, state = 0;
  uint8_t before = 0, after = 0;
  const bool log = Enabled() && Read32(base, context + 4, ped) && Read32(base, context + 8, state) &&
                   state && Read8(base, state + 8, before);
  __imp__sub_826A8318(ctx, base);
  if (!log || !before) return;
  Read8(base, state + 8, after);
  if (!after) {
    REXLOG_INFO("ped-ai[{}]: ped {:08X} taser ATTACHED after TazerInto", MapTag(), ped);
  } else if (Changed(ped, Kind(kWantBase + 63), 1, /*quiet_zero=*/true)) {
    REXLOG_INFO("ped-ai[{}]: ped {:08X} waiting for TazerInto to finish before attaching the taser",
                MapTag(), ped);
  }
}

// Taser aiming (taze state 1 -> 2; skate3_ped_taser_arm_fix). DrawTazer enter
// (sub_826A8258) plays "TazerInto" (ped vtable +248 = +240 with no loop and
// 0.2 s blends) and sets [[ped+0x1708]+0xCC0] = 1. The ped update's tail
// sub_82E3ED50 (every AI update, from sub_82E37F68) then, while the taser is
// drawn (bit 0x40 of [board+0xCD0]) in state 1, asks the ped's animation
// channel object [[ped+5892]+17936] (vtable +36, channel "PedChannel" at
// 0x830CE530) how long its clip has left; at <= 0.2 s (0x82099280) it plays
// "TazerCyc" (vtable +240, loop 1, blends 0.2) and sets state 2. Only state 2
// queues the taser's line-of-sight ray (type 5, sub_82E363F0), so a ped stuck
// in state 1 never aims, never fires, and puts the taser away after its 10 s.
// Log 350: two of three Downtown taser peds sat in state 1 for their whole
// draw (taser attached 0.1 s after the draw, no ray ever queued); the one that
// fired reached state 2 0.35 s after drawing and all its rays came back clear.
// So the body channel stays busy with some other clip. A second after the draw
// this does what the game does at the end of TazerInto, and logs how long the
// channel claimed to have left (and the query function) to find that clip.
namespace {
constexpr uint32_t kPedChannelKey = 0x830CE530;  // "PedChannel" (static init sub_82F94330)
constexpr uint32_t kTazerCycName = 0x82064A94;   // "TazerCyc"
constexpr int64_t kArmStallUs = 1000000;

struct ArmWatch {
  int64_t since_us = 0;
  int64_t logged_us = 0;
};
std::mutex g_arm_mutex;
std::unordered_map<uint32_t, ArmWatch> g_arm;  // AI state ([ped+0x1708]) -> state 1 seen since
std::atomic<size_t> g_arm_count{0};

bool GuestCode(uint32_t address) { return address >= 0x82380000u && address < 0x83000000u; }

// Indirect guest calls on a copy of the caller's registers.
double CallGuestFloat(PPCContext ctx, uint8_t* base, uint32_t function) {
  REX_CALL_INDIRECT_FUNC(function);
  return ctx.f1.f64;
}
void CallGuest(PPCContext ctx, uint8_t* base, uint32_t function) { REX_CALL_INDIRECT_FUNC(function); }
}  // namespace

extern "C" REX_FUNC(sub_82E3ED50) {
  const uint32_t ped = ctx.r3.u32;
  __imp__sub_82E3ED50(ctx, base);
  uint32_t board = 0, state = 0;
  uint8_t flags = 0;
  if (!Read32(base, ped + 0x1708, board) || !board || !Read8(base, board + 0xCD0, flags) ||
      !Read32(base, board + 0xCC0, state)) {
    return;
  }
  const bool arming = (flags & 0x40) != 0 && state == 1;
  if (!arming && g_arm_count.load(std::memory_order_relaxed) == 0) return;
  const int64_t now = SteadyUs();
  ArmWatch watch;
  {
    std::lock_guard<std::mutex> lock(g_arm_mutex);
    if (!arming) {
      g_arm.erase(board);
      g_arm_count.store(g_arm.size(), std::memory_order_relaxed);
      return;
    }
    if (g_arm.size() > 64) g_arm.clear();
    const auto [it, fresh] = g_arm.try_emplace(board);
    if (fresh) it->second.since_us = now;
    g_arm_count.store(g_arm.size(), std::memory_order_relaxed);
    watch = it->second;
  }
  const int64_t waited_us = now - watch.since_us;
  const bool fix = REXCVAR_GET(skate3_ped_taser_arm_fix) && waited_us >= kArmStallUs;
  const bool log = Enabled() && waited_us >= 400000 && now - watch.logged_us >= 500000;
  if (!fix && !log) return;
  uint32_t character = 0, channel_vtable = 0, query = 0, anim_state = 0;
  double left = -1.0;
  if (Read32(base, ped + 5892, character) && character &&
      Read32(base, character + 17936, channel_vtable) && channel_vtable &&
      Read32(base, channel_vtable + 36, query) && GuestCode(query)) {
    auto call = ctx;
    call.r3.u64 = character + 17936;
    call.r4.u64 = kPedChannelKey;
    left = CallGuestFloat(call, base, query);
  }
  if (character) Read32(base, character + 140, anim_state);
  uint8_t action = 0, action2 = 0;
  Read8(base, board + 3278, action);
  Read8(base, board + 3279, action2);
  if (log) {
    {
      std::lock_guard<std::mutex> lock(g_arm_mutex);
      const auto it = g_arm.find(board);
      if (it != g_arm.end()) it->second.logged_us = now;
    }
    REXLOG_INFO("ped-ai[{}]: taser ped {:08X} drawn {} ms, not aiming yet: PedChannel clip has {:.2f} s left "
                "(aims at <= 0.20; query {:08X}), action flags {:02X} {:02X}, anim state {}",
                MapTag(), ped, waited_us / 1000, left, query, action, action2, anim_state);
  }
  if (!fix) return;
  uint32_t vtable = 0, play = 0;
  if (!Read32(base, ped, vtable) || !Read32(base, vtable + 240, play) || !GuestCode(play)) return;
  auto call = ctx;
  call.r3.u64 = ped;
  call.r4.u64 = kTazerCycName;
  call.r8.u64 = 1;
  call.f1.f64 = call.f2.f64 = call.f3.f64 = double(0.2f);
  CallGuest(call, base, play);
  REX_STORE_U32(board + 0xCC0, 2);
  {
    std::lock_guard<std::mutex> lock(g_arm_mutex);
    g_arm.erase(board);
    g_arm_count.store(g_arm.size(), std::memory_order_relaxed);
  }
  if (Enabled()) {
    REXLOG_INFO("ped-ai[{}]: taser ped {:08X} aims after {} ms (PedChannel claimed {:.2f} s left): TazerCyc, "
                "taze state 2", MapTag(), ped, waited_us / 1000, left);
  }
}

// Hand prop actions (the coffee cup, phone, newspaper... and the taser). Log
// 351 shows the real reason the taser lady never aimed by herself: the aiming
// code above runs only while the ped has an action ([ped+5920], bit 0x02 of
// [board+3278]), and her draws had none ("action flags 10"). DrawTazer's
// update attaches "tazr" with sub_82E3DDA0, which only sets the ped's hand
// prop record ([ped+5760], livingworld_handprops) and requests a start (bit
// 0x01 of [board+3279]); the manager pass sub_82E262D0 then calls
// sub_82E3DE18(ped, &prop key), which looks the prop's model up in the
// dynamic object model registry (handprops +40 -> dmo_models record, e.g.
// tasergun_lw; registry 82321BE0 vtable +8 = sub_82BFDA58, a map find) and
// spawns it as a dynamic object ([0x83085498] vtable +16); only then
// sub_82E3EC60 begins the action. A missing model or a refused spawn clears
// the hand prop silently. One line per prop and outcome (first 4 each), and a
// summary every 15 s with skate3_ped_behaviour_log.
namespace {
struct PropName {
  uint64_t key;
  const char* name;
};
constexpr PropName kHandProps[] = {
    {0xFCBA896AD5FEA8A2ull, "tazr"},          {0x24CB798A3CE2F521ull, "coffee"},
    {0xAFBC356E8C70C051ull, "cellphone"},     {0xF7F546F5C7E60F11ull, "newspaper"},
    {0xC140CB593A5B0A19ull, "briefcase"},     {0x4469441AC9A48918ull, "grocerybag"},
    {0xF2BD586E175404B1ull, "purse"},         {0xFEA9696F11B95BB2ull, "drinks"},
    {0xEE08BD1094E65F86ull, "pop"},           {0x4AC830F6003C82F4ull, "waterbottle"},
    {0xD0DD9844BAFB5F4Dull, "apple"},         {0x20BCE4F1E06CEC58ull, "orange"},
    {0xFDDD2FCF8A4160A9ull, "dufflebag"},     {0x03B9A27F6CB11DFFull, "reading"},
    {0xF1E0E5845055108Aull, "bags"},          {0xE607E98623CEDABBull, "map"},
    {0x1E000CCE7B09B435ull, "magazine"},      {0xC6A1588FEC761EDCull, "mail"},
    {0x70D5295EEDF54AB2ull, "crumpledpaper"}, {0x65231B87C7E27C84ull, "paperwrappedbottle"},
    {0xD2510D2F76A88E47ull, "panhandlingcan"}, {0xA9A4A1C434A3E055ull, "skateboard"},
    {0xD7EDBD362D7D2152ull, "default"}};
const char* HandPropName(uint64_t key) {
  for (const auto& p : kHandProps) {
    if (p.key == key) return p.name;
  }
  return "?";
}
struct PropStart {
  bool active = false;
  bool looked_up = false;
  uint64_t model = 0;
  uint32_t found = 0;
};
thread_local PropStart t_prop;
std::mutex g_prop_mutex;
std::unordered_map<uint64_t, int> g_prop_lines;  // (prop key ^ outcome) -> lines
std::atomic<uint32_t> g_prop_counts[2][3];        // [map][started, no model, no object]
}  // namespace

extern "C" REX_FUNC(sub_82BFDA58) {  // model registry find (key r4) -> model or 0
  const uint64_t key = ctx.r4.u64;
  __imp__sub_82BFDA58(ctx, base);
  if (t_prop.active && !t_prop.looked_up) {
    t_prop.looked_up = true;
    t_prop.model = key;
    t_prop.found = ctx.r3.u32;
  }
}

extern "C" REX_FUNC(sub_82E3DE18) {  // start the hand prop action (ped r3, &prop key r4)
  const uint32_t ped = ctx.r3.u32;
  uint64_t prop = 0;
  uint32_t hi = 0, lo = 0;
  if (Read32(base, ctx.r4.u32, hi) && Read32(base, ctx.r4.u32 + 4, lo)) prop = (uint64_t(hi) << 32) | lo;
  const PropStart saved = t_prop;
  t_prop = PropStart{};
  t_prop.active = true;
  __imp__sub_82E3DE18(ctx, base);
  const PropStart seen = t_prop;
  t_prop = saved;
  if (!Enabled()) return;
  uint32_t action = 0;
  Read32(base, ped + 5920, action);
  const int outcome = action ? 0 : (seen.looked_up && !seen.found) ? 1 : 2;
  const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
  g_prop_counts[map][outcome].fetch_add(1, std::memory_order_relaxed);
  {
    std::lock_guard<std::mutex> lock(g_prop_mutex);
    if (g_prop_lines.size() > 256 || g_prop_lines[prop ^ uint64_t(outcome)]++ >= 4) return;
  }
  static constexpr const char* kOutcome[] = {"started", "NOT STARTED: its model is not loaded",
                                             "NOT STARTED: the dynamic object was refused"};
  REXLOG_INFO("ped-ai[{}]: ped {:08X} hand prop '{}' ({:016X}): {} (model {:016X} -> {:08X}, action {:08X})",
              MapTag(), ped, HandPropName(prop), prop, kOutcome[outcome], seen.model, seen.found, action);
}

// Log 352: on the Skate 3 map EVERY hand prop (coffee, map, phone... and the
// taser: her hand stays empty while she zaps) was refused by the dynamic
// object spawn, while New San Van started all of them. The spawn is the
// living-world dynamic object manager ([0x83085498], vtable 8230C638 +16 =
// sub_826B8830(manager, descriptor)); it refuses when
//   1. bit 0x80 of [manager+168] is clear (manager off),
//   2. livingworld_entities 'dynamicobjects' is not found (sub_8254ED30),
//   3. the world's registry ([[0x830CFD94]+212] + 0xF4F0, vtable +8) lacks
//      the model (descriptor +104),
//   4. the world's live dynamic objects ([[world+22432]+16]) are 49 or more:
//      the movable street props (dumpsters, dispensers, cans; spawner
//      sub_826B9D58, up to 49, or 100 in its other mode) share the count,
//   5. the pool ([world+22440] vtable +20) has no free object.
// One line per check and map (first 6), plus the live count in the summary.
uint64_t Read64(uint8_t* base, uint32_t address);  // defined with the waypoint patch below
struct DmoSpawn {
  bool active = false;
  bool found = false;
  uint32_t collection = 0;
};
thread_local DmoSpawn t_dmo;
std::atomic<uint32_t> g_dmo_reasons[2][6];  // [map][started, off, no record, no model, full, pool]
std::atomic<uint32_t> g_dmo_count_max[2];
std::atomic<uint32_t> g_dmo_lines[2][6];

extern "C" REX_FUNC(sub_8254ED30) {  // livingworld_entities record r4 into handle r3
  const uint32_t out = ctx.r3.u32;
  __imp__sub_8254ED30(ctx, base);
  if (t_dmo.active && !t_dmo.found) {
    t_dmo.found = true;
    Read32(base, out, t_dmo.collection);
  }
}

extern "C" REX_FUNC(sub_826B8830) {  // dynamic object spawn (manager r3, descriptor r4) -> object or 0
  const uint32_t manager = ctx.r3.u32, descriptor = ctx.r4.u32;
  if (!Enabled()) {
    __imp__sub_826B8830(ctx, base);
    return;
  }
  uint8_t flags = 0;
  uint32_t game = 0, world = 0, list = 0, count = 0;
  Read8(base, manager + 168, flags);
  const bool have_count = Read32(base, 0x830CFD94u, game) && Read32(base, game + 212, world) &&
                          Read32(base, world + 22432, list) && Read32(base, list + 16, count);
  const uint64_t model = Read64(base, descriptor + 104);
  const DmoSpawn saved = t_dmo;
  t_dmo = DmoSpawn{};
  t_dmo.active = true;
  __imp__sub_826B8830(ctx, base);
  const DmoSpawn seen = t_dmo;
  t_dmo = saved;
  const uint32_t object = ctx.r3.u32;
  const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
  int reason = 0;
  if (!object) {
    reason = !(flags & 0x80) ? 1 : !seen.collection ? 2 : (have_count && count >= 49) ? 4 : 3;
  }
  g_dmo_reasons[map][reason].fetch_add(1, std::memory_order_relaxed);
  uint32_t high = g_dmo_count_max[map].load(std::memory_order_relaxed);
  while (have_count && count > high &&
         !g_dmo_count_max[map].compare_exchange_weak(high, count, std::memory_order_relaxed)) {
  }
  if (g_dmo_lines[map][reason].fetch_add(1, std::memory_order_relaxed) >= 6) return;
  static constexpr const char* kReason[] = {
      "spawned",
      "REFUSED: the dynamic object manager is off (bit 0x80 of +168 clear)",
      "REFUSED: no livingworld_entities 'dynamicobjects' record",
      "REFUSED: the world's model registry lacks the model, or the object pool is empty",
      "REFUSED: 49 or more live dynamic objects (the street props share that count)",
      ""};
  REXLOG_INFO("ped-ai[{}]: dynamic object spawn of model {:016X}: {} (live dynamic objects {}, manager flags {:02X})",
              MapTag(), model, kReason[reason], have_count ? std::to_string(count) : std::string("?"), flags);
}

// Census picks (log 352: 7 of the 34 ped models near the skater in New San
// Van were Skate 3's own: female_tourist_1, female_adult_2, male_skater_2,
// male_tourist_1, female_business_1, female_adult_3, male_business_2). The
// district census under each spawn point comes from the WorldPainter layer
// (sub_826B8A28 -> sub_82C0EAC0 layer 2 peds / 3 vehicles; key 'default' and
// zero peds where nothing is painted), and is loaded by sub_8269AF00
// (livingworld_census RefSpec, key r4). New San Van's census keys are all
// nsv_s2_* (worldpainter.txt); a Skate 3 key here shows where Skate 3 peds
// enter. One line per key and caller while New San Van is loaded.
const char* CensusName(uint64_t key) {
  switch (key) {
    case 0x5B6A0051DD6A295Cull: return "Skate 3 'pedestrians'";
    case 0xD7EDBD362D7D2152ull: return "Skate 3 'default'";
    case 0x9757A109BBCB975Full: return "nsv_s2_pedestrians";
    case 0xF767D5339B3F37A3ull: return "nsv_s2_default";
    default: return "";
  }
}
std::mutex g_census_mutex;
std::unordered_set<uint64_t> g_census_seen;

void LogCensusPick(const char* what, uint64_t key, uint32_t caller) {
  if (!Enabled() || !skate3::world_loading::IsSkate2Gameplay()) return;
  std::lock_guard<std::mutex> lock(g_census_mutex);
  if (g_census_seen.size() >= 96 || !g_census_seen.insert(key ^ (uint64_t(caller) << 1)).second) return;
  REXLOG_INFO("ped-ai[nsv]: {} {:016X} {} (from {:08X})", what, key, CensusName(key), caller);
}

extern "C" REX_FUNC(sub_8269AF00) {  // livingworld_census RefSpec r3 = key r4
  LogCensusPick("census", ctx.r4.u64, uint32_t(ctx.lr));
  __imp__sub_8269AF00(ctx, base);
}

// The same RefSpec shape for class F97B9086CAA50C53; a living-world
// component (sub_82E35028, vtable 8232D140) starts with key 'pedestrians'.
extern "C" REX_FUNC(sub_8269AE98) {
  LogCensusPick("class F97B9086CAA50C53 record", ctx.r4.u64, uint32_t(ctx.lr));
  __imp__sub_8269AE98(ctx, base);
}

// Log 353: the street props filled the count to 49 right after load (244
// spawned in the first 15 s) and every hand prop was refused at 49. The prop
// spawner sub_826B9D58 (and its trim sub_826BAD98) stop at 49 minus the
// reserve [world+0xBF8C]; only the reserve leaves room for hand props, whose
// spawn allows up to 49. Skate 3 sets it to 2 from a callback that one game
// state (vtable 82301A50) registers on event 6 (sub_825C5AF8 ->
// sub_825C5B20 -> sub_825C5B78) and to 0 in sub_825C5EA0; it was 0 all through
// log 353. Raise it to 2 before each spawner pass (skate3_ped_hand_prop_slots).
constexpr uint32_t kDynamicObjectReserve = 0xBF8C;
std::atomic<uint32_t> g_reserve_seen{~0u};
extern "C" REX_FUNC(sub_826B9D58) {  // street prop spawner (population manager r3, ...)
  uint32_t game = 0, world = 0, reserve = 0;
  if (REXCVAR_GET(skate3_ped_hand_prop_slots) && Read32(base, 0x830CFD94u, game) && Read32(base, game + 212, world) &&
      world >= 0x10000 && Read32(base, world + kDynamicObjectReserve, reserve)) {
    g_reserve_seen.store(reserve, std::memory_order_relaxed);
    if (reserve < 2) REX_STORE_U32(world + kDynamicObjectReserve, 2);
  }
  __imp__sub_826B9D58(ctx, base);
}

std::string HandPropSummary() {
  std::string text;
  if (const uint32_t reserve = g_reserve_seen.exchange(~0u); reserve != ~0u)
    text += "game's hand prop reserve " + std::to_string(reserve) + " (kept at 2 or more); ";
  for (int map = 0; map < 2; ++map) {
    text += map ? "; nsv dynamic object spawns " : "dynamic object spawns: s3 ";
    text += std::to_string(g_dmo_reasons[map][0].exchange(0)) + " spawned, refused off/record/model/full " +
            std::to_string(g_dmo_reasons[map][1].exchange(0)) + "/" +
            std::to_string(g_dmo_reasons[map][2].exchange(0)) + "/" +
            std::to_string(g_dmo_reasons[map][3].exchange(0)) + "/" +
            std::to_string(g_dmo_reasons[map][4].exchange(0)) + ", live objects max " +
            std::to_string(g_dmo_count_max[map].exchange(0));
  }
  text += "; hand props: ";
  for (int map = 0; map < 2; ++map) {
    text += map ? "; nsv " : "s3 ";
    text += std::to_string(g_prop_counts[map][0].exchange(0)) + " started, " +
            std::to_string(g_prop_counts[map][1].exchange(0)) + " model not loaded, " +
            std::to_string(g_prop_counts[map][2].exchange(0)) + " object refused";
  }
  return text;
}

// TazeWantTarget update (vtable 82309D6C slot 13): once its board timer 0x27
// runs out it applies the taze to the target and sets bit 0x20 of
// [board+0xCD0]. One line per hit.
extern "C" REX_FUNC(sub_826A8420) {
  const uint32_t context = ctx.r4.u32;
  uint32_t ped = 0, board = 0;
  uint8_t before = 0, after = 0;
  const bool log = Enabled() && Read32(base, context + 4, ped) && Read32(base, ped + 0x1708, board) &&
                   Read8(base, board + 0xCD0, before);
  __imp__sub_826A8420(ctx, base);
  if (!log || (before & 0x20) || !Read8(base, board + 0xCD0, after) || !(after & 0x20)) return;
  uint32_t action = 0;
  Read32(base, ped + 5920, action);
  REXLOG_INFO("ped-ai[{}]: taser ped {:08X} FIRES (taze applied to its target; hand prop action {:08X})", MapTag(),
              ped, action);
}

// Sets or clears a want's "needs addressing" flag (rlwimi bit 0x80 of the
// want entry): r3 = the ped's AI state object ([ped+0x1708]), r4 = want
// index, r5 = flag. Called by 82E41B98 (activate want, flag 1) and 826A6210.
// This is the decision point: which reaction a hit ped actually chose.
extern "C" REX_FUNC(sub_82E42A58) {
  const uint32_t board = ctx.r3.u32;
  const uint32_t want = ctx.r4.u32;
  const uint32_t flag = ctx.r5.u32 & 1u;
  __imp__sub_82E42A58(ctx, base);
  if (!Enabled()) return;
  Count(kHookWantSet);
  if (want > 63) return;
  if (Changed(board, Kind(kWantBase + want), flag, /*quiet_zero=*/true)) {
    const char* name = want < std::size(kWantNames) ? kWantNames[want] : "?";
    REXLOG_INFO("ped-ai[{}]: state {:08X} want {} ({}) {}", MapTag(), board, name, want,
                flag ? "SET" : "cleared");
  }
}

// Plugin transfer conditions of the sit / ATM / fountain plugins
// (PluginDescriptor/*.xml; registered by sub_8275C1xx, vtable slot 8),
// evaluated in order (an "and" expression) for peds near an interaction
// point: HasFirstWaypointAvailable, InFrontOfFirstWaypoint, CanSitWithHandProp,
// not IsOnRoad (its slot 8 is the return-0 stub 8274CA90: never true),
// not IsNearCrossWalk, not IsChasing. Counted per map and result in the 15 s
// summary (per-ped lines toggled hundreds of times: log 340); then the
// interaction's LockFirstWaypoint enter (sub_826A0580) shows a ped committing.
enum Transfer : uint32_t {
  kTAvailable, kTInFront, kTCanSit, kTChasing, kTNearCrosswalk, kTransferCount
};
constexpr const char* kTransferNames[kTransferCount] = {"available", "in-front", "can-sit",
                                                        "chasing", "near-crosswalk"};
std::atomic<uint32_t> g_transfer[kTransferCount][2][2];  // [condition][map][result]
std::atomic<uint32_t> g_locks[2];
std::atomic<uint32_t> g_lock_samples{0};

void CountTransfer(Transfer t, uint32_t result) {
  const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
  g_transfer[t][map][result ? 1 : 0].fetch_add(1, std::memory_order_relaxed);
}

std::string WaypointSummary() {
  std::string text;
  for (int map = 0; map < 2; ++map) {
    text += map ? "; nsv" : "s3";
    for (uint32_t t = 0; t < kTransferCount; ++t) {
      text += std::string(" ") + kTransferNames[t] + "=" +
              std::to_string(g_transfer[t][map][1].exchange(0, std::memory_order_relaxed)) + "/" +
              std::to_string(g_transfer[t][map][0].exchange(0, std::memory_order_relaxed));
    }
    text += " locks=" + std::to_string(g_locks[map].exchange(0, std::memory_order_relaxed));
  }
  return text;
}

extern "C" REX_FUNC(sub_8269C3D8) {  // HasFirstWaypointAvailable
  __imp__sub_8269C3D8(ctx, base);
  if (!Enabled()) return;
  Count(kHookWaypoint);
  CountTransfer(kTAvailable, ctx.r3.u32 & 0xFFu);
}

extern "C" REX_FUNC(sub_8269CF58) {  // InFrontOfFirstWaypoint
  __imp__sub_8269CF58(ctx, base);
  if (Enabled()) CountTransfer(kTInFront, ctx.r3.u32 & 0xFFu);
}

extern "C" REX_FUNC(sub_8269D268) {  // CanSitWithHandProp
  __imp__sub_8269D268(ctx, base);
  if (Enabled()) CountTransfer(kTCanSit, ctx.r3.u32 & 0xFFu);
}

extern "C" REX_FUNC(sub_8269D360) {  // IsChasing
  __imp__sub_8269D360(ctx, base);
  if (Enabled()) CountTransfer(kTChasing, ctx.r3.u32 & 0xFFu);
}

// IsNearCrossWalk (factory 826C4878 -> 8269CB60, vtable 823087D8 slot 8): the
// last unmeasured term of the sit / ATM transfer expressions. Log 343: NSV
// peds pass available, in-front and can-sit hundreds of times and are not
// chasing, yet never lock a bench; a "near a crosswalk" answer would block all
// of them (New San Van's crosswalk areas come from the navigation bake).
extern "C" REX_FUNC(sub_8269CC38) {
  __imp__sub_8269CC38(ctx, base);
  if (Enabled()) CountTransfer(kTNearCrosswalk, ctx.r3.u32 & 0xFFu);
}

// Sender component lookups inside the message handler sub_82E38FB8.
extern "C" REX_FUNC(sub_82965630) {
  const uint32_t lr = uint32_t(ctx.lr);
  __imp__sub_82965630(ctx, base);
  if ((lr != 0x82E39068u && lr != 0x82E390DCu) || !Enabled()) return;
  const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
  const int found = ctx.r3.u32 != 0;
  (lr == 0x82E39068u ? g_sender_actor : g_sender_chasee)[map][found].fetch_add(
      1, std::memory_order_relaxed);
}

// Knowledge-add path (callers 826A5A70, 82E3C468, 82E3C4F8, 82E3D240, 82E41B98).
extern "C" REX_FUNC(sub_82E42868) {
  if (Enabled()) {
    const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
    std::lock_guard<std::mutex> lock(g_add_path_mutex);
    if (g_add_path[map].size() < 32) ++g_add_path[map][uint32_t(ctx.lr)];
  }
  __imp__sub_82E42868(ctx, base);
}

// Knowledge add (board r3, entity handle r4: two words; -1 / negative = none).
extern "C" REX_FUNC(sub_82E42690) {
  if (Enabled()) {
    const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
    uint32_t id = 0xFFFFFFFFu, serial = 0;
    const bool read = Read32(base, ctx.r4.u32, id) && Read32(base, ctx.r4.u32 + 4, serial);
    const int valid = read && id != 0xFFFFFFFFu && int32_t(serial) >= 0;
    g_knowledge_adds[map][valid].fetch_add(1, std::memory_order_relaxed);
  }
  __imp__sub_82E42690(ctx, base);
}

// Mood event pushers (virtual; this r3, event r4) and the push wrapper's gates.
void CountPusher(PPCContext& ctx, uint8_t* base, int which) {
  if (!Enabled()) return;
  const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
  g_pusher_calls[map][which].fetch_add(1, std::memory_order_relaxed);
  uint32_t type = 0xFFFFFFFFu;
  if (which == 0) Read32(base, ctx.r4.u32 + 16, type);
  std::lock_guard<std::mutex> lock(g_add_path_mutex);
  if (g_pusher_callers[map].size() < 32) ++g_pusher_callers[map][uint32_t(ctx.lr)];
  if (which == 0 && g_pusher_types[map].size() < 32) ++g_pusher_types[map][type];
}

extern "C" REX_FUNC(sub_82E3D198) {
  CountPusher(ctx, base, 0);
  __imp__sub_82E3D198(ctx, base);
}

extern "C" REX_FUNC(sub_82E3DA40) {
  CountPusher(ctx, base, 1);
  __imp__sub_82E3DA40(ctx, base);
}

// Log 347: the wrapper's first gate is the mood manager ([0x830854C8],
// vtable +16 = 82BFED98 -> 82BFE960): a table at manager+4 keyed by the ped's
// livingworld_entities collection (u64 [[[board+3108]+2020]+224]) holding the
// event types it reacts to. sub_82BFE010(manager) fills it once at startup
// (from sub_826BB8E0) by walking every livingworld_entities record and its
// livingworld_entities_moodreactions (712E5451373663BE), so New San Van's
// nsv_s2_* entities, loaded later, are absent: every NSV event was refused
// there (perception never ran; Skate 3 peds: ~15% reach it). The builder
// re-assigns its fixed category slots and ORs each entity's reaction bits into
// the table (find, OR, insert), so running it again is harmless. Log 349: 271
// ran it in NSV and NSV still stored nothing: it does not walk every entity
// but sub_828D9FC0(livingworld_entities, h64("regular"), &list), the entities
// whose parent chain (sub_828DA3E0: parent collection +16, key +24) reaches
// 'regular'. New San Van's hierarchy is renamed (nsv_s2_regular <-
// nsv_s2_pedestrians <- nsv_s2_default), so none qualified. During this
// rebuild the list call asks for nsv_s2_regular instead (its 15 ped types
// carry moodreactions). Once per world load (RenderWorldIdentity), game
// thread, at the wrapper's entry, before the wrapper reads the table.
constexpr uint64_t kRegularEntity = 0x1BD348A61C97F559ull;     // h64("regular")
constexpr uint64_t kNsvRegularEntity = 0x06EDB3B685D10A95ull;  // h64("nsv_s2_regular")
std::atomic<uint64_t> g_moods_world{0};
thread_local bool t_nsv_mood_rebuild = false;
uint32_t g_nsv_mood_entities = 0;  // game thread only
std::vector<uint64_t> g_nsv_entity_keys;  // game thread only: the rebuild's NSV ped types

// Interaction points (skate3_skate2_ped_waypoints). Each ped type lists the
// waypoint entities it may use (F89323E420A6AAA3: 32-byte entries {class,
// key, 0, chance}; nsv_s2_regular also 66A6F2052AF5A836, the global
// conversation point). The living-world preparation renamed those entities
// with the rest (nsv_s2_waypoint_sit...), but New San Van's world cells keep
// Skate 2's keys (waypoint_sit, waypoint_atm...: 24 sit, 8 ATM, 24 bin, 11
// fountain, 6 vending references in the prepared cells, none to a renamed
// one), and Skate 3's own ped types list those same plain keys. So no NSV ped
// ever matched a bench or ATM (log 350/351: every transfer check passed,
// zero locks). Point the lists back at the plain keys (Skate 3's own
// waypoint_* entities) once per world load. Code only, idempotent.
struct WaypointRename {
  uint64_t from, to;
  const char* name;
};
constexpr WaypointRename kWaypointRenames[] = {
    {0x10D5B264E7F143D2ull, 0xE25FA5ECC1166AF1ull, "sit"},
    {0xDF176C9939196C2Bull, 0x31D6497AD6475CB9ull, "atm"},
    {0x1477F8E65F93AE4Bull, 0x519852A53B91D8D9ull, "vendingmachine"},
    {0xDB8C1089AC3EFEB1ull, 0x2CD52EBA0C63059Full, "conversation"},
    {0x5BCA094D9D542ECFull, 0xA5B3F3ACD828805Bull, "usetrashbin"},
    {0x1651F6F17EF2D25Eull, 0x810D90D62F23A51Dull, "newspaperbox"},
    {0x0475DC5909A5C958ull, 0x039FE06A97F0908Full, "waterfountain"},
    {0x37CEDFCBF897C9DCull, 0x9E1B7CCF03A3C06Dull, "conversation_global"}};
constexpr uint64_t kLivingWorldEntitiesClass = 0x60D703C77CAB1631ull;  // h64("livingworld_entities")
constexpr uint64_t kWaypointListAttr = 0xF89323E420A6AAA3ull;
constexpr uint64_t kGlobalConversationAttr = 0x66A6F2052AF5A836ull;

bool Write64(uint8_t* base, uint32_t address, uint64_t value) {
  if (address < 0x10000 || address > 0xfffffff7u) return false;
  const uint64_t be = __builtin_bswap64(value);
  std::memcpy(base + address, &be, 8);
  return true;
}

uint64_t Read64(uint8_t* base, uint32_t address) {
  uint32_t hi = 0, lo = 0;
  if (!Read32(base, address, hi) || !Read32(base, address + 4, lo)) return 0;
  return (uint64_t(hi) << 32) | lo;
}

// Rewrites one {class, key} reference at `at` (key at +8); true if renamed.
bool RenameWaypointRef(uint8_t* base, uint32_t at) {
  if (Read64(base, at) != kLivingWorldEntitiesClass) return false;
  const uint64_t key = Read64(base, at + 8);
  for (const auto& r : kWaypointRenames) {
    if (key == r.from) return Write64(base, at + 8, r.to);
  }
  return false;
}

void PatchNsvWaypointLists(PPCContext& ctx, uint8_t* base) {
  std::vector<uint64_t> keys = g_nsv_entity_keys;
  keys.push_back(kNsvRegularEntity);
  uint32_t types = 0, renamed = 0, odd = 0;
  for (const uint64_t key : keys) {
    auto call = ctx;
    call.r3.u64 = kLivingWorldEntitiesClass;
    call.r4.u64 = key;
    sub_82B69B08(call, base);  // FindCollection (adds a reference)
    const uint32_t collection = call.r3.u32;
    if (!collection) continue;
    bool touched = false;
    // sub_82B72420(collection, attribute, index) returns element `index`
    // (own or inherited data; for an array header {u16, u16 count, u16
    // element size, u16} + 8 + index * size) and 0 past the count. Log 352:
    // round 276 read element 0 as the header, so 13 of 14 lists were skipped.
    for (const uint64_t attr : {kWaypointListAttr, kGlobalConversationAttr}) {
      const uint32_t limit = attr == kWaypointListAttr ? 32 : 1;
      for (uint32_t index = 0; index < limit; ++index) {
        call = ctx;
        call.r3.u32 = collection;
        call.r4.u64 = attr;
        call.r5.u64 = index;
        sub_82B72420(call, base);
        const uint32_t element = call.r3.u32;
        if (!element) break;
        if (Read64(base, element) != kLivingWorldEntitiesClass) {
          ++odd;
          break;
        }
        if (RenameWaypointRef(base, element)) ++renamed, touched = true;
      }
    }
    types += touched ? 1 : 0;
    call = ctx;
    call.r3.u32 = collection;
    sub_82B69978(call, base);  // release FindCollection's reference
  }
  REXLOG_INFO("skate2-peds: interaction point lists of {} New San Van ped types now name the city's own points "
              "({} entries renamed{})", types, renamed, odd ? ", some entries were not entity references" : "");
}

void RegisterNsvPedMoods(PPCContext& ctx, uint8_t* base) {
  if (!REXCVAR_GET(skate3_skate2_ped_moods) || !skate3::world_loading::IsSkate2Gameplay()) return;
  const uint64_t world = skate3::world_loading::RenderWorldIdentity();
  if (g_moods_world.exchange(world, std::memory_order_relaxed) == world) return;
  uint32_t manager = 0;
  if (!Read32(base, 0x830854C8u, manager) || manager < 0x10000) return;
  auto call = ctx;
  call.r3.u32 = manager;
  g_nsv_mood_entities = 0;
  t_nsv_mood_rebuild = true;
  sub_82BFE010(call, base);
  t_nsv_mood_rebuild = false;
  REXLOG_INFO("skate2-peds: {} New San Van ped types (under nsv_s2_regular) added to the mood table (manager {:08X})",
              g_nsv_mood_entities, manager);
  if (REXCVAR_GET(skate3_skate2_ped_waypoints)) PatchNsvWaypointLists(ctx, base);
}

// Collections of class r3 whose parent chain reaches key r4, into vector r5
// (8-byte keys). Swapped to New San Van's root only inside the rebuild above.
extern "C" REX_FUNC(sub_828D9FC0) {
  if (!t_nsv_mood_rebuild || ctx.r4.u64 != kRegularEntity) {
    __imp__sub_828D9FC0(ctx, base);
    return;
  }
  const uint32_t list = ctx.r5.u32;
  ctx.r4.u64 = kNsvRegularEntity;
  __imp__sub_828D9FC0(ctx, base);
  uint32_t begin = 0, end = 0;
  if (Read32(base, list, begin) && Read32(base, list + 4, end) && end >= begin) {
    g_nsv_mood_entities = (end - begin) / 8;
    g_nsv_entity_keys.clear();
    for (uint32_t at = begin; at + 8 <= end && g_nsv_entity_keys.size() < 64; at += 8) {
      uint32_t hi = 0, lo = 0;
      if (Read32(base, at, hi) && Read32(base, at + 4, lo)) g_nsv_entity_keys.push_back((uint64_t(hi) << 32) | lo);
    }
  }
}

extern "C" REX_FUNC(sub_82E41060) {  // push wrapper (board r3, event r4)
  RegisterNsvPedMoods(ctx, base);
  if (Enabled()) {
    const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
    g_wrap_entries[map].fetch_add(1, std::memory_order_relaxed);
    uint8_t flags = 0;
    if (Read8(base, ctx.r3.u32 + 3278, flags) && (flags & 0x08)) {
      g_wrap_blocked_bit[map].fetch_add(1, std::memory_order_relaxed);
    }
    uint32_t owner = 0, info = 0, hi = 0, lo = 0;
    if (Read32(base, ctx.r3.u32 + 3108, owner) && Read32(base, owner + 2020, info) &&
        Read32(base, info + 224, hi) && Read32(base, info + 228, lo)) {
      std::lock_guard<std::mutex> lock(g_add_path_mutex);
      if (g_ped_keys[map].size() < 16) ++g_ped_keys[map][(uint64_t(hi) << 32) | lo];
    }
    if (!g_manager_logged.exchange(true)) {
      uint32_t manager = 0, vtable = 0, f16 = 0, f20 = 0, f40 = 0, f60 = 0;
      if (Read32(base, 0x830854C8u, manager) && Read32(base, manager, vtable)) {
        Read32(base, vtable + 16, f16);
        Read32(base, vtable + 20, f20);
        Read32(base, vtable + 40, f40);
        Read32(base, vtable + 60, f60);
      }
      REXLOG_INFO("ped-ai: mood manager {:08X} vtable {:08X}: +16 {:08X} +20 {:08X} +40 {:08X} +60 {:08X}",
                  manager, vtable, f16, f20, f40, f60);
    }
  }
  __imp__sub_82E41060(ctx, base);
}

extern "C" REX_FUNC(sub_82E41400) {  // wrapper's filter: reached = global check and bit passed
  if (Enabled() && ctx.lr == 0x82E410B8) {
    g_wrap_filtered[skate3::world_loading::IsSkate2Gameplay() ? 1 : 0].fetch_add(
        1, std::memory_order_relaxed);
  }
  __imp__sub_82E41400(ctx, base);
}

extern "C" REX_FUNC(sub_82E414D8) {  // event stored on the ped's board
  if (Enabled()) {
    g_wrap_stored[skate3::world_loading::IsSkate2Gameplay() ? 1 : 0].fetch_add(
        1, std::memory_order_relaxed);
  }
  __imp__sub_82E414D8(ctx, base);
}

// Mood update's want/knowledge pass (from sub_82E37F68).
extern "C" REX_FUNC(sub_82E41B98) {
  if (Enabled()) {
    g_knowledge_scans[skate3::world_loading::IsSkate2Gameplay() ? 1 : 0].fetch_add(
        1, std::memory_order_relaxed);
  }
  __imp__sub_82E41B98(ctx, base);
}

// A ped notices an interaction point (sub_82E3B060, ped r3, point r4): the
// point's waypoint type key (component vt40) is compared with each entry of
// the ped type's list (F89323E420A6AAA3, from ped +5660), then the entry's
// chance is rolled (sub_8269A588). Logs 354-355: New San Van peds never claimed
// a point and almost never ran the transfer checks; Skate 3 peds claimed 3-6
// per run. Counted per map (calls, list matches, uses) in the 15 s summary,
// plus the first point type keys each map sees.
std::atomic<uint32_t> g_notice[2][3];  // [map][noticed, type in list, wants to use]
std::mutex g_notice_mutex;
std::unordered_set<uint64_t> g_notice_types;
thread_local bool t_notice_match = false;
extern "C" REX_FUNC(sub_8269A588) {  // chance roll of a matched list entry
  __imp__sub_8269A588(ctx, base);
  t_notice_match = true;
}
extern "C" REX_FUNC(sub_82E3B060) {
  const uint32_t ped = ctx.r3.u32;
  const bool saved = t_notice_match;
  t_notice_match = false;
  __imp__sub_82E3B060(ctx, base);
  const bool matched = t_notice_match;
  t_notice_match = saved;
  if (!Enabled()) return;
  const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
  g_notice[map][0].fetch_add(1, std::memory_order_relaxed);
  if (matched) g_notice[map][1].fetch_add(1, std::memory_order_relaxed);
  if (matched && (ctx.r3.u32 & 0xFF)) g_notice[map][2].fetch_add(1, std::memory_order_relaxed);
  uint32_t entity = 0;
  Read32(base, ped + 5660, entity);
  const uint64_t ped_type = entity ? Read64(base, entity + 24) : 0;
  std::lock_guard<std::mutex> lock(g_notice_mutex);
  if (g_notice_types.size() < 40 && g_notice_types.insert(ped_type ^ (uint64_t(map) << 63) ^ (matched ? 1 : 0)).second)
    REXLOG_INFO("ped-ai[{}]: ped {:08X} (type {:016X}) noticed an interaction point: {}", MapTag(), ped, ped_type,
                matched ? "its type is in the ped type's list" : "type NOT in the ped type's list");
}
std::string NoticeSummary() {
  std::string text;
  for (int map = 0; map < 2; ++map) {
    text += std::string(map ? "; nsv " : "s3 ") + std::to_string(g_notice[map][0].exchange(0)) + " noticed, " +
            std::to_string(g_notice[map][1].exchange(0)) + " type in list, " +
            std::to_string(g_notice[map][2].exchange(0)) + " roll true";
  }
  return text;
}

// Behaviour LockFirstWaypoint, enter (vtable 8230A528 slot 12): a ped
// claims the interaction point it is about to use.
extern "C" REX_FUNC(sub_826A0580) {
  const uint32_t context = ctx.r4.u32;
  __imp__sub_826A0580(ctx, base);
  if (!Enabled()) return;
  const int map = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
  g_locks[map].fetch_add(1, std::memory_order_relaxed);
  if (g_lock_samples.fetch_add(1, std::memory_order_relaxed) < 40) {
    uint32_t ped = 0;
    Read32(base, context + 4, ped);
    REXLOG_INFO("ped-ai[{}]: ped {:08X} claims an interaction point (LockFirstWaypoint)", MapTag(),
                ped);
  }
}

// EB001A interaction payload relocated on load: +0 point count (0x60 each),
// +4 link count (0x30 each).
extern "C" REX_FUNC(sub_82964790) {
  const uint32_t payload = ctx.r3.u32;
  uint32_t points = 0, links = 0;
  const bool log = Enabled() && Read32(base, payload, points) && Read32(base, payload + 4, links);
  __imp__sub_82964790(ctx, base);
  if (Enabled()) Count(kHookPayload);
  if (log) {
    REXLOG_INFO("ped-ai[{}]: interaction payload {:08X} loaded: {} points, {} links", MapTag(),
                payload, points, links);
  }
}
