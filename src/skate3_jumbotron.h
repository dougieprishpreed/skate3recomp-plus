#pragma once

#include <cstdint>
#include <filesystem>
#include <vector>

// Looped clip on New San Van's Stadium video screens (see skate3_jumbotron.cpp
// and tools/build_jumbotron_video.py).
namespace skate3::jumbotron {

// A converted clip: BC1 frames, each its mip chain in order (rows of 8-byte
// blocks, mip m is (width >> m) x (height >> m)).
struct Clip {
  uint32_t width = 0;
  uint32_t height = 0;
  uint32_t mips = 0;
  uint32_t frames = 0;
  uint32_t frame_bytes = 0;
  double fps = 30.0;
  uint32_t serial = 0;  // unique per load (never 0)
  std::vector<uint8_t> data;

  const uint8_t* Frame(uint32_t index) const {
    return data.data() + size_t(index % frames) * frame_bytes;
  }
};

// Remembers <app_root>/mods/skate2/jumbotron; call during startup.
void Install(const std::filesystem::path& app_root);

// Render thread. The first call starts loading the clip named by
// skate3_skate2_jumbotron_clip on a background thread; returns the clip once
// loaded, nullptr while loading or when the file is missing or invalid.
const Clip* Acquire();

// True once a load finished without a usable clip.
bool Unavailable();

// Render thread: frees the loaded clip (the next Acquire loads it again).
void Release();

}  // namespace skate3::jumbotron
