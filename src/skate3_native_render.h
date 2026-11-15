#pragma once

// Skate 3 native renderer (data-driven scene renderer).
//
// The guest-function hooks in skate3_native_render.cpp are link-time
// weak-symbol overrides and are always present; everything they do is gated
// behind the skate3_native_render cvar. Install() only announces state and
// validates configuration. The scene capture/build and rendering live in
// skate3_native_scene.cpp.

#include <cstdint>

namespace skate3::native_render {

void Install();
// The game's simulation tick wait, replaced (see skate3_native_render.cpp):
// returns at the next tick; period_100ns = the game's tick period.
void SimTickWait(int64_t period_100ns);
// steady_clock now in nanoseconds (the frame pacer's clock).
int64_t SteadyNowNs();
// Whether camera/actor smoothing should run this frame (full or adaptive
// smoothing, see skate3_motion_smoothing_auto / skate3_adaptive_smoothing_max).
bool MotionSmoothingWanted();
// Share of the full smoothing delay and filter to apply this frame:
// 0 = the game's own frame, 1 = full smoothing.
double MotionSmoothingScale();
// The camera showed an uneven simulation step (no step or two).
void NoteUnevenStep();

}  // namespace skate3::native_render
