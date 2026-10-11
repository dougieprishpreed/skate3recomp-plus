// Gameplay cheats (settings menu, World > Cheats). Each switch changes the
// game's own state or tuning in place while it is on, in every map (Skate 3's
// and New San Vanelona alike), and takes effect immediately.
//
// Remove Air Timer: the skater state update (sub_82D8ADE8) counts the frames
// spent airborne (physics state 200) at +0x2C and, past 300 (five seconds),
// raises +0x39, the game's out-of-the-air reset. The community "No Air Timer"
// XEX removes the counter's store (0x82D8AFC4, stw r10,0x2C(r31)); clearing the
// counter before each update has the same effect.
//
// No Fall Damage and No Hall of Meat Bail Timer: values in the
// physics_wipeout/default tuning record (skatercollections), reached through
// the tuning manager [0x830CFDA4] -> +0x108 instance -> +4 record:
//   +0xFC  11.2  landing impact limit on the board (sub_82D90D08; the
//                community "No Fall Damage" XEX replaces this load,
//                0x82D90DD8), +0xF8 5.0 the same check's other state;
//   +0x140 11.5  fall speed limit off the board, as in a hippy jump
//                (sub_82D4D838);
//   +0x124 20.0  Hall of Meat bail time limit (sub_82D3E980). A bail still
//                ends once the body has come to rest.
// A switch writes a limit nothing reaches into the loaded record, and the
// record's own value goes back when it is switched off. Writes happen only
// while the field still holds the record's value or the cheat's, so a moved
// or reused record is never touched.
//
// Go Out of Bounds: the community out-of-bounds XEX removes three stores of a
// surface's flags word, the word that marks ground a skater is sent back from
// (grass, hills):
//   0x8276D7A8  ClusteredMeshLineTest job (sub_8276D608): result +0x64;
//   0x8276DFD0  the second line test job (sub_8276DE40): result +0x84;
//   0x82DB6600  the skater's three contacts copying it (sub_82DB6580,
//               +0x5F0 + 0x30 each).
// The hooks clear those words once each function has run. Zero is the jobs'
// own "nothing found" value and the only value the patched game ever leaves
// in a contact. Every worker running a job clears the whole batch after its
// own share, so the last one to finish clears after the last write.
//
// No Black Screen: sub_827FF648 sets the scene-visible byte (+0x347A5) and
// clears it when no world is resident within 30 m of the view (the query
// sub_82478D00, returning to 0x827FF73C): the screen goes black far outside
// the map. The community XEX removes that clear (0x827FF74C); here the byte
// is set back when that query said not resident, and every other reason for
// a black screen stays.
//
// Realistic Ragdoll: the skeleton drive setup (sub_82BE7860) copies
// physics_skeleton_drives' 23 joint blocks (9 floats) to skeleton +0x310 and
// scales words 0-3 by attribute 851DFC1D6128976F (1.2) and words 5-6 by
// 057EDDFA341F9492 (1.0). The motor updates read them every frame (words 0-3
// in sub_82BEB0C8, words 5-6 in sub_82BEB488; controller +8 = skeleton), so
// the slider scales the six words from the copy taken at setup, live.
//
// Remove Camera Shake: the camera's ShakeEffect (two of them, camera system
// +0x260 / +0x264, playing camera/1.shk and camera/2.shk) is evaluated by
// sub_82E05800: amplitude = (impulse +0x28 + an amount that rises with speed)
// x blend +0x2C, the blend moving 5 per second toward on or off and clamped to
// 0-1. Writing impulse 0 and blend -1 before each evaluation clamps the blend
// to 0: no shake or sway at any speed. skate3_camera_shake_log records what
// the effect does.

#include "skate3_cheats.h"

#include "generated/skate3_init.h"
#include "native/skate3_native_guest_read.h"

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <unordered_map>

#include <rex/cvar.h>
#include <rex/logging.h>

REXCVAR_DEFINE_BOOL(skate3_cheat_no_air_timer, false, "Skate 3",
                    "Cheat: no five-second air timer; stay airborne as long as you like");
REXCVAR_DEFINE_BOOL(skate3_cheat_no_fall_damage, false, "Skate 3",
                    "Cheat: land drops from any height without bailing (on the board and "
                    "after hippy jumps); slamming into walls or objects still wipes you out");
REXCVAR_DEFINE_BOOL(skate3_cheat_no_hom_bail_timer, false, "Skate 3",
                    "Cheat: Hall of Meat bails last until the skater comes to rest instead of "
                    "ending after 20 seconds");
REXCVAR_DEFINE_BOOL(skate3_cheat_out_of_bounds, false, "Skate 3",
                    "Cheat: walk, skate and footplant on ground that normally sends you back "
                    "(grass, hills)");
REXCVAR_DEFINE_BOOL(skate3_cheat_no_black_screen, false, "Skate 3",
                    "Cheat: no black screen when you or the camera go far outside the map");
REXCVAR_DEFINE_BOOL(skate3_cheat_no_camera_shake, false, "Skate 3",
                    "Cheat: no camera shake or sway, including the sway that builds up at "
                    "high speed");
REXCVAR_DEFINE_DOUBLE(skate3_cheat_ragdoll_drive_scale, 1.0, "Skate 3",
                      "Realistic Ragdoll: how firmly skaters hold their bail pose; 1 = the "
                      "game's own bails, 0 = a fully loose ragdoll (0-1 in the menu)")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DEFINE_BOOL(skate3_camera_shake_log, false, "Skate 3",
                    "Diagnostic: log the camera ShakeEffect's speed, impulse, blend and "
                    "amplitude settings about four times a second per effect");

namespace {

constexpr uint32_t kTuningManager = 0x830CFDA4;
constexpr uint32_t kWipeoutInstance = 0x108;
// Attrib's shared all-zero block: an instance whose record is missing points
// here, and it must never be written.
constexpr uint32_t kAttribZeroBlock = 0x830D0850;
// 1,000,000: no landing speed, fall speed or bail time reaches it.
constexpr uint32_t kNeverReachedBits = 0x49742400;

struct WipeoutField {
  uint32_t offset;
  uint32_t record_bits;  // the physics_wipeout/default value
  const char* what;
};
constexpr std::array<WipeoutField, 3> kFallFields = {{
    {0xFC, 0x41333333, "landing impact limit (11.2)"},
    {0xF8, 0x40A00000, "landing impact limit, other state (5)"},
    {0x140, 0x41380000, "off-board fall speed limit (11.5)"},
}};
constexpr WipeoutField kBailTimeField = {0x124, 0x41A00000, "Hall of Meat bail time (20 s)"};

bool LoadU32(uint8_t* base, uint32_t addr, uint32_t& out) {
  uint32_t raw = 0;
  if (addr < 0x10000 || addr > 0xFFFFFFFC ||
      !skate3::native_scene::GuestTryCopy(&raw, base + addr, sizeof(raw))) {
    return false;
  }
  out = __builtin_bswap32(raw);
  return true;
}

void StoreU32(uint8_t* base, uint32_t addr, uint32_t value) {
  const uint32_t raw = __builtin_bswap32(value);
  std::memcpy(base + addr, &raw, sizeof(raw));
}

float BitsToFloat(uint32_t bits) {
  float value;
  std::memcpy(&value, &bits, sizeof(value));
  return value;
}

uint32_t FloatToBits(float value) {
  uint32_t bits;
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

bool LoadF32(uint8_t* base, uint32_t addr, float& out) {
  uint32_t bits = 0;
  if (!LoadU32(base, addr, bits)) {
    return false;
  }
  out = BitsToFloat(bits);
  return true;
}

// One log line per switch change (the hooks see the switches).
void NoteSwitch(std::atomic<int>& last, bool on, const char* name) {
  const int now = on ? 1 : 0;
  if (last.exchange(now, std::memory_order_relaxed) != now) {
    REXLOG_INFO("cheats: {} {}", name, on ? "on" : "off");
  }
}

// ---- physics_wipeout tuning ------------------------------------------------

// The loaded physics_wipeout/default record, or 0.
uint32_t WipeoutRecord(uint8_t* base) {
  uint32_t manager = 0, instance = 0, record = 0;
  if (!LoadU32(base, kTuningManager, manager) ||
      !LoadU32(base, manager + kWipeoutInstance, instance) ||
      !LoadU32(base, instance + 4, record) || record < 0x10000 ||
      record == kAttribZeroBlock) {
    return 0;
  }
  return record;
}

void ApplyField(uint8_t* base, uint32_t record, const WipeoutField& field, bool on) {
  uint32_t bits = 0;
  if (!LoadU32(base, record + field.offset, bits)) {
    return;
  }
  const uint32_t want = on ? kNeverReachedBits : field.record_bits;
  if (bits == want) {
    return;
  }
  if (bits != field.record_bits && bits != kNeverReachedBits) {
    // Not the record this cheat knows: leave it alone (reported once).
    static bool reported = false;
    if (!reported) {
      reported = true;
      REXLOG_WARN("cheats: {} at {:08X} holds {:08X}, not the expected tuning value; "
                  "left unchanged",
                  field.what, record + field.offset, bits);
    }
    return;
  }
  StoreU32(base, record + field.offset, want);
}

// Brings the wipeout record in line with the switches. Called at the record's
// readers, so a switch takes effect at the next landing, fall or bail.
void ApplyWipeoutCheats(uint8_t* base) {
  const bool no_fall = REXCVAR_GET(skate3_cheat_no_fall_damage);
  const bool no_bail_time = REXCVAR_GET(skate3_cheat_no_hom_bail_timer);
  static bool fall_applied = false, bail_time_applied = false;
  if (!no_fall && !no_bail_time && !fall_applied && !bail_time_applied) {
    return;  // nothing on and nothing to restore
  }
  const uint32_t record = WipeoutRecord(base);
  if (record == 0) {
    return;
  }
  if (no_fall || fall_applied) {
    for (const WipeoutField& field : kFallFields) {
      ApplyField(base, record, field, no_fall);
    }
    if (fall_applied != no_fall) {
      REXLOG_INFO("cheats: No Fall Damage {}", no_fall ? "on" : "off");
    }
    fall_applied = no_fall;
  }
  if (no_bail_time || bail_time_applied) {
    ApplyField(base, record, kBailTimeField, no_bail_time);
    if (bail_time_applied != no_bail_time) {
      REXLOG_INFO("cheats: No Hall of Meat Bail Timer {}", no_bail_time ? "on" : "off");
    }
    bail_time_applied = no_bail_time;
  }
}

// ---- Go Out of Bounds --------------------------------------------------------

// Clears one surface-flags word in each result record of a line test batch.
void ClearLineTestFlags(uint8_t* base, uint32_t job, uint32_t count, uint32_t item_size,
                        uint32_t result_ptr_offset, uint32_t flags_offset) {
  uint32_t items = 0;
  if (count == 0 || count > 8192 || !LoadU32(base, job + 0xC, items)) {
    return;
  }
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t result = 0;
    if (LoadU32(base, items + i * item_size + result_ptr_offset, result) && result >= 0x10000) {
      uint32_t flags = 0;
      if (LoadU32(base, result + flags_offset, flags) && flags != 0) {
        StoreU32(base, result + flags_offset, 0);
      }
    }
  }
}

std::atomic<int> g_out_of_bounds_noted{0};

// ---- Realistic Ragdoll -------------------------------------------------------

constexpr uint32_t kJoints = 23;
constexpr std::array<uint32_t, 6> kDriveWords = {0, 1, 2, 3, 5, 6};

struct TrackedSkeleton {
  std::array<uint32_t, kJoints * kDriveWords.size()> setup{};  // as the game set them up
  float applied = 1.0f;
};

std::mutex g_skeleton_mutex;
std::unordered_map<uint32_t, TrackedSkeleton> g_skeletons;
std::atomic<int> g_scaled_skeletons{0};  // tracked skeletons with applied != 1
std::atomic<uint32_t> g_ragdoll_noted{FloatToBits(1.0f)};

float RagdollScale() {
  const double scale = REXCVAR_GET(skate3_cheat_ragdoll_drive_scale);
  return scale >= 0.0 && scale <= 4.0 ? float(scale) : 1.0f;
}

uint32_t DriveWordAddress(uint32_t skeleton, uint32_t index) {
  const uint32_t joint = index / uint32_t(kDriveWords.size());
  const uint32_t word = kDriveWords[index % kDriveWords.size()];
  return skeleton + 0x310 + joint * 0x24 + word * 4;
}

// Writes setup x scale. With `expect` set, first checks every word still holds
// setup x expect (else the object was freed or rewritten; returns false).
bool WriteDrives(uint8_t* base, uint32_t skeleton, const TrackedSkeleton& tracked, float scale,
                 const float* expect) {
  if (expect != nullptr) {
    for (uint32_t i = 0; i < tracked.setup.size(); ++i) {
      uint32_t bits = 0;
      if (!LoadU32(base, DriveWordAddress(skeleton, i), bits) ||
          bits != FloatToBits(BitsToFloat(tracked.setup[i]) * *expect)) {
        return false;
      }
    }
  }
  for (uint32_t i = 0; i < tracked.setup.size(); ++i) {
    StoreU32(base, DriveWordAddress(skeleton, i),
             FloatToBits(BitsToFloat(tracked.setup[i]) * scale));
  }
  return true;
}

void NoteRagdollScale(float scale) {
  const uint32_t bits = FloatToBits(scale);
  if (g_ragdoll_noted.exchange(bits, std::memory_order_relaxed) != bits) {
    REXLOG_INFO("cheats: Realistic Ragdoll {:.2f}", scale);
  }
}

// Called at the motor updates: brings a tracked skeleton to the slider.
void ApplyRagdollScale(uint8_t* base, uint32_t controller) {
  const float scale = RagdollScale();
  if (scale == 1.0f && g_scaled_skeletons.load(std::memory_order_relaxed) == 0) {
    return;
  }
  uint32_t skeleton = 0;
  if (!LoadU32(base, controller + 8, skeleton) || skeleton < 0x10000) {
    return;
  }
  std::lock_guard<std::mutex> lock(g_skeleton_mutex);
  const auto it = g_skeletons.find(skeleton);
  if (it == g_skeletons.end() || it->second.applied == scale) {
    return;
  }
  const float was = it->second.applied;
  if (!WriteDrives(base, skeleton, it->second, scale, &was)) {
    if (was != 1.0f) {
      g_scaled_skeletons.fetch_sub(1, std::memory_order_relaxed);
    }
    g_skeletons.erase(it);
    return;
  }
  if ((was != 1.0f) != (scale != 1.0f)) {
    g_scaled_skeletons.fetch_add(scale != 1.0f ? 1 : -1, std::memory_order_relaxed);
  }
  it->second.applied = scale;
  NoteRagdollScale(scale);
}

// ---- Camera shake diagnostics ------------------------------------------------

struct ShakeLogSlot {
  uint32_t effect = 0;
  std::chrono::steady_clock::time_point last{};
};
std::array<ShakeLogSlot, 4> g_shake_log_slots;
std::atomic<int> g_camera_shake_noted{0};

}  // namespace

// Skater state update (r3 = state object): counts airborne frames at +0x2C.
extern "C" REX_FUNC(sub_82D8ADE8) {
  if (REXCVAR_GET(skate3_cheat_no_air_timer) && ctx.r3.u32 >= 0x10000) {
    StoreU32(base, ctx.r3.u32 + 0x2C, 0);
  }
  __imp__sub_82D8ADE8(ctx, base);
}

// Landing impact check (reads physics_wipeout +0xF8 / +0xFC).
extern "C" REX_FUNC(sub_82D90D08) {
  ApplyWipeoutCheats(base);
  __imp__sub_82D90D08(ctx, base);
}

// Off-board air update (reads physics_wipeout +0x140, the fall speed limit).
extern "C" REX_FUNC(sub_82D4D838) {
  ApplyWipeoutCheats(base);
  __imp__sub_82D4D838(ctx, base);
}

// Bail end check (reads physics_wipeout +0x124, the Hall of Meat time limit).
extern "C" REX_FUNC(sub_82D3E980) {
  ApplyWipeoutCheats(base);
  __imp__sub_82D3E980(ctx, base);
}

// ClusteredMeshLineTest job (r3 = items, r4 = job; 80-byte items, result at
// item +0x24, its surface flags at +0x64).
extern "C" REX_FUNC(sub_8276D608) {
  const uint32_t count = ctx.r3.u32, job = ctx.r4.u32;
  __imp__sub_8276D608(ctx, base);
  if (REXCVAR_GET(skate3_cheat_out_of_bounds)) {
    ClearLineTestFlags(base, job, count, 80, 0x24, 0x64);
  }
}

// Second line test job (r3 = items, r4 = job; 112-byte items, result at
// item +0x4C, its surface flags at +0x84).
extern "C" REX_FUNC(sub_8276DE40) {
  const uint32_t count = ctx.r3.u32, job = ctx.r4.u32;
  __imp__sub_8276DE40(ctx, base);
  if (REXCVAR_GET(skate3_cheat_out_of_bounds)) {
    ClearLineTestFlags(base, job, count, 112, 0x4C, 0x84);
  }
}

// Skater contact update (r3 = skater physics; three contacts at +0x5F0).
extern "C" REX_FUNC(sub_82DB6580) {
  const uint32_t skater = ctx.r3.u32;
  __imp__sub_82DB6580(ctx, base);
  const bool on = REXCVAR_GET(skate3_cheat_out_of_bounds);
  NoteSwitch(g_out_of_bounds_noted, on, "Go Out of Bounds");
  if (on && skater >= 0x10000) {
    for (uint32_t contact = 0; contact < 3; ++contact) {
      StoreU32(base, skater + 0x5F0 + contact * 0x30, 0);
    }
  }
}

// The black-screen check's residency query reports here (its sub_82478D00
// hook lives in skate3_world_loading.cpp, which already answers it for New
// San Van while that world loads); a false answer is remembered for the
// check's hook below.
namespace {
thread_local bool tl_black_screen_not_resident = false;
std::atomic<int> g_black_screen_noted{0};
}  // namespace
void skate3::cheats::OnBlackScreenResidency(bool resident) {
  if (!resident) {
    tl_black_screen_not_resident = true;
  }
}

// Black-screen check (r3 = scene; scene-visible byte at +0x347A5).
extern "C" REX_FUNC(sub_827FF648) {
  const uint32_t visible = ctx.r3.u32 + 0x347A5;
  tl_black_screen_not_resident = false;
  __imp__sub_827FF648(ctx, base);
  const bool on = REXCVAR_GET(skate3_cheat_no_black_screen);
  NoteSwitch(g_black_screen_noted, on, "No Black Screen");
  if (on && tl_black_screen_not_resident) {
    base[visible] = 1;
  }
  tl_black_screen_not_resident = false;
}

// Skater skeleton drive setup (r3 = skeleton; called once from the skeleton
// constructor sub_82BE4290): keeps the game's values and applies the slider.
extern "C" REX_FUNC(sub_82BE7860) {
  const uint32_t skeleton = ctx.r3.u32;
  __imp__sub_82BE7860(ctx, base);
  if (skeleton < 0x10000) {
    return;
  }
  TrackedSkeleton tracked;
  for (uint32_t i = 0; i < tracked.setup.size(); ++i) {
    if (!LoadU32(base, DriveWordAddress(skeleton, i), tracked.setup[i])) {
      return;
    }
  }
  const float scale = RagdollScale();
  if (scale != 1.0f) {
    WriteDrives(base, skeleton, tracked, scale, nullptr);
    NoteRagdollScale(scale);
  }
  tracked.applied = scale;
  std::lock_guard<std::mutex> lock(g_skeleton_mutex);
  const auto it = g_skeletons.find(skeleton);
  if (it != g_skeletons.end() && it->second.applied != 1.0f) {
    g_scaled_skeletons.fetch_sub(1, std::memory_order_relaxed);
  }
  if (scale != 1.0f) {
    g_scaled_skeletons.fetch_add(1, std::memory_order_relaxed);
  }
  g_skeletons[skeleton] = tracked;
  if (g_skeletons.size() > 512) {
    // Characters stream in and out: drop skeletons whose blocks no longer
    // hold what was written (freed or rebuilt elsewhere).
    for (auto entry = g_skeletons.begin(); entry != g_skeletons.end();) {
      uint32_t bits = 0;
      const float applied = entry->second.applied;
      const bool live =
          LoadU32(base, DriveWordAddress(entry->first, 0), bits) &&
          bits == FloatToBits(BitsToFloat(entry->second.setup[0]) * applied);
      if (live) {
        ++entry;
        continue;
      }
      if (applied != 1.0f) {
        g_scaled_skeletons.fetch_sub(1, std::memory_order_relaxed);
      }
      entry = g_skeletons.erase(entry);
    }
  }
}

// Joint motor updates (r3 = motor controller, +8 = skeleton): words 0-3 and
// words 5-6 respectively.
extern "C" REX_FUNC(sub_82BEB0C8) {
  ApplyRagdollScale(base, ctx.r3.u32);
  __imp__sub_82BEB0C8(ctx, base);
}
extern "C" REX_FUNC(sub_82BEB488) {
  ApplyRagdollScale(base, ctx.r3.u32);
  __imp__sub_82BEB488(ctx, base);
}

// Camera ShakeEffect evaluation (r4 = effect; f1 = time step, f2 = speed,
// r9 = shaking on/off). Effect +0x24 shake time, +0x28 impulse, +0x2C blend;
// effect +0 = its settings: +0x2FC/+0x300 amplitude at rest / at speed
// +0x304, +0x308/+0x30C frequency, +0x310 its speed, +0x2F4 impulse frequency.
extern "C" REX_FUNC(sub_82E05800) {
  const uint32_t effect = ctx.r4.u32;
  const bool no_shake = REXCVAR_GET(skate3_cheat_no_camera_shake);
  NoteSwitch(g_camera_shake_noted, no_shake, "Remove Camera Shake");
  const bool log = REXCVAR_GET(skate3_camera_shake_log) && effect >= 0x10000;
  float impulse = 0.0f, blend = 0.0f;
  if (log) {
    LoadF32(base, effect + 0x28, impulse);
    LoadF32(base, effect + 0x2C, blend);
  }
  if (no_shake && effect >= 0x10000) {
    StoreU32(base, effect + 0x28, 0);
    StoreU32(base, effect + 0x2C, FloatToBits(-1.0f));
  }
  const double step = ctx.f1.f64, speed = ctx.f2.f64, arg3 = ctx.f3.f64;
  const uint32_t shaking = ctx.r9.u32 & 0xFF;
  __imp__sub_82E05800(ctx, base);
  if (!log) {
    return;
  }
  const auto now = std::chrono::steady_clock::now();
  ShakeLogSlot* slot = nullptr;
  for (ShakeLogSlot& s : g_shake_log_slots) {
    if (s.effect == effect || s.effect == 0) {
      slot = &s;
      break;
    }
  }
  if (slot == nullptr || (slot->effect == effect && now - slot->last < std::chrono::milliseconds(250))) {
    return;
  }
  slot->effect = effect;
  slot->last = now;
  float time = 0.0f, blend_after = 0.0f;
  LoadF32(base, effect + 0x24, time);
  LoadF32(base, effect + 0x2C, blend_after);
  uint32_t settings = 0;
  float p[12] = {};
  if (LoadU32(base, effect, settings)) {
    for (int i = 0; i < 12; ++i) {
      LoadF32(base, settings + 0x2F4 + i * 4, p[i]);
    }
  }
  REXLOG_INFO(
      "camera-shake: effect={:08X} speed={:.2f} step={:.4f} arg3={:.2f} shaking={} "
      "impulse={:.3f} blend={:.3f}->{:.3f} time={:.2f} settings={:08X} "
      "[2F4 imp_freq={:.3f} 2F8={:.3f} amp {:.3f}->{:.3f} at speed {:.2f} "
      "freq {:.3f}->{:.3f} at speed {:.2f} 314={:.3f} 318={:.3f} 31C={:.3f} 320={:.3f}] "
      "removed={}",
      effect, speed, step, arg3, shaking, impulse, blend, blend_after, time, settings, p[0],
      p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8], p[9], p[10], p[11], no_shake ? 1 : 0);
}
