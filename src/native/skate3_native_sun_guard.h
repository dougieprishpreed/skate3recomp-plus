#pragma once
#include <cstdint>
#include <cstring>

namespace skate3::native_scene {
inline float SunAxisDot(const float* a, const float* b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

struct SunAxisCandidate {
  float axis[3] = {};
  uint64_t capture = 0;
  bool have = false;
  bool confirmed = false;

  bool Confirm(const float* sun, uint64_t fresh_capture, bool fresh_gameplay) {
    if (!fresh_gameplay) {
      *this = {};
      return false;
    }
    const bool matches = have && SunAxisDot(axis, sun) > 0.9999f;
    if (have && capture == fresh_capture) {
      // Rendering the same captured scene again is not another observation.
      return confirmed && matches;
    }
    confirmed = matches && fresh_capture > capture && fresh_capture - capture <= 2;
    std::memcpy(axis, sun, sizeof(axis));
    capture = fresh_capture;
    have = true;
    return confirmed;
  }
};
}  // namespace skate3::native_scene
