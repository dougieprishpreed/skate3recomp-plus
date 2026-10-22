#pragma once

#include <cstdint>

namespace skate3::native_scene {

// World identity is a load serial plus the destination's NSV bit. It does
// not depend on presence/gameplay, which changes after scenery can submit.
inline bool PersistentStaticProxyAllowed(uint64_t world_identity, uint8_t family) {
  return (world_identity & 1u) == 0 || family != 11;
}

struct ShadowWorldLifetime {
  uint64_t identity = ~uint64_t(0);
  bool awaiting_gameplay = false;

  bool Update(uint64_t world_identity, bool gameplay) {
    const bool changed = identity != world_identity;
    if (changed) {
      identity = world_identity;
      awaiting_gameplay = true;
    }
    // Loading can continue publishing an outgoing/partial world behind its
    // UI. Clear again at takeover; ordinary pause/resume retains the cache.
    const bool takeover = awaiting_gameplay && gameplay;
    if (takeover) awaiting_gameplay = false;
    return changed || takeover;
  }
};

}  // namespace skate3::native_scene
