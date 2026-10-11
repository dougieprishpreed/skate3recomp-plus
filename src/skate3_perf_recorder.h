#pragma once

// Performance flight recorder: rolling per-frame and per-tick history of
// where frame time goes (guest frame loop, native render, every process
// thread's CPU, GPU/VRAM, memory, disk, decode queues, world streaming), so a
// transient slowdown can be attributed after the fact. Writes CSVs + a log
// summary on the capture hotkey (Home), or when the guest frame rate drops
// and recovers with skate3_perf_recorder_auto (off by default).

#include <cstdint>

namespace rex::ui {
class Presenter;
}

namespace skate3::perf_recorder {

// Guest render thread, once per guest frame at the swap boundary: time the
// frame cap slept, BuildFrameScene time, meshes submitted, the camera
// smoothing clock (play-clock error vs its lag target, camera tick period)
// and the frame's camera world position.
void OnGuestFrame(uint64_t pace_ns, uint64_t build_ns, uint32_t meshes,
                  float smooth_err_ms, float smooth_period_ms, const float cam[3]);

// Command processor thread, one record per native RenderScene call.
struct RenderRecord {
  uint64_t total_ns = 0;    // RenderScene wall time
  uint64_t cpu_cycles = 0;  // this thread's CPU cycles during the call
  uint64_t pre_ns = 0;    // setup before the item loop (incl. prewarm/evict)
  uint64_t items_ns = 0;  // opaque + transparent item loop
  uint64_t shadow_ns = 0; // CSM atlas + static sun map + gate
  uint64_t settle_ns = 0; // post-takeover settle pass
  uint64_t commit_ns = 0; // decode-result commits
  uint64_t twod_ns = 0;   // 2D/HUD replay
  uint64_t tail_ns = 0;   // resolve, post chain, final transitions
  uint64_t decode_ns = 0; // inline mesh + texture decodes
  uint32_t mesh_decodes = 0;
  uint32_t tex_decodes = 0;
  uint32_t items = 0;
  uint32_t draws = 0;
  uint8_t path = 0;         // 0 = native frame; else the yield reason
  uint8_t nsm_rebuild = 0;  // static sun map rebuild reason (0 = none)
  uint8_t ws_recenter = 0;  // props' world-shadow map re-centered
  bool complete = false;    // phase fields valid (a fully rendered frame)
};
void OnRenderFrame(const RenderRecord& r);

// World streaming / loading activity (any thread).
enum class Activity : uint8_t {
  kStreamLoad,
  kStreamUnload,
  kStreamLoadDone,
  kStreamUnloadDone,
  kFileRequest,
  kDecompress,
  kCount,
};
void Count(Activity a);
void SetRenderPaused(bool paused);

// This thread's consumed CPU cycles (monotonic per thread; converted to time
// at dump with the calibrated cycle rate). 0 where unsupported.
uint64_t ThreadCycles();
// Names the calling thread for the per-thread CPU breakdown.
void NameCurrentThread(const char* name);

// Capture hotkey: dump the recent history now (plus a few seconds after).
void RequestCapture();

// Presenter whose guest-frame GPU stats (frame span on the GPU timeline,
// GPU command thread fence waits) the recorder samples. Set on the UI
// thread at startup; cleared at shutdown before the presenter goes away.
void SetPresenter(rex::ui::Presenter* presenter);

}  // namespace skate3::perf_recorder
