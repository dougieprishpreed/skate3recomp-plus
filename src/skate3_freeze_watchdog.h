#pragma once

#include <cstdint>

namespace skate3::freeze_capture {

// Progress means a completed gameplay update, not a moving camera or a render
// swap. Standing still therefore cannot trip this detector. Suppress automatic
// captures in menus, outside New San Van and after shutdown starts.
class Watchdog {
 public:
  bool Poll(int64_t now_us, int64_t completed_us, uint64_t completed,
            bool eligible) {
    if (!eligible || completed_us == 0 || now_us < completed_us) {
      latched_ = false;
      progress_ = completed;
      return false;
    }
    if (completed != progress_) {
      latched_ = false;
      progress_ = completed;
    }
    if (latched_ || now_us - completed_us < 10'000'000) return false;
    latched_ = true;
    return true;
  }

 private:
  uint64_t progress_ = 0;
  bool latched_ = false;
};

}  // namespace skate3::freeze_capture
