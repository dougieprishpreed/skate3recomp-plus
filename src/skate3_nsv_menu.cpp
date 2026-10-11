// New San Vanelona entries in the pause-menu Locations list.
//
// tools/build_nsv_dlc.py installs a DLC-style content package whose
// fe_locations entries belong to the world "dist_newsanvan" and each borrow a
// distinct retail locator; it also writes mods/skate2/nsv_spawns.txt mapping
// every borrowed locator to a Skate 2 spawn transform. Choosing an entry runs
// the retail teleport step sub_828646E0(manager, &world key, locator name,
// matrix, ...) (reports/log099_nsv_locations_menu.md), which stores the key
// in 830B7AE8+48 and teleports in-world when it equals +40, else posts the
// event3 world reload. For New San Van's key the same call runs with the
// entry's Skate 2 spawn matrix and the key chosen by ArmMenuSpawn, so the
// game's own teleport flow loads (or moves inside) New San Van. An entry's
// optional `stream=` (the Skate 2 DLC maps) names the world stream to run in
// BAM's place; a pick on another stream than the running one reloads.

#include "generated/skate3_init.h"
#include "native/skate3_native_guest_read.h"
#include "skate3_nsv_menu.h"
#include "skate3_world_loading.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include <rex/cvar.h>
#include <rex/logging.h>

REXCVAR_DEFINE_BOOL(skate3_nsv_menu, true, "Skate 3",
                    "Load New San Van when one of its pause-menu Locations entries is chosen "
                    "(package from tools/build_nsv_dlc.py)");

namespace {

constexpr uint64_t kNewSanVanWorld = 0xF715A377BE05BD01ull;  // h64("dist_newsanvan")
constexpr uint32_t kWorldState = 0x830B7AE8;

struct Spawn {
  std::string locator;
  std::string title;
  std::array<float, 16> transform{};
  std::string stream = "BAM";  // Skate 2 world stream (DLC maps name theirs)
};
std::vector<Spawn> spawns;  // Loaded during Install before guest execution.

bool U64(uint8_t* base, uint32_t address, uint64_t& value) {
  if (address < 0x10000 || address > 0xfffffff7 ||
      !skate3::native_scene::GuestTryCopy(&value, base + address, sizeof(value))) {
    return false;
  }
  value = __builtin_bswap64(value);
  return true;
}

// Guest C string (printable ASCII, at most 63 characters), or empty.
std::string CStr(uint8_t* base, uint32_t address) {
  if (address < 0x10000 || address > 0xffffff00) return {};
  char buf[64] = {};
  if (!skate3::native_scene::GuestTryCopy(buf, base + address, sizeof(buf) - 1)) return {};
  size_t len = 0;
  while (len < sizeof(buf) - 1 && buf[len] >= 32 && buf[len] < 127) ++len;
  return buf[len] == 0 ? std::string(buf, len) : std::string{};
}

// Translation of a guest row-major matrix (floats 12..14), for logs.
std::array<float, 3> Position(uint8_t* base, uint32_t matrix) {
  std::array<float, 3> xyz{};
  std::array<uint32_t, 3> bits{};
  if (matrix >= 0x10000 &&
      skate3::native_scene::GuestTryCopy(bits.data(), base + matrix + 48, sizeof(bits))) {
    for (size_t i = 0; i < 3; ++i) {
      const uint32_t v = __builtin_bswap32(bits[i]);
      std::memcpy(&xyz[i], &v, 4);
    }
  }
  return xyz;
}

}  // namespace

void skate3::nsv_menu::Install(const std::filesystem::path& app_root) {
  spawns.clear();
  const auto path = app_root / "mods" / "skate2" / "nsv_spawns.txt";
  std::ifstream in(path);
  if (!in) {
    REXLOG_INFO("nsv-menu: no spawn table at {}; run tools/build_nsv_dlc.py", path.string());
    return;
  }
  std::string line;
  while (std::getline(in, line)) {
    const auto hash = line.find('#');
    std::istringstream fields(line.substr(0, hash));
    Spawn spawn;
    if (!(fields >> spawn.locator)) continue;
    bool complete = true;
    for (float& value : spawn.transform) complete = complete && static_cast<bool>(fields >> value);
    if (!complete) {
      REXLOG_WARN("nsv-menu: skipping malformed spawn line '{}'", line);
      continue;
    }
    std::string option;
    while (fields >> option) {
      if (option.starts_with("stream=") && option.size() > 7) spawn.stream = option.substr(7);
    }
    if (hash != std::string::npos) {
      spawn.title = line.substr(hash + 1);
      spawn.title.erase(0, spawn.title.find_first_not_of(' '));
    }
    if (spawn.title.empty()) spawn.title = spawn.locator;
    spawns.push_back(std::move(spawn));
  }
  REXLOG_INFO("nsv-menu: {} New San Van spawns from {}", spawns.size(), path.string());
}

extern "C" REX_FUNC(sub_828646E0) {
  uint64_t world = 0;
  const bool have_world = U64(base, ctx.r4.u32, world);
  const std::string locator = CStr(base, ctx.r5.u32);
  const auto xyz = Position(base, ctx.r6.u32);
  static std::atomic<uint32_t> logged{0};
  if (have_world && world == kNewSanVanWorld && REXCVAR_GET(skate3_nsv_menu)) {
    auto it = std::find_if(spawns.begin(), spawns.end(),
                           [&](const Spawn& s) { return s.locator == locator; });
    if (it == spawns.end()) {
      if (spawns.empty()) {
        REXLOG_ERROR("nsv-menu: no New San Van spawn table; run tools/build_nsv_dlc.py");
        return;  // The retail call would load the package's stream-less world.
      }
      REXLOG_WARN("nsv-menu: locator '{}' not in the spawn table; using '{}'", locator,
                  spawns.front().title);
      it = spawns.begin();
    }
    uint64_t key = 0;
    if (!skate3::world_loading::ArmMenuSpawn(base, it->title, it->stream, key)) return;
    // Key and matrix in an aligned scratch frame below the stack pointer; the
    // callee's frames grow below it (the matrix is read with lvx128).
    auto call = ctx;
    const uint32_t frame = (ctx.r1.u32 - 256) & ~15u;
    const uint64_t key_be = __builtin_bswap64(key);
    std::memcpy(base + frame + 80, &key_be, 8);
    for (size_t i = 0; i < it->transform.size(); ++i) {
      uint32_t bits;
      std::memcpy(&bits, &it->transform[i], 4);
      bits = __builtin_bswap32(bits);
      std::memcpy(base + frame + 96 + i * 4, &bits, 4);
    }
    call.r1.u64 = frame;
    call.r4.u64 = frame + 80;
    call.r6.u64 = frame + 96;
    REXLOG_INFO("nsv-menu: Locations entry locator='{}' -> '{}' stream={} ({:.3f},{:.3f},{:.3f}) "
                "key={:016X}",
                locator, it->title, it->stream, it->transform[12], it->transform[13],
                it->transform[14], key);
    __imp__sub_828646E0(call, base);
    return;
  }
  if (logged.fetch_add(1) < 64) {
    uint64_t loaded = 0, target = 0;
    U64(base, kWorldState + 40, loaded);
    U64(base, kWorldState + 48, target);
    REXLOG_INFO("nsv-menu: retail location teleport world={:016X} locator='{}' "
                "pos=({:.3f},{:.3f},{:.3f}) +40={:016X} +48={:016X} caller={:08X}",
                world, locator, xyz[0], xyz[1], xyz[2], loaded, target, uint32_t(ctx.lr));
  }
  if (have_world) skate3::world_loading::BeforeRetailTeleport(base, world);
  __imp__sub_828646E0(ctx, base);
}
