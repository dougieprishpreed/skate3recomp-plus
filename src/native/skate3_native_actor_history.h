#pragma once

#include <cstdint>

namespace skate3::native_scene { struct FrameScene; struct DrawItem; }

namespace skate3::actor_history {
// Camera smoothing events of one frame (SmoothCamera; 0 = smoothing not run).
enum CameraEvent : uint32_t {
  kCameraSmoothed = 1,      // the frame used the re-timed camera
  kCameraFewSamples = 2,    // raw frame: fewer than three ring samples
  kCameraStale = 4,         // raw frame: newest camera sample older than 100 ms
  kCameraRingGap = 8,       // ring restarted: first sample or a gap over 100 ms
  kCameraRingJump = 16,     // ring restarted: camera moved over 5 m
  kCameraRingTurn = 32,     // ring restarted: hard cut in rotation
  kCameraClockSnap = 64,    // playback clock over 100 ms off its target, snapped
  kCameraDisabled = 128,    // reconstruction check failed, smoothing off this run
  kCameraBuiltSample = 256, // the sampler missed this frame's camera; the build added it
  kCameraOutlier = 512,     // a camera sample off the path of its neighbours was dropped
};
// How a recorded part's smoothing history served it this frame.
enum PartEvent : uint32_t {
  kPartReset = 1,         // discontinuity reset its history
  kPartRawShort = 2,      // raw pose: no history, or newest over 100 ms old
  kPartRawSize = 4,       // raw pose: palette size changed inside the window
  kPartCasterHold = 8,    // shadow-caster capture replaced by the held pose
  kPartInterpolated = 16, // evaluated at the shared playback time
  kPartHeld = 32,         // short/restarted history drawn at the playback time
};
// Frame-level state the builder hands to End.
struct FrameState {
  uint32_t camera_events = 0;
  float camera_err_ms = 0, camera_age_ms = -1, camera_period_ms = 0;
  double camera_play = 0;
  uint32_t render_paused = 0;
  uint32_t stream_loads = 0, stream_unloads = 0, stream_loads_done = 0, stream_unloads_done = 0;
};

// Optional observer. All scene/guest reads occur on the builder thread.
void Begin(uint8_t* base, const native_scene::FrameScene& scene, double now, uint64_t guest_frame);
// The game's own camera (scene.cam_pos/view_proj before smoothing, the view
// matrix the build read at host time view_t); picks the nearest parts to it.
void Camera(const native_scene::FrameScene& scene, const float view[16], double view_t);
// A camera sample the smoother receives (any thread): source 1 = the game's
// SetViewMatrix, 2 = the polling sampler; guest_ticks/trace_serial = the guest
// clock and its read trace position at that moment. Kept with the frames
// together with the guest clock reads around them; nothing while capture is off.
void CameraSample(double t, const float view[16], const float proj[16], uint32_t source,
                  uint64_t guest_ticks, uint64_t trace_serial);
// The guest timing trace (rex/chrono/guest_tick_trace.h: clock reads with
// their code site, kernel waits, signals, timers, title events), handed in by
// the scene code so this observer stays SDK-independent. TickRead mirrors
// rex::chrono::GuestTickRead.
struct TickRead {
  uint64_t ticks;
  uint32_t thread, serial, site;
  uint16_t kind, arg;
};
// Title events in that trace (kind 5, arg): the frame boundary's steps on the
// game's main thread.
enum TitleTimingEvent : uint16_t {
  kTitleCameraSet = 1,     // the game stored the main camera (SetViewMatrix)
  kTitleFrameEndBegin = 2, // guest D3D Swap entered: our frame-end work starts
  kTitleSwapCall = 3,      // our frame-end work done, the guest Swap runs
  kTitlePaceBegin = 4,     // guest Swap returned, the frame pacing wait starts
  kTitlePaceEnd = 5,       // pacing done: the next guest frame starts
  kTitleSimTick = 6,       // simulation tick released (site 1 = locked to the frames)
  kTitleSimRate = 7,       // the game set its simulation rate (site = Hz)
};
// kTitleCameraSet's site: the game's simulation tick count at that moment.

struct ClockAccess {
  void (*set_trace)(bool enabled) = nullptr;
  size_t (*copy_trace)(uint64_t from_serial, TickRead* out, size_t max) = nullptr;
  uint64_t tick_frequency = 0;
};
void SetClockAccess(const ClockAccess& access);
void Retime(const native_scene::DrawItem& item, uint64_t key, uint32_t count,
            double period, double play);
void Note(const native_scene::DrawItem& item, uint32_t part_events, float age_ms = -1.0f);
void End(const native_scene::FrameScene& scene, double camera_tick, bool retimed,
         const FrameState& state);
void Request();
void Shutdown();
}  // namespace skate3::actor_history
