// Locations-menu probe (diagnostic, bounded logs): maps how the pause-menu
// Locations list is built and how a selection becomes a teleport, so New San
// Van can be appended to it. Evidence so far (reports/log098_nsv_menu_probe.md):
//   sub_82B69B08 = Attrib FindCollection(classKey r3, collectionKey r4) ->
//                  collection* (r3)
//   sub_82562AA0 = the fe_locations instance helper (out r3, collection key
//                  r4): class key 0xDDCD6BC23255E508 = h64("fe_locations");
//                  callers include TeleportLocalPlayerToToFELocation (Lua),
//                  IsLocationUnlocked (Lua) and several FE functions
//   sub_82706F50 / sub_82720B50 = the retail menu-teleport pair (log012)
// Every log line carries a guest backtrace (return addresses walked through
// the PPC back chain; the saved LR sits at back_chain - 8), deduplicated per
// call chain so one menu session stays small.

#include "generated/skate3_init.h"
#include "native/skate3_native_guest_read.h"
#include "skate3_menu_probe.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_set>

#include <rex/cvar.h>
#include <rex/logging.h>

REXCVAR_DEFINE_BOOL(skate3_menu_probe, false, "Skate 3",
                    "Diagnostic: log how the pause-menu Locations list is built and how a "
                    "selection teleports (fe_locations / world / challenge Attrib lookups, "
                    "FE candidate functions, the teleport pair), each with a deduplicated "
                    "guest backtrace. Bounded; for the New San Van menu work (done, run 099).")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);

namespace {

constexpr uint64_t kClassFeLocations = 0xDDCD6BC23255E508ull;
constexpr uint64_t kClassWorld = 0xBC69C54E6E102D88ull;
constexpr uint64_t kClassChallenges = 0x8876E0B556740E36ull;
constexpr uint64_t kClassFreeskateLoc = 0xA4F35B159D7432B5ull;  // freeskate_* records

bool U32(uint8_t* base, uint32_t address, uint32_t& value) {
  if (address < 0x10000 || address > 0xfffffffb ||
      !skate3::native_scene::GuestTryCopy(&value, base + address, sizeof(value))) {
    return false;
  }
  value = __builtin_bswap32(value);
  return true;
}

bool U64(uint8_t* base, uint32_t address, uint64_t& value) {
  uint32_t hi = 0, lo = 0;
  if (!U32(base, address, hi) || !U32(base, address + 4, lo)) return false;
  value = (uint64_t(hi) << 32) | lo;
  return true;
}

// Guest C string at address (printable ASCII only), or empty.
std::string CStr(uint8_t* base, uint32_t address, size_t max_len = 64) {
  if (address < 0x40000000 || address >= 0xA0000000) return {};
  char buf[96] = {};
  const size_t n = std::min<size_t>(max_len, sizeof(buf) - 1);
  if (!skate3::native_scene::GuestTryCopy(buf, base + address, n)) return {};
  size_t len = 0;
  while (len < n && buf[len] != 0) {
    const unsigned char c = static_cast<unsigned char>(buf[len]);
    if (c < 32 || c > 126) return {};
    ++len;
  }
  if (len < 3 || len == n) return {};
  return std::string(buf, len);
}

// Return-address chain: the call site (lr), then each caller's saved LR.
std::string Backtrace(uint8_t* base, uint32_t lr, uint32_t sp, uint64_t* hash) {
  std::string s;
  uint64_t h = 1469598103934665603ull;
  char buf[16];
  auto add = [&](uint32_t a) {
    std::snprintf(buf, sizeof(buf), "%08X ", a);
    s += buf;
    h = (h ^ a) * 1099511628211ull;
  };
  add(lr);
  uint32_t frame = sp;
  for (int i = 0; i < 14; ++i) {
    uint32_t back = 0, ra = 0;
    if (!U32(base, frame, back) || back <= frame || back - frame > 0x20000) break;
    if (!U32(base, back - 8, ra) || ra < 0x82000000 || ra >= 0x83200000) break;
    add(ra);
    frame = back;
  }
  if (hash) *hash = h;
  return s;
}

// Deduplicating admission: first sighting of a hash only, capped per site.
struct Gate {
  std::mutex m;
  std::unordered_set<uint64_t> seen;
  std::atomic<uint32_t> logged{0};
  bool Admit(uint64_t h, uint32_t cap) {
    if (logged.load(std::memory_order_relaxed) >= cap) return false;
    std::lock_guard lock(m);
    if (!seen.insert(h).second) return false;
    logged.fetch_add(1, std::memory_order_relaxed);
    return true;
  }
};

const char* ClassName(uint64_t key) {
  switch (key) {
    case kClassFeLocations: return "fe_locations";
    case kClassWorld: return "world";
    case kClassChallenges: return "challenges";
    case kClassFreeskateLoc: return "freeskate_loc";
    default: return nullptr;
  }
}

// Collection dump: header words + the layout block (collection+36), with any
// word that points at a printable C string resolved.
std::string DumpCollection(uint8_t* base, uint32_t coll) {
  std::string s;
  char buf[96];
  if (coll == 0) return " coll=null";
  std::snprintf(buf, sizeof(buf), " coll=%08X hdr=[", coll);
  s += buf;
  for (int i = 0; i < 12; ++i) {
    uint32_t w = 0;
    U32(base, coll + i * 4, w);
    std::snprintf(buf, sizeof(buf), "%08X%s", w, i == 11 ? "]" : " ");
    s += buf;
  }
  uint32_t layout = 0;
  if (U32(base, coll + 36, layout) && layout != 0) {
    std::snprintf(buf, sizeof(buf), " layout=%08X [", layout);
    s += buf;
    for (int i = 0; i < 24; ++i) {
      uint32_t w = 0;
      U32(base, layout + i * 4, w);
      std::snprintf(buf, sizeof(buf), "%08X%s", w, i == 23 ? "]" : " ");
      s += buf;
      const std::string str = CStr(base, w);
      if (!str.empty()) {
        s += "{\"" + str + "\"} ";
      }
    }
  }
  return s;
}

Gate g_find_keys;     // distinct (class, collection) lookups
Gate g_find_chains;   // distinct call chains per class
Gate g_fe_helper;     // sub_82562AA0 chains
Gate g_candidates;    // FE candidate functions (per fn + chain)
Gate g_teleport;      // 82706F50 / 82720B50
Gate g_events;        // gameplay events other than the per-frame ones

}  // namespace

namespace skate3::menu_probe {

void OnGameplayEvent(uint8_t* base, uint32_t lr, uint32_t sp, uint32_t object, uint32_t payload,
                     uint32_t type) {
  if (!REXCVAR_GET(skate3_menu_probe) || type == 1 || type == 0) return;
  uint64_t h = 0;
  const std::string bt = Backtrace(base, lr, sp, &h);
  if (!g_events.Admit(h ^ (uint64_t(type) << 56), 64)) return;
  std::string words;
  char buf[16];
  for (int i = 0; i < 24; ++i) {
    uint32_t w = 0;
    U32(base, payload + i * 4, w);
    std::snprintf(buf, sizeof(buf), "%08X ", w);
    words += buf;
  }
  REXLOG_INFO("nsv-menu-probe: event type={} object={:08X} payload={:08X} words=[{}] bt=[{}]",
              type, object, payload, words, bt);
}

void OnLocationMenuAction(uint8_t* base, uint32_t lr, uint32_t sp, uint32_t object) {
  if (!REXCVAR_GET(skate3_menu_probe)) return;
  uint64_t h = 0;
  const std::string bt = Backtrace(base, lr, sp, &h);
  if (!g_candidates.Admit(h ^ 0x8251C3C8ull, 256)) return;
  REXLOG_INFO("nsv-menu-probe: fn=8251C3C8 object={:08X} bt=[{}]", object, bt);
}

}  // namespace skate3::menu_probe

// Attrib FindCollection(classKey, collectionKey) -> collection*.
extern "C" REX_FUNC(sub_82B69B08) {
  const uint64_t cls = ctx.r3.u64;
  const uint64_t key = ctx.r4.u64;
  const uint32_t lr = uint32_t(ctx.lr);
  const uint32_t sp = ctx.r1.u32;
  const char* cname = REXCVAR_GET(skate3_menu_probe) ? ClassName(cls) : nullptr;
  __imp__sub_82B69B08(ctx, base);
  if (cname == nullptr) return;
  const uint32_t coll = ctx.r3.u32;
  uint64_t h = 0;
  const std::string bt = Backtrace(base, lr, sp, &h);
  const bool new_key = g_find_keys.Admit(cls * 31 + key, 600);
  const bool new_chain = g_find_chains.Admit(h ^ cls, 200);
  if (!new_key && !new_chain) return;
  REXLOG_INFO("nsv-menu-probe: find class={} key={:016X} new_key={} new_chain={}{} bt=[{}]", cname,
              key, new_key, new_chain, new_key ? DumpCollection(base, coll) : std::string(), bt);
}

// fe_locations instance helper (out r3, collection key r4).
extern "C" REX_FUNC(sub_82562AA0) {
  const uint64_t key = ctx.r4.u64;
  const uint32_t lr = uint32_t(ctx.lr);
  const uint32_t sp = ctx.r1.u32;
  __imp__sub_82562AA0(ctx, base);
  if (!REXCVAR_GET(skate3_menu_probe)) return;
  uint64_t h = 0;
  const std::string bt = Backtrace(base, lr, sp, &h);
  if (!g_fe_helper.Admit(h, 96)) return;
  REXLOG_INFO("nsv-menu-probe: fe_location_instance key={:016X} bt=[{}]", key, bt);
}

// FE / challenge candidates that call the fe_locations helper.
#define NSV_PROBE_CANDIDATE(ADDR)                                                          \
  extern "C" REX_FUNC(sub_##ADDR) {                                                        \
    if (REXCVAR_GET(skate3_menu_probe)) {                                                  \
      uint64_t h = 0;                                                                      \
      const std::string bt = Backtrace(base, uint32_t(ctx.lr), ctx.r1.u32, &h);            \
      if (g_candidates.Admit(h ^ 0x##ADDR##ull, 256)) {                                    \
        REXLOG_INFO("nsv-menu-probe: fn=" #ADDR " r3={:08X} r4={:016X} r5={:016X} "       \
                    "r6={:08X} bt=[{}]",                                                   \
                    ctx.r3.u32, ctx.r4.u64, ctx.r5.u64, ctx.r6.u32, bt);                   \
      }                                                                                    \
    }                                                                                      \
    __imp__sub_##ADDR(ctx, base);                                                          \
  }

NSV_PROBE_CANDIDATE(82565B28)
NSV_PROBE_CANDIDATE(825E0CE0)
NSV_PROBE_CANDIDATE(825E0DD0)
NSV_PROBE_CANDIDATE(8260A288)
NSV_PROBE_CANDIDATE(826457E0)
NSV_PROBE_CANDIDATE(8264A2A0)

// Retail menu-teleport init (child r3, spawn matrix r4, ...).
extern "C" REX_FUNC(sub_82706F50) {
  if (REXCVAR_GET(skate3_menu_probe)) {
    uint64_t h = 0;
    const std::string bt = Backtrace(base, uint32_t(ctx.lr), ctx.r1.u32, &h);
    if (g_teleport.Admit(h ^ 0x82706F50ull, 32)) {
      float m[16] = {};
      for (int i = 0; i < 16; ++i) {
        uint32_t w = 0;
        U32(base, ctx.r4.u32 + i * 4, w);
        std::memcpy(&m[i], &w, 4);
      }
      REXLOG_INFO("nsv-menu-probe: teleport_init child={:08X} pos=({:.3f},{:.3f},{:.3f}) "
                  "fwd=({:.3f},{:.3f},{:.3f}) r5={:016X} r6={:08X} r7={:08X} r8={:08X} bt=[{}]",
                  ctx.r3.u32, m[12], m[13], m[14], m[8], m[9], m[10], ctx.r5.u64, ctx.r6.u32,
                  ctx.r7.u32, ctx.r8.u32, bt);
    }
  }
  __imp__sub_82706F50(ctx, base);
}

extern "C" REX_FUNC(sub_82720B50) {
  if (REXCVAR_GET(skate3_menu_probe)) {
    uint64_t h = 0;
    const std::string bt = Backtrace(base, uint32_t(ctx.lr), ctx.r1.u32, &h);
    if (g_teleport.Admit(h ^ 0x82720B50ull, 32)) {
      REXLOG_INFO("nsv-menu-probe: teleport_begin self={:08X} cont={:08X} child={:08X} bt=[{}]",
                  ctx.r3.u32, ctx.r4.u32, ctx.r5.u32, bt);
    }
  }
  __imp__sub_82720B50(ctx, base);
}
