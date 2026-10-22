#pragma once

#include <cstdint>

namespace skate3::native_scene { struct FrameScene; struct DrawItem; }

namespace skate3::actor_history {
// Optional observer. All scene/guest reads occur on the builder thread.
void Begin(uint8_t* base, const native_scene::FrameScene& scene, double now, uint64_t guest_frame);
void Retime(const native_scene::DrawItem& item, uint64_t key, uint32_t count,
            double period, double play);
void End(const native_scene::FrameScene& scene, double camera_tick, bool retimed);
void Request();
void Shutdown();
}  // namespace skate3::actor_history
