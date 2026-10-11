// Looped clip on New San Van's Stadium video screens.
//
// Skate 2 draws its jumbotrons with incandescent.videoscreen (videoscreen.fx:
// the baseincandescent body, diffuse x lerp(1, lightmap, 0) x 1, over a
// diffuse the game streamed video frames into); the BAM data only carries the
// "JUMBO TRON TEMP" placeholder diffuse. tools/build_jumbotron_video.py
// converts a Skate 2 movie into BC1 frames with mips
// (mods/skate2/jumbotron/<clip>.s2jv); this module loads the file off the
// render thread, and the native renderer (UpdateJumbotron in
// skate3_native_scene_gpu.cpp) streams the current frame into the screens'
// diffuse slot.

#include "skate3_jumbotron.h"

#include <atomic>
#include <cstring>
#include <fstream>
#include <memory>
#include <string>
#include <thread>

#include <rex/cvar.h>
#include <rex/logging.h>

REXCVAR_DEFINE_BOOL(skate3_skate2_jumbotron, true, "Skate 3",
                    "Play a looped clip on New San Van's Stadium video screens "
                    "(mods/skate2/jumbotron/<clip>.s2jv from tools/build_jumbotron_video.py); "
                    "off shows the placeholder art")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DEFINE_STRING(skate3_skate2_jumbotron_clip, "attract", "Skate 3",
                      "Clip name for New San Van's Stadium video screens "
                      "(mods/skate2/jumbotron/<name>.s2jv)")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);

namespace skate3::jumbotron {
namespace {

constexpr uint32_t kVersion = 1;
constexpr uint32_t kFormatBc1 = 1;

struct LoadState {
  std::atomic<bool> done{false};
  bool ok = false;
  Clip clip;
};

std::filesystem::path clip_dir;
std::shared_ptr<LoadState> state;  // render thread only
std::string state_name;
uint32_t last_serial = 0;

bool Load(const std::filesystem::path& path, Clip& clip) {
  std::ifstream f(path, std::ios::binary);
  if (!f) {
    REXLOG_WARN("jumbotron: {} not found; the Stadium screens stay dark "
                "(python tools/build_jumbotron_video.py)",
                path.string());
    return false;
  }
  uint32_t h[16] = {};
  f.read(reinterpret_cast<char*>(h), sizeof(h));
  // Little-endian file on little-endian hosts (x64 / arm64).
  if (!f || std::memcmp(h, "S2JV", 4) != 0 || h[1] != kVersion || h[9] != kFormatBc1 ||
      h[2] < 4 || h[3] < 4 || h[2] > 4096 || h[3] > 4096 || h[4] == 0 || h[4] > 12 ||
      h[5] == 0 || h[6] == 0 || h[7] == 0) {
    REXLOG_WARN("jumbotron: {} is not a version {} BC1 clip", path.string(), kVersion);
    return false;
  }
  clip.width = h[2];
  clip.height = h[3];
  clip.mips = h[4];
  clip.frames = h[5];
  clip.fps = double(h[6]) / double(h[7]);
  clip.frame_bytes = h[8];
  uint32_t expected = 0;
  for (uint32_t m = 0; m < clip.mips; ++m) {
    const uint32_t w = clip.width >> m, hh = clip.height >> m;
    if (w % 4 != 0 || hh % 4 != 0) {
      REXLOG_WARN("jumbotron: {} mip {} is not block aligned", path.string(), m);
      return false;
    }
    expected += (w / 4) * (hh / 4) * 8;
  }
  if (expected != clip.frame_bytes) {
    REXLOG_WARN("jumbotron: {} frame size {} != {}", path.string(), clip.frame_bytes, expected);
    return false;
  }
  clip.data.resize(size_t(clip.frames) * clip.frame_bytes);
  f.read(reinterpret_cast<char*>(clip.data.data()), std::streamsize(clip.data.size()));
  if (size_t(f.gcount()) != clip.data.size()) {
    REXLOG_WARN("jumbotron: {} is truncated", path.string());
    clip.data.clear();
    return false;
  }
  REXLOG_INFO("jumbotron: loaded {} ({} frames, {}x{} x{} mips BC1, {:.3f} fps, {:.1f} s, "
              "{:.1f} MB)",
              path.string(), clip.frames, clip.width, clip.height, clip.mips, clip.fps,
              clip.frames / clip.fps, clip.data.size() / 1e6);
  return true;
}

}  // namespace

void Install(const std::filesystem::path& app_root) {
  clip_dir = app_root / "mods" / "skate2" / "jumbotron";
}

const Clip* Acquire() {
  const std::string name(REXCVAR_GET(skate3_skate2_jumbotron_clip));
  if (!state || state_name != name) {
    if (state && !state->done.load(std::memory_order_acquire)) {
      return nullptr;  // let the previous load finish first
    }
    auto next = std::make_shared<LoadState>();
    next->clip.serial = ++last_serial;
    state = next;
    state_name = name;
    std::thread([next, path = clip_dir / (name + ".s2jv")] {
      next->ok = Load(path, next->clip);
      next->done.store(true, std::memory_order_release);
    }).detach();
    return nullptr;
  }
  if (!state->done.load(std::memory_order_acquire) || !state->ok) {
    return nullptr;
  }
  return &state->clip;
}

bool Unavailable() {
  return state && state->done.load(std::memory_order_acquire) && !state->ok;
}

void Release() {
  if (state && state->done.load(std::memory_order_acquire)) {
    state.reset();
    state_name.clear();
  }
}

}  // namespace skate3::jumbotron
