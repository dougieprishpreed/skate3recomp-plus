// Performance flight recorder (see skate3_perf_recorder.h).
//
// Three rolling histories, all lock-free single-producer rings:
//   guest  - one record per guest frame (swap boundary, guest render thread)
//   render - one record per native RenderScene call (command processor)
//   ticks  - one record per ~100 ms from the sampler thread: every process
//            thread's CPU, system/process CPU, memory, page faults, disk
//            reads, GPU utilization/clocks/temperature/throttle and VRAM
//            (NVML), the process's VRAM use vs budget (DXGI), the native
//            decode queues and world-streaming activity counters.
// The capture hotkey (Home) writes logs/perf_<n>_<time>_{guest,render,ticks,
// threads}.csv (the last ~30 s plus a few seconds after the press) and a log
// summary. With skate3_perf_recorder_auto (off by default) the sampler also
// watches the guest frame rate: a drop below 80% of its recent normal for
// over half a second, followed by 4 s of recovery, writes the same (~30 s
// before the drop through the recovery), comparing the drop with the seconds
// before it.
#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <dxgi1_4.h>
#include <intrin.h>
#include <psapi.h>
#include <tlhelp32.h>
#endif

#include "skate3_perf_recorder.h"
#include "native/skate3_native_guest_read.h"
#include "skate3_freeze_capture.h"

#include "native/skate3_native_guest_read.h"
#include "skate3_native_scene.h"
#include "skate3_world_loading.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include <rex/cvar.h>
#include <rex/logging.h>
#include <rex/ui/presenter.h>

REXCVAR_DEFINE_BOOL(skate3_perf_recorder, false, "Skate 3",
                    "Performance flight recorder: keeps ~2 minutes of per-frame "
                    "timings, per-thread CPU, GPU/VRAM, memory, disk and streaming "
                    "activity, and writes logs/perf_* CSVs + a log summary on the "
                    "Home key (and on frame rate drops with skate3_perf_recorder_auto).")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DEFINE_BOOL(skate3_perf_recorder_auto, false, "Skate 3",
                    "Automatic perf captures: write one when the game's frame rate "
                    "falls below 80% of its recent normal for over half a second "
                    "and then recovers (at most 8 per session). Off: captures are "
                    "written only on the Home key.")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DEFINE_BOOL(skate3_perf_recorder_system, false, "Skate 3",
                    "Perf recorder system sampling: per-thread CPU (a system-wide "
                    "thread snapshot every 2 s) and GPU utilization / clocks / VRAM "
                    "(NVML and DXGI driver queries every second). Off by default: "
                    "those are periodic costs of their own. Frame timings, process "
                    "counters, decode queues and streaming stay recorded either way; "
                    "the stack profiler needs this on to find its threads.")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DEFINE_BOOL(skate3_perf_scene_detail, false, "Skate 3",
                    "Record NSV scene-build phases and rotating CPU draw samples without GPU probes");
REXCVAR_DEFINE_BOOL(skate3_perf_event_detail, false, "Skate 3",
                    "Extra cooperative NSV event phase timings for Home captures: "
                    "resource presentation, primitive setup and GUID lookups. "
                    "Only timed inside a recorded NSV dispatch. Off by default.")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DEFINE_BOOL(skate3_perf_profiler, false, "Skate 3",
                    "Stack-sampling profiler for perf captures: samples the game's "
                    "render thread (~1 kHz) and the GPU command thread (~250 Hz) and "
                    "writes per-function sample counts with each capture "
                    "(logs/perf_*_profile.csv). Costs the sampled threads ~1-2%.")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);

REXCVAR_DEFINE_BOOL(skate3_perf_profile_on_home, false, "Skate 3",
                    "Sample NSV CPU stacks only for the six seconds following Home; "
                    "idle otherwise. Does not enable periodic system/GPU queries.")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);

REXCVAR_DEFINE_BOOL(skate3_perf_work_timeline, false, "Skate 3",
                    "Home-only NSV coarse dispatch/decode/decompression spans and all-thread CPU; no extra stack suspension or GPU queries")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);

namespace skate3::perf_recorder {
namespace {

using Clock = std::chrono::steady_clock;

int64_t NowUs() {
  return std::chrono::duration_cast<std::chrono::microseconds>(
             Clock::now().time_since_epoch())
      .count();
}

// Single-producer ring with per-slot sequence numbers: the consumer copies
// only slots whose sequence was even and unchanged across the copy.
template <typename T, size_t N>
class Ring {
 public:
  void Push(const T& v) {
    const uint64_t i = next_.load(std::memory_order_relaxed);
    Slot& s = slots_[i % N];
    const uint32_t seq = s.seq.load(std::memory_order_relaxed);
    s.seq.store(seq + 1, std::memory_order_relaxed);
    std::atomic_thread_fence(std::memory_order_release);
    s.v = v;
    s.seq.store(seq + 2, std::memory_order_release);
    next_.store(i + 1, std::memory_order_release);
  }
  std::vector<T> Snapshot() const {
    std::vector<T> out;
    const uint64_t end = next_.load(std::memory_order_acquire);
    const uint64_t begin = end > N ? end - N : 0;
    out.reserve(size_t(end - begin));
    for (uint64_t i = begin; i < end; ++i) {
      const Slot& s = slots_[i % N];
      const uint32_t a = s.seq.load(std::memory_order_acquire);
      if ((a & 1u) != 0) {
        continue;
      }
      T v = s.v;
      std::atomic_thread_fence(std::memory_order_acquire);
      if (s.seq.load(std::memory_order_relaxed) != a) {
        continue;
      }
      out.push_back(v);
    }
    return out;
  }
  uint64_t Count() const { return next_.load(std::memory_order_acquire); }

 private:
  struct Slot {
    std::atomic<uint32_t> seq{0};
    T v{};
  };
  Slot slots_[N];
  std::atomic<uint64_t> next_{0};
};

struct GuestEventCallbacks {
  uint64_t total_us = 0, max_us = 0;
  uint64_t registration_guest_us = 0, registration_host_us = 0;
  uint32_t calls = 0, function = 0, subscriber = 0, registration_calls = 0;
  uint64_t resource_us = 0, setup_us = 0, lookup_us = 0;
  uint32_t resource_calls = 0, setup_calls = 0, lookup_calls = 0;
  uint64_t setup_cpu_cycles = 0;
  uint64_t primitive_dispatch_us = 0, primitive_clear_us = 0, primitive_collect_us = 0;
  uint64_t primitive_insert_us = 0, primitive_create_us = 0, primitive_release_us = 0;
  uint64_t primitive_request_us = 0, primitive_dispatch_max_us = 0;
  uint32_t primitive_dispatch_calls = 0, primitive_clear_calls = 0, primitive_collect_calls = 0;
  uint32_t primitive_insert_calls = 0, primitive_create_calls = 0, primitive_release_calls = 0;
  uint32_t primitive_request_calls = 0, primitive_dispatch_function = 0;
  uint32_t lookup_index_calls = 0, lookup_scan_nodes = 0;
  uint64_t contexts_us = 0, parameters_us = 0, registry_us = 0, allocation_us = 0;
  uint32_t contexts_calls = 0, parameters_calls = 0, registry_calls = 0, allocation_calls = 0;
  uint32_t registration_meshes = 0;
  uint64_t context_materials_us = 0, context_factory_us = 0;
  uint64_t context_factory_max_us = 0;
  uint32_t context_factory_key = 0;
  uint64_t context_bindings_us = 0, context_resources_us = 0;
  uint32_t context_materials_calls = 0, context_factory_calls = 0;
  uint32_t context_bindings_calls = 0, context_resources_calls = 0;
  uint32_t context_resource_fast_calls = 0, context_resource_scan_entries = 0;
};
thread_local GuestEventCallbacks g_current_callbacks;
thread_local bool g_event_active = false;
thread_local uint32_t g_callback_depth = 0;
thread_local uint32_t g_callback_target = 0;
thread_local bool g_event_detail = false;
thread_local bool g_callback_forward_pending = false;
thread_local uint32_t g_registration_depth = 0;
thread_local uint32_t g_primitive_depth = 0;

struct SceneBuildDetail {
  uint64_t warm_started_total = 0, warm_published_total = 0;
  uint64_t warm_unready_total = 0, warm_rejected_total = 0;
  uint32_t warm_queue = 0, warm_pending = 0;
  uint64_t capture_us = 0, world_us = 0, merge_us = 0, retain_us = 0;
  uint32_t records = 0, core_hits = 0, core_walks = 0, fingerprints = 0, rebinds = 0;
  uint32_t miss_absent = 0, miss_refresh = 0, miss_pending = 0, miss_invalid = 0, warm_reuse = 0;
  bool valid = false;
  uint64_t geometry_sample_us = 0, cache_sample_us = 0;
  uint32_t geometry_calls = 0, geometry_sample_calls = 0;
  uint64_t item_capacity = 0, reserve_bytes = 0, reserve_us = 0;
  uint32_t reserve_grows = 0;
  uint64_t walk_sample_us = 0, fp_sample_us = 0;
  uint32_t walk_sample_calls = 0, fp_sample_calls = 0, read_faults = 0;
};
thread_local bool g_scene_active = false;
thread_local SceneBuildDetail g_scene_current, g_scene_ready;
thread_local uint64_t g_scene_sample_frame = 0;

struct GuestEvents {
  uint64_t total_us = 0;
  uint64_t max_us = 0;
  uint32_t calls = 0;
  uint32_t type = 0, token = 0, caller = 0;  // slowest dispatch in this frame
  GuestEventCallbacks callbacks;  // within that same slowest dispatch
};
thread_local GuestEvents g_guest_events;

struct GuestRec {
  int64_t t_us = 0;
  uint32_t dt_us = 0;
  uint32_t pace_us = 0;
  uint32_t build_us = 0;
  uint32_t meshes = 0;
  uint64_t cpu_cycles = 0;  // guest render thread, since the previous frame
  GuestEvents events;
  SceneBuildDetail scene;
  float smooth_err_ms = 0.0f;
  float smooth_period_ms = 0.0f;
  float cam[3] = {};  // camera world position
};

struct RenderRec {
  int64_t t_us = 0;
  uint32_t dt_us = 0;
  RenderRecord r;
};

struct ThreadSample {
  uint32_t tid = 0;
  uint32_t cpu_us = 0;
  uint32_t kernel_us = UINT32_MAX, user_us = UINT32_MAX;
};
constexpr int kMaxThreadSamples = 64;

struct TickRec {
  int64_t t_us = 0;
  uint32_t len_us = 0;
  uint32_t guest_frames = 0;
  uint32_t render_frames = 0;
  float sys_busy_pct = -1.0f;
  float proc_cpu_pct = -1.0f;  // 100 = one full core
  float ws_mb = -1.0f;
  float private_mb = -1.0f;
  float faults_s = -1.0f;
  float io_read_mb_s = -1.0f;
  float io_reads_s = -1.0f;
  float io_write_mb_s = -1.0f;
  int16_t gpu_util = -1;
  int16_t gpu_mem_util = -1;
  int16_t gfx_mhz = -1;
  int16_t mem_mhz = -1;
  int16_t pstate = -1;
  // Presenter guest-frame stats: host GPU time span of a recent frame (first
  // to last command on the GPU timeline) and the time the GPU command thread
  // spent blocked on host GPU fences during the last frame.
  float gpu_span_ms = -1.0f;
  float gpu_wait_ms = -1.0f;
  float vram_used_mb = -1.0f;
  float vram_total_mb = -1.0f;
  uint64_t throttle = 0;
  float proc_vram_mb = -1.0f;
  float proc_vram_budget_mb = -1.0f;
  uint32_t queues[4] = {};
  uint32_t activity[size_t(Activity::kCount)] = {};
  uint8_t render_paused = 0;
  uint8_t skate2 = 0;
  uint8_t nthreads = 0;
  ThreadSample threads[kMaxThreadSamples];
};

Ring<GuestRec, 16384> g_guest;
struct SwapRec {
  int64_t t_us = 0, begin_us = 0;
  uint64_t cpu_cycles = 0;
};
Ring<SwapRec, 4096> g_swaps;
Ring<RenderRec, 16384> g_render;
Ring<TickRec, 2048> g_ticks;

// Stack samples of the profiled threads (leaf first).
constexpr int kProfFrames = 20;
// Host UI = the window thread that paints and presents (blocking present
// calls show up there).
constexpr const char* kProfThreads[] = {"render_thread", "GPU Commands", "Host UI"};
// Samples taken this long after the last guest swap are inside the excess
// work of an overlong guest frame (normal frame work is 7-12 ms).
constexpr int64_t kLateUs = 20'000;
struct StackSample {
  int64_t t_us = 0;
  int64_t since_swap_us = 0;  // time since the last guest swap
  uint8_t thread = 0;         // index into kProfThreads
  uint8_t n = 0;
  uint64_t pcs[kProfFrames] = {};
};
Ring<StackSample, 131072> g_prof;  // ~85 s at 1.5 kHz

std::atomic<uint32_t> g_activity[size_t(Activity::kCount)] = {};
std::atomic<bool> g_render_paused{false};
std::atomic<int64_t> g_manual_press_us{0};
constexpr int64_t kManualCaptureUs = 6'000'000;
bool ManualProfileActive(int64_t now) {
  const int64_t press = g_manual_press_us.load(std::memory_order_acquire);
  return press != 0 && now >= press && now - press < kManualCaptureUs &&
         REXCVAR_GET(skate3_perf_profile_on_home) &&
         skate3::world_loading::IsSkate2Gameplay();
}
std::atomic<int64_t> g_last_native_us{0};  // last fully rendered native frame
std::atomic<int64_t> g_last_guest_us{0};   // last guest swap
std::atomic<bool> g_started{false};

// Each claimed lane has ONE producer (a process-lifetime thread id). Reuse the
// existing single-producer recorder without a shared event-insertion mutex.
struct WorkSpan {
  int64_t press_us = 0, begin_us = 0, end_us = 0;
  uint64_t cycles = 0;
  uint32_t tid = 0;
  WorkPhase phase = WorkPhase::kSortCapture;
  WorkAsset asset;
};
constexpr size_t kWorkLanes = 16, kWorkLaneRecords = 4096;
struct WorkLane {
  std::atomic<uint32_t> owner{0};
  std::atomic<int64_t> press{0};
  std::atomic<uint64_t> first{0};
  Ring<WorkSpan, kWorkLaneRecords> records;
};
std::array<WorkLane, kWorkLanes> g_work;
std::atomic<uint64_t> g_work_unassigned{0};

std::atomic<double> g_cycles_per_us{0.0};

// Thread names by id. Heap objects that are never destroyed: the sampler is
// a detached thread and may still touch them while statics are torn down at
// process exit.
std::mutex& NamesMutex() {
  static auto* m = new std::mutex();
  return *m;
}
std::unordered_map<uint32_t, std::string>& Names() {
  static auto* names = new std::unordered_map<uint32_t, std::string>();
  return *names;
}

// Presenter whose guest-frame GPU stats the sampler reads (SetPresenter);
// guarded so the app can detach it at shutdown while a read is in flight.
std::mutex& PresenterMutex() {
  static auto* m = new std::mutex();
  return *m;
}
rex::ui::Presenter* g_presenter = nullptr;  // guarded by PresenterMutex()

// Host GPU span of a recent guest frame and the GPU command thread's fence
// waits in the last frame (the presenter's stats, enabled on first use:
// timestamps around each guest frame, the FPS overlay's data source).
[[maybe_unused]] void SamplePresenter(TickRec& t) {
  std::lock_guard<std::mutex> lock(PresenterMutex());
  if (g_presenter == nullptr) {
    return;
  }
  if (!g_presenter->GuestFrameStatsEnabled()) {
    g_presenter->SetGuestFrameStatsEnabled(true);  // readable from the next tick
    return;
  }
  const rex::ui::Presenter::GuestFrameStats s = g_presenter->GetGuestFrameStats();
  t.gpu_span_ms = float(s.gpu_ms);
  t.gpu_wait_ms = float(s.wait_ms);
}

void StartSampler();

}  // namespace

#if defined(_WIN32)
uint64_t ThreadCycles() {
  ULONG64 cycles = 0;
  QueryThreadCycleTime(GetCurrentThread(), &cycles);
  return uint64_t(cycles);
}

void NameCurrentThread(const char* name) {
  using SetFn = HRESULT(WINAPI*)(HANDLE, PCWSTR);
  static const auto set = reinterpret_cast<SetFn>(
      GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "SetThreadDescription"));
  if (set == nullptr || name == nullptr) {
    return;
  }
  wchar_t wide[128];
  const int n = MultiByteToWideChar(CP_UTF8, 0, name, -1, wide, 128);
  if (n > 0) {
    set(GetCurrentThread(), wide);
  }
}
#else
uint64_t ThreadCycles() { return 0; }
void NameCurrentThread(const char*) {}
#endif

WorkTimelineScope::WorkTimelineScope(WorkPhase phase, bool enabled) : phase_(phase) {
#if defined(_WIN32)
  if (!enabled || !REXCVAR_GET(skate3_perf_work_timeline) ||
      !REXCVAR_GET(skate3_perf_recorder) || !REXCVAR_GET(skate3_perf_profile_on_home) ||
      !skate3::world_loading::IsSkate2Gameplay()) return;
  const int64_t press = g_manual_press_us.load(std::memory_order_acquire);
  static thread_local int64_t checked_press = 0;
  static thread_local bool expired = false;
  if (press != checked_press) { checked_press = press; expired = false; }
  if (!press || expired) return;
  const int64_t now = NowUs();
  if (now < press || now - press >= kManualCaptureUs) { expired = true; return; }
  static thread_local uint32_t lane_index = UINT32_MAX;
  if (lane_index == UINT32_MAX) {
    const uint32_t tid = GetCurrentThreadId();
    for (uint32_t i = 0; i < kWorkLanes; ++i) {
      uint32_t empty = 0;
      if (g_work[i].owner.load(std::memory_order_relaxed) == tid ||
          g_work[i].owner.compare_exchange_strong(empty, tid, std::memory_order_acq_rel)) {
        lane_index = i; break;
      }
    }
    if (lane_index == UINT32_MAX) {
      g_work_unassigned.fetch_add(1, std::memory_order_relaxed);
      expired = true;  // retry at the next Home, not on every work call
      return;
    }
  }
  auto& lane = g_work[lane_index];
  if (lane.press.load(std::memory_order_relaxed) != press) {
    lane.first.store(lane.records.Count(), std::memory_order_relaxed);
    lane.press.store(press, std::memory_order_release);
  }
  lane_ = lane_index; press_us_ = press;
  begin_us_ = now; begin_cycles_ = ThreadCycles();
#else
  (void)enabled;
#endif
}
void WorkTimelineScope::SetMesh(uint32_t mesh, bool miss) {
  if (!begin_us_) return;
  asset_.mesh = mesh;
  asset_.miss = miss;
}
void WorkTimelineScope::SetAsset(uint32_t mesh, uint64_t fingerprint, uint64_t diffuse_guid,
                                 uint32_t vb_bytes, uint32_t ib_count,
                                 const float* bbox_min, const float* bbox_max) {
  if (!begin_us_) return;
  asset_.mesh = mesh; asset_.fingerprint = fingerprint; asset_.diffuse_guid = diffuse_guid;
  asset_.vb_bytes = vb_bytes; asset_.ib_count = ib_count; asset_.valid = true;
  std::copy_n(bbox_min, 3, asset_.bbox_min);
  std::copy_n(bbox_max, 3, asset_.bbox_max);
}
WorkTimelineScope::~WorkTimelineScope() {
  if (!begin_us_) return;
  const uint64_t end_cycles = ThreadCycles();
  const uint64_t cycles = begin_cycles_ != 0 && end_cycles >= begin_cycles_
      ? end_cycles - begin_cycles_ : UINT64_MAX;
  const int64_t end = NowUs();
  // Keep coarse work, not a high-volume history of tiny dispatcher calls.
  if (end - begin_us_ < 50) return;
  auto& lane = g_work[lane_];
  lane.records.Push({press_us_, begin_us_, end, cycles,
                    lane.owner.load(std::memory_order_relaxed), phase_, asset_});
}

uint64_t BeginGuestEvent() {
  if (!REXCVAR_GET(skate3_perf_recorder)) return 0;
  g_current_callbacks = {};
  g_event_active = true;
  g_event_detail = REXCVAR_GET(skate3_perf_event_detail);
  return uint64_t(NowUs());
}

void EndGuestEvent(uint64_t begin_us, uint32_t type, uint32_t token, uint32_t caller) {
  if (begin_us == 0) return;
  const uint64_t elapsed = uint64_t(NowUs()) - begin_us;
  g_event_active = false;
  g_guest_events.total_us += elapsed;
  ++g_guest_events.calls;
  if (elapsed > g_guest_events.max_us) {
    g_guest_events.max_us = elapsed;
    g_guest_events.type = type;
    g_guest_events.token = token;
    g_guest_events.caller = caller;
    g_guest_events.callbacks = g_current_callbacks;
  }
}

GuestEventCallbackScope::GuestEventCallbackScope(uint32_t function, uint32_t subscriber)
    : function_(function), subscriber_(subscriber) {
  if (!g_event_active) return;
  entered_ = true;
  if (g_callback_depth++ == 0) {
    g_callback_target = function_;
    g_callback_forward_pending = function_ == 0x823D0F58;
    begin_us_ = uint64_t(NowUs());
  }
}

GuestEventCallbackScope::~GuestEventCallbackScope() {
  if (!entered_) return;
  --g_callback_depth;
  if (begin_us_ == 0) return;
  const uint64_t elapsed = uint64_t(NowUs()) - begin_us_;
  g_current_callbacks.total_us += elapsed;
  ++g_current_callbacks.calls;
  if (elapsed > g_current_callbacks.max_us) {
    g_current_callbacks.max_us = elapsed;
    g_current_callbacks.function = g_callback_target;
    g_current_callbacks.subscriber = subscriber_;
  }
}

void NoteGuestEventCallbackTarget(uint32_t function) {
  if (g_event_active && g_callback_depth == 1 && g_callback_forward_pending) {
    g_callback_target = function;
    g_callback_forward_pending = false;
  }
}

void NoteIndexedTextureLookup(uint32_t scanned_nodes) {
  if (!g_event_active || !g_event_detail) return;
  ++g_current_callbacks.lookup_index_calls;
  g_current_callbacks.lookup_scan_nodes += scanned_nodes;
}

bool RegistrationDetailEnabled() {
  return g_event_active && g_event_detail && g_registration_depth != 0;
}
void NoteRegistrationMeshes(uint32_t meshes) {
  if (RegistrationDetailEnabled()) g_current_callbacks.registration_meshes += meshes;
}
void NoteFastContextResourceSearch(uint32_t scanned_entries) {
  if (!RegistrationDetailEnabled()) return;
  ++g_current_callbacks.context_resource_fast_calls;
  g_current_callbacks.context_resource_scan_entries += scanned_entries;
}

namespace {
bool HomeSceneCountersActive(bool nsv) {
  if (!nsv || !REXCVAR_GET(skate3_perf_recorder) ||
      !REXCVAR_GET(skate3_perf_profile_on_home)) return false;
  const int64_t press = g_manual_press_us.load(std::memory_order_acquire);
  static thread_local int64_t expired_press = 0;
  if (!press || press == expired_press) return false;
  const int64_t now = NowUs();
  if (now < press) return false;
  if (now - press >= kManualCaptureUs) {
    expired_press = press;
    return false;
  }
  return true;
}
thread_local bool g_scene_sample_timings = false;
}  // namespace

bool SceneCountersActive() { return g_scene_active; }
void NoteSceneWarmState(uint64_t started, uint64_t published, uint64_t unready,
                        uint64_t rejected, uint32_t queued, uint32_t pending) {
  if (!g_scene_active) return;
  g_scene_current.warm_started_total = started;
  g_scene_current.warm_published_total = published;
  g_scene_current.warm_unready_total = unready;
  g_scene_current.warm_rejected_total = rejected;
  g_scene_current.warm_queue = queued;
  g_scene_current.warm_pending = pending;
}

bool SceneDetailEnabled(bool nsv) {
  return nsv && REXCVAR_GET(skate3_perf_recorder) && REXCVAR_GET(skate3_perf_scene_detail);
}
GuestSceneScope::GuestSceneScope(bool nsv, uint64_t capture_ns, uint32_t records) {
  if (g_scene_active) return;
  g_scene_ready = {};
  const bool detail = SceneDetailEnabled(nsv);
  if (!detail && !HomeSceneCountersActive(nsv)) return;
  g_scene_sample_timings = detail;
  entered_ = true; g_scene_active = true; g_scene_current = {};
  ++g_scene_sample_frame;
  begin_read_faults_ = skate3::native_scene::GuestCopyFaultCountForThread();
  g_scene_current.capture_us = capture_ns / 1000;
  g_scene_current.records = records; g_scene_current.valid = true;
}
GuestSceneScope::~GuestSceneScope() {
  if (!entered_) return;
  g_scene_current.read_faults = uint32_t(
      skate3::native_scene::GuestCopyFaultCountForThread() - begin_read_faults_);
  g_scene_active = false; g_scene_sample_timings = false;
  g_scene_ready = g_scene_current;
}
GuestSceneCacheScope::GuestSceneCacheScope(SceneCacheWork work) : work_(work) {
  if (!g_scene_active || !g_scene_sample_timings) return;
  const uint32_t ordinal = work == SceneCacheWork::kWalk ? g_scene_current.core_walks
      : (work == SceneCacheWork::kFingerprint ? g_scene_current.fingerprints : 0);
  if (ordinal == 0 || ((ordinal + uint32_t(g_scene_sample_frame * 17)) & 63u) != 0) return;
  begin_us_ = uint64_t(NowUs());
}
GuestSceneCacheScope::~GuestSceneCacheScope() {
  if (begin_us_ == 0) return;
  const uint64_t elapsed = uint64_t(NowUs()) - begin_us_;
  if (work_ == SceneCacheWork::kWalk) {
    ++g_scene_current.walk_sample_calls; g_scene_current.walk_sample_us += elapsed;
  } else if (work_ == SceneCacheWork::kFingerprint) {
    ++g_scene_current.fp_sample_calls; g_scene_current.fp_sample_us += elapsed;
  }
}
GuestSceneGeometryScope::GuestSceneGeometryScope() {
  if (!g_scene_active) return;
  const uint32_t ordinal = ++g_scene_current.geometry_calls;
  if (!g_scene_sample_timings) return;
  if (((ordinal + uint32_t(g_scene_sample_frame * 17)) & 63u) != 0) return;
  begin_us_ = uint64_t(NowUs());
}
GuestSceneGeometryScope::~GuestSceneGeometryScope() {
  if (begin_us_ == 0) return;
  EndCache();
  ++g_scene_current.geometry_sample_calls;
  g_scene_current.geometry_sample_us += uint64_t(NowUs()) - begin_us_;
}
void GuestSceneGeometryScope::BeginCache() {
  if (begin_us_ != 0) cache_begin_us_ = uint64_t(NowUs());
}
void GuestSceneGeometryScope::EndCache() {
  if (cache_begin_us_ == 0) return;
  g_scene_current.cache_sample_us += uint64_t(NowUs()) - cache_begin_us_;
  cache_begin_us_ = 0;
}
void NoteSceneItemCapacity(uint64_t capacity, uint64_t reserved_bytes, uint64_t reserve_ns) {
  if (!g_scene_active) return;
  g_scene_current.item_capacity = capacity;
  if (reserved_bytes != 0) {
    ++g_scene_current.reserve_grows;
    g_scene_current.reserve_bytes += reserved_bytes;
    g_scene_current.reserve_us += reserve_ns / 1000;
  }
}
void NoteSceneCacheWork(SceneCacheWork work) {
  if (!g_scene_active) return;
  switch (work) {
    case SceneCacheWork::kHit: ++g_scene_current.core_hits; break;
    case SceneCacheWork::kWalk: ++g_scene_current.core_walks; break;
    case SceneCacheWork::kFingerprint: ++g_scene_current.fingerprints; break;
    case SceneCacheWork::kRebind: ++g_scene_current.rebinds; break;
    case SceneCacheWork::kMissAbsent: ++g_scene_current.miss_absent; break;
    case SceneCacheWork::kMissRefresh: ++g_scene_current.miss_refresh; break;
    case SceneCacheWork::kMissPending: ++g_scene_current.miss_pending; break;
    case SceneCacheWork::kMissInvalid: ++g_scene_current.miss_invalid; break;
    case SceneCacheWork::kWarmReuse: ++g_scene_current.warm_reuse; break;
  }
}
GuestScenePhaseScope::GuestScenePhaseScope(SceneBuildPhase phase) : phase_(phase) {
  if (g_scene_active) begin_us_ = uint64_t(NowUs());
}
GuestScenePhaseScope::~GuestScenePhaseScope() {
  End();
}
void GuestScenePhaseScope::End() {
  if (begin_us_ == 0) return;
  const auto elapsed = uint64_t(NowUs()) - begin_us_;
  begin_us_ = 0;
  switch (phase_) {
    case SceneBuildPhase::kWorld: g_scene_current.world_us += elapsed; break;
    case SceneBuildPhase::kMerge: g_scene_current.merge_us += elapsed; break;
    case SceneBuildPhase::kRetain: g_scene_current.retain_us += elapsed; break;
  }
}

GuestEventWorkScope::GuestEventWorkScope(GuestEventWork work, uint32_t target)
    : work_(work), target_(target) {
  if (g_event_active && work == GuestEventWork::kRenderInstance) {
    registration_entered_ = true; ++g_registration_depth;
  }
  if (g_event_active && g_event_detail && work == GuestEventWork::kPrimitiveSetup) {
    primitive_entered_ = true;
    if (++g_primitive_depth == 1) begin_cycles_ = ThreadCycles();
  }
  const bool registration_phase = work >= GuestEventWork::kInstanceContexts &&
                                  work <= GuestEventWork::kContextResources;
  const bool primitive_phase = work >= GuestEventWork::kPrimitiveDispatch;
  if (g_event_active && (work <= GuestEventWork::kNativePrewarm || g_event_detail) &&
      (!registration_phase || g_registration_depth != 0) &&
      (!primitive_phase || g_primitive_depth != 0)) {
    begin_us_ = uint64_t(NowUs());
  }
}

GuestEventWorkScope::~GuestEventWorkScope() {
  if (registration_entered_) --g_registration_depth;
  if (primitive_entered_) --g_primitive_depth;
  if (begin_cycles_ != 0) {
    const uint64_t end_cycles = ThreadCycles();
    if (end_cycles >= begin_cycles_) g_current_callbacks.setup_cpu_cycles += end_cycles - begin_cycles_;
  }
  if (begin_us_ == 0) return;
  const uint64_t elapsed = uint64_t(NowUs()) - begin_us_;
  if (work_ == GuestEventWork::kRenderInstance) {
    g_current_callbacks.registration_guest_us += elapsed;
    ++g_current_callbacks.registration_calls;
  } else if (work_ == GuestEventWork::kNativePrewarm) {
    g_current_callbacks.registration_host_us += elapsed;
  } else if (work_ == GuestEventWork::kResourcePresentation) {
    g_current_callbacks.resource_us += elapsed;
    ++g_current_callbacks.resource_calls;
  } else if (work_ == GuestEventWork::kPrimitiveSetup) {
    g_current_callbacks.setup_us += elapsed;
    ++g_current_callbacks.setup_calls;
  } else if (work_ == GuestEventWork::kPrimitiveDispatch) {
    g_current_callbacks.primitive_dispatch_us += elapsed; ++g_current_callbacks.primitive_dispatch_calls;
    if (elapsed > g_current_callbacks.primitive_dispatch_max_us) {
      g_current_callbacks.primitive_dispatch_max_us = elapsed;
      g_current_callbacks.primitive_dispatch_function = target_;
    }
  } else if (work_ == GuestEventWork::kPrimitiveClear) {
    g_current_callbacks.primitive_clear_us += elapsed; ++g_current_callbacks.primitive_clear_calls;
  } else if (work_ == GuestEventWork::kPrimitiveCollect) {
    g_current_callbacks.primitive_collect_us += elapsed; ++g_current_callbacks.primitive_collect_calls;
  } else if (work_ == GuestEventWork::kPrimitiveInsert) {
    g_current_callbacks.primitive_insert_us += elapsed; ++g_current_callbacks.primitive_insert_calls;
  } else if (work_ == GuestEventWork::kPrimitiveCreate) {
    g_current_callbacks.primitive_create_us += elapsed; ++g_current_callbacks.primitive_create_calls;
  } else if (work_ == GuestEventWork::kPrimitiveRelease) {
    g_current_callbacks.primitive_release_us += elapsed; ++g_current_callbacks.primitive_release_calls;
  } else if (work_ == GuestEventWork::kPrimitiveRequest) {
    g_current_callbacks.primitive_request_us += elapsed; ++g_current_callbacks.primitive_request_calls;
  } else if (work_ == GuestEventWork::kGuidLookup) {
    g_current_callbacks.lookup_us += elapsed;
    ++g_current_callbacks.lookup_calls;
  } else if (work_ == GuestEventWork::kInstanceContexts) {
    g_current_callbacks.contexts_us += elapsed; ++g_current_callbacks.contexts_calls;
  } else if (work_ == GuestEventWork::kInstanceParameters) {
    g_current_callbacks.parameters_us += elapsed; ++g_current_callbacks.parameters_calls;
  } else if (work_ == GuestEventWork::kInstanceRegistry) {
    g_current_callbacks.registry_us += elapsed; ++g_current_callbacks.registry_calls;
  } else if (work_ == GuestEventWork::kInstanceAllocation) {
    g_current_callbacks.allocation_us += elapsed; ++g_current_callbacks.allocation_calls;
  } else if (work_ == GuestEventWork::kContextMaterials) {
    g_current_callbacks.context_materials_us += elapsed; ++g_current_callbacks.context_materials_calls;
  } else if (work_ == GuestEventWork::kContextFactory) {
    g_current_callbacks.context_factory_us += elapsed; ++g_current_callbacks.context_factory_calls;
    if (elapsed > g_current_callbacks.context_factory_max_us) {
      g_current_callbacks.context_factory_max_us = elapsed;
      g_current_callbacks.context_factory_key = target_;
    }
  } else if (work_ == GuestEventWork::kContextBindings) {
    g_current_callbacks.context_bindings_us += elapsed; ++g_current_callbacks.context_bindings_calls;
  } else if (work_ == GuestEventWork::kContextResources) {
    g_current_callbacks.context_resources_us += elapsed; ++g_current_callbacks.context_resources_calls;
  }
}

GuestSwapScope::GuestSwapScope(bool native) {
  if (!native || !REXCVAR_GET(skate3_perf_recorder) ||
      !REXCVAR_GET(skate3_perf_profile_on_home)) return;
  const int64_t press = g_manual_press_us.load(std::memory_order_acquire);
  static thread_local int64_t expired_press = 0;
  if (!press || press == expired_press ||
      !skate3::world_loading::IsSkate2Gameplay()) return;
  const int64_t now = NowUs();
  if (now < press) return;
  if (now - press >= kManualCaptureUs) {
    expired_press = press;
    return;
  }
  begin_us_ = now;
  begin_cycles_ = ThreadCycles();
}

GuestSwapScope::~GuestSwapScope() {
  if (!begin_us_) return;
  const uint64_t cycles = ThreadCycles();
  g_swaps.Push({NowUs(), begin_us_, cycles - begin_cycles_});
}

void OnGuestFrame(uint64_t pace_ns, uint64_t build_ns, uint32_t meshes,
                  float smooth_err_ms, float smooth_period_ms, const float cam[3]) {
  const GuestEvents events = g_guest_events;
  g_guest_events = {};
  const SceneBuildDetail scene = g_scene_ready;
  g_scene_ready = {};
  if (!REXCVAR_GET(skate3_perf_recorder)) {
    return;
  }
  if (!g_started.exchange(true, std::memory_order_acq_rel)) {
    StartSampler();
  }
  static int64_t s_prev_us = 0;
  static uint64_t s_prev_cycles = 0;
  GuestRec r;
  r.events = events;
  r.scene = scene;
  r.t_us = NowUs();
  const uint64_t cycles = ThreadCycles();
  r.dt_us = s_prev_us != 0 ? uint32_t(std::min<int64_t>(r.t_us - s_prev_us, 0xFFFFFFFF)) : 0;
  r.cpu_cycles = s_prev_cycles != 0 ? cycles - s_prev_cycles : 0;
  s_prev_us = r.t_us;
  s_prev_cycles = cycles;
  r.pace_us = uint32_t(pace_ns / 1000);
  r.build_us = uint32_t(build_ns / 1000);
  r.meshes = meshes;
  r.smooth_err_ms = smooth_err_ms;
  r.smooth_period_ms = smooth_period_ms;
  std::memcpy(r.cam, cam, sizeof(r.cam));
  g_guest.Push(r);
  g_last_guest_us.store(r.t_us, std::memory_order_relaxed);
}

void OnRenderFrame(const RenderRecord& rec) {
  if (!REXCVAR_GET(skate3_perf_recorder)) {
    return;
  }
  static int64_t s_prev_us = 0;
  RenderRec r;
  r.t_us = NowUs();
  r.dt_us = s_prev_us != 0 ? uint32_t(std::min<int64_t>(r.t_us - s_prev_us, 0xFFFFFFFF)) : 0;
  s_prev_us = r.t_us;
  r.r = rec;
  g_render.Push(r);
  if (rec.complete) {
    g_last_native_us.store(r.t_us, std::memory_order_relaxed);
  }
}

void Count(Activity a, uint32_t amount) {
  g_activity[size_t(a)].fetch_add(amount, std::memory_order_relaxed);
}

void SetRenderPaused(bool paused) {
  g_render_paused.store(paused, std::memory_order_relaxed);
}

void RequestCapture() {
  const int64_t now = NowUs();
  g_manual_press_us.store(now, std::memory_order_release);
  REXLOG_WARN("perf-capture: capture requested (Home); writing in ~6 s");
}

void SetPresenter(rex::ui::Presenter* presenter) {
  if (!presenter) skate3::freeze_capture::Shutdown();
  std::lock_guard<std::mutex> lock(PresenterMutex());
  g_presenter = presenter;
}

namespace {

#if defined(_WIN32)

// ---- NVML (NVIDIA driver library, loaded dynamically; absent = skipped) ----
struct Nvml {
  struct Util {
    unsigned int gpu;
    unsigned int memory;
  };
  struct Mem {
    unsigned long long total;
    unsigned long long free;
    unsigned long long used;
  };
  using Dev = void*;
  int (*init)() = nullptr;
  int (*get_handle)(unsigned int, Dev*) = nullptr;
  int (*get_name)(Dev, char*, unsigned int) = nullptr;
  int (*get_util)(Dev, Util*) = nullptr;
  int (*get_mem)(Dev, Mem*) = nullptr;
  int (*get_clock)(Dev, int, unsigned int*) = nullptr;
  int (*get_throttle)(Dev, unsigned long long*) = nullptr;
  int (*get_pstate)(Dev, int*) = nullptr;
  Dev dev = nullptr;

  bool Load() {
    HMODULE lib = LoadLibraryW(L"nvml.dll");
    if (lib == nullptr) {
      lib = LoadLibraryW(L"C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvml.dll");
    }
    if (lib == nullptr) {
      return false;
    }
    const auto sym = [&](const char* name) { return GetProcAddress(lib, name); };
    init = reinterpret_cast<int (*)()>(sym("nvmlInit_v2"));
    get_handle = reinterpret_cast<int (*)(unsigned int, Dev*)>(
        sym("nvmlDeviceGetHandleByIndex_v2"));
    get_name = reinterpret_cast<int (*)(Dev, char*, unsigned int)>(sym("nvmlDeviceGetName"));
    get_util = reinterpret_cast<int (*)(Dev, Util*)>(sym("nvmlDeviceGetUtilizationRates"));
    get_mem = reinterpret_cast<int (*)(Dev, Mem*)>(sym("nvmlDeviceGetMemoryInfo"));
    get_clock = reinterpret_cast<int (*)(Dev, int, unsigned int*)>(sym("nvmlDeviceGetClockInfo"));
    get_throttle = reinterpret_cast<int (*)(Dev, unsigned long long*)>(
        sym("nvmlDeviceGetCurrentClocksThrottleReasons"));
    get_pstate = reinterpret_cast<int (*)(Dev, int*)>(sym("nvmlDeviceGetPerformanceState"));
    if (init == nullptr || get_handle == nullptr || init() != 0 ||
        get_handle(0, &dev) != 0) {
      dev = nullptr;
      return false;
    }
    char name[96] = {};
    if (get_name != nullptr && get_name(dev, name, sizeof(name)) == 0) {
      REXLOG_INFO("perf-capture: NVML GPU 0 = {}", name);
    }
    return true;
  }

  void Sample(TickRec& t) const {
    if (dev == nullptr) {
      return;
    }
    Util u{};
    if (get_util != nullptr && get_util(dev, &u) == 0) {
      t.gpu_util = int16_t(u.gpu);
      t.gpu_mem_util = int16_t(u.memory);
    }
    Mem m{};
    if (get_mem != nullptr && get_mem(dev, &m) == 0) {
      t.vram_used_mb = float(double(m.used) / 1048576.0);
      t.vram_total_mb = float(double(m.total) / 1048576.0);
    }
    unsigned int v = 0;
    if (get_clock != nullptr && get_clock(dev, 0, &v) == 0) {
      t.gfx_mhz = int16_t(v);
    }
    if (get_clock != nullptr && get_clock(dev, 2, &v) == 0) {
      t.mem_mhz = int16_t(v);
    }
    unsigned long long reasons = 0;
    if (get_throttle != nullptr && get_throttle(dev, &reasons) == 0) {
      t.throttle = reasons;
    }
    int ps = -1;
    if (get_pstate != nullptr && get_pstate(dev, &ps) == 0) {
      t.pstate = int16_t(ps);
    }
  }
};

// ---- DXGI: this process's local video memory use vs its budget ----
struct DxgiVram {
  IDXGIAdapter3* adapter = nullptr;

  bool Load() {
    HMODULE lib = LoadLibraryW(L"dxgi.dll");
    if (lib == nullptr) {
      return false;
    }
    using CreateFn = HRESULT(WINAPI*)(REFIID, void**);
    const auto create = reinterpret_cast<CreateFn>(GetProcAddress(lib, "CreateDXGIFactory1"));
    IDXGIFactory1* factory = nullptr;
    if (create == nullptr ||
        FAILED(create(__uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory)))) {
      return false;
    }
    IDXGIAdapter1* best = nullptr;
    SIZE_T best_mem = 0;
    IDXGIAdapter1* a = nullptr;
    for (UINT i = 0; factory->EnumAdapters1(i, &a) != DXGI_ERROR_NOT_FOUND; ++i) {
      DXGI_ADAPTER_DESC1 d{};
      if (SUCCEEDED(a->GetDesc1(&d)) && (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) == 0 &&
          d.DedicatedVideoMemory > best_mem) {
        if (best != nullptr) {
          best->Release();
        }
        best = a;
        best_mem = d.DedicatedVideoMemory;
      } else {
        a->Release();
      }
    }
    factory->Release();
    if (best == nullptr) {
      return false;
    }
    const HRESULT hr =
        best->QueryInterface(__uuidof(IDXGIAdapter3), reinterpret_cast<void**>(&adapter));
    best->Release();
    return SUCCEEDED(hr) && adapter != nullptr;
  }

  void Sample(TickRec& t) const {
    if (adapter == nullptr) {
      return;
    }
    DXGI_QUERY_VIDEO_MEMORY_INFO info{};
    if (SUCCEEDED(adapter->QueryVideoMemoryInfo(0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &info))) {
      t.proc_vram_mb = float(double(info.CurrentUsage) / 1048576.0);
      t.proc_vram_budget_mb = float(double(info.Budget) / 1048576.0);
    }
  }
};

// ---- Every thread of this process: handle, name, last cycle count ----
struct ThreadTable {
  struct Entry {
    HANDLE handle = nullptr;
    uint64_t last_cycles = 0, last_kernel = 0, last_user = 0;
    bool times_valid = false, cycles_valid = false;
    bool seen = false;
  };
  std::unordered_map<DWORD, Entry> threads;

  void Refresh() {
    using GetDescFn = HRESULT(WINAPI*)(HANDLE, PWSTR*);
    static const auto get_desc = reinterpret_cast<GetDescFn>(
        GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "GetThreadDescription"));
    const DWORD pid = GetCurrentProcessId();
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) {
      return;
    }
    for (auto& [tid, e] : threads) {
      e.seen = false;
    }
    THREADENTRY32 te{};
    te.dwSize = sizeof(te);
    for (BOOL ok = Thread32First(snap, &te); ok; ok = Thread32Next(snap, &te)) {
      if (te.th32OwnerProcessID != pid) {
        continue;
      }
      auto it = threads.find(te.th32ThreadID);
      if (it == threads.end()) {
        HANDLE h = OpenThread(THREAD_QUERY_LIMITED_INFORMATION, FALSE, te.th32ThreadID);
        if (h == nullptr) {
          continue;
        }
        Entry e;
        e.handle = h;
        ULONG64 c = 0;
        e.cycles_valid = QueryThreadCycleTime(h, &c) != 0;
        e.last_cycles = c;
        it = threads.emplace(te.th32ThreadID, e).first;
      }
      it->second.seen = true;
      if (get_desc != nullptr) {
        PWSTR desc = nullptr;
        if (SUCCEEDED(get_desc(it->second.handle, &desc)) && desc != nullptr) {
          char name[160] = {};
          WideCharToMultiByte(CP_UTF8, 0, desc, -1, name, sizeof(name) - 1, nullptr, nullptr);
          LocalFree(desc);
          if (name[0] != '\0') {
            std::lock_guard<std::mutex> lock(NamesMutex());
            Names()[te.th32ThreadID] = name;
          }
        }
      }
    }
    CloseHandle(snap);
    for (auto it = threads.begin(); it != threads.end();) {
      if (!it->second.seen) {
        CloseHandle(it->second.handle);
        it = threads.erase(it);
      } else {
        ++it;
      }
    }
  }

  static uint64_t TimeValue(const FILETIME& t) {
    return (uint64_t(t.dwHighDateTime) << 32) | t.dwLowDateTime;
  }
  // A Home-only gap must not be charged to the next 100ms CPU sample.
  void Prime() {
    for (auto& [tid, e] : threads) {
      ULONG64 cycles = 0;
      e.cycles_valid = QueryThreadCycleTime(e.handle, &cycles) != 0;
      e.last_cycles = cycles;
      FILETIME created{}, exited{}, kernel{}, user{};
      e.times_valid = GetThreadTimes(e.handle, &created, &exited, &kernel, &user) != 0;
      if (e.times_valid) { e.last_kernel = TimeValue(kernel); e.last_user = TimeValue(user); }
    }
  }

  // Per-thread CPU microseconds since the previous call, heaviest first.
  void Sample(TickRec& t, double cycles_per_us) {
    std::vector<ThreadSample> all;
    all.reserve(threads.size());
    for (auto& [tid, e] : threads) {
      ULONG64 c = 0;
      if (!QueryThreadCycleTime(e.handle, &c)) {
        e.cycles_valid = false; e.times_valid = false;
        continue;
      }
      const uint64_t delta = e.cycles_valid && c >= e.last_cycles ? c - e.last_cycles : 0;
      e.last_cycles = c;
      e.cycles_valid = true;
      if (cycles_per_us <= 0.0) {
        continue;
      }
      const double us = double(delta) / cycles_per_us;
      uint32_t kernel_us = UINT32_MAX, user_us = UINT32_MAX;
      FILETIME created{}, exited{}, kernel{}, user{};
      const bool times_ok = GetThreadTimes(e.handle, &created, &exited, &kernel, &user) != 0;
      if (times_ok) {
        const uint64_t k = TimeValue(kernel), u = TimeValue(user);
        if (e.times_valid && k >= e.last_kernel && u >= e.last_user) {
          kernel_us = uint32_t(std::min<uint64_t>((k - e.last_kernel) / 10, 4000000000ULL));
          user_us = uint32_t(std::min<uint64_t>((u - e.last_user) / 10, 4000000000ULL));
        }
        e.last_kernel = k; e.last_user = u;
      }
      e.times_valid = times_ok;
      if (us >= 50.0) {
        all.push_back({uint32_t(tid), uint32_t(std::min(us, 4.0e9)), kernel_us, user_us});
      }
    }
    std::sort(all.begin(), all.end(), [](const ThreadSample& a, const ThreadSample& b) {
      return a.cpu_us > b.cpu_us;
    });
    t.nthreads = uint8_t(std::min<size_t>(all.size(), kMaxThreadSamples));
    for (int i = 0; i < t.nthreads; ++i) {
      t.threads[i] = all[size_t(i)];
    }
  }
};

uint64_t FileTimeU64(const FILETIME& f) {
  return (uint64_t(f.dwHighDateTime) << 32) | f.dwLowDateTime;
}

// ---- Stack-sampling profiler ----
// Suspends a target thread, unwinds up to kProfFrames frames from its
// context (x64 unwind data), resumes it. Nothing here allocates or locks
// while the target is suspended. Every memory read that can fault goes
// through a call the SEH scope covers: the stack bounds and leaf return
// addresses via GuestTryCopy, the unwinder through a volatile function
// pointer in this noinline function (see the compiler-trap notes at
// GuestTryCopy: open-coded faulting loads or inlining drop the scope).
using LookupFn = PRUNTIME_FUNCTION(NTAPI*)(DWORD64, PDWORD64, PUNWIND_HISTORY_TABLE);
using UnwindFn = PEXCEPTION_ROUTINE(NTAPI*)(DWORD, DWORD64, DWORD64, PRUNTIME_FUNCTION,
                                            PCONTEXT, PVOID*, PDWORD64,
                                            PKNONVOLATILE_CONTEXT_POINTERS);
volatile LookupFn g_lookup_fn = RtlLookupFunctionEntry;
volatile UnwindFn g_unwind_fn = RtlVirtualUnwind;

__declspec(noinline) int WalkSuspended(HANDLE thread, const uint8_t* teb, uint64_t* pcs) {
  CONTEXT ctx{};
  ctx.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
  if (!GetThreadContext(thread, &ctx)) {
    return 0;
  }
  // NT_TIB: StackBase at +8, StackLimit at +16.
  uint64_t bounds[2] = {};
  if (!skate3::native_scene::GuestTryCopy(bounds, teb + 8, sizeof(bounds))) {
    return 0;
  }
  const uint64_t stack_base = bounds[0], stack_limit = bounds[1];
  int n = 0;
  pcs[n++] = ctx.Rip;
  __try {
    while (n < kProfFrames) {
      DWORD64 image_base = 0;
      PRUNTIME_FUNCTION fn = g_lookup_fn(ctx.Rip, &image_base, nullptr);
      if (fn == nullptr) {
        // Leaf function: the return address sits at [rsp].
        if (ctx.Rsp < stack_limit || ctx.Rsp + 8 > stack_base) {
          break;
        }
        uint64_t ret = 0;
        if (!skate3::native_scene::GuestTryCopy(&ret, reinterpret_cast<const void*>(ctx.Rsp),
                                                sizeof(ret))) {
          break;
        }
        ctx.Rip = ret;
        ctx.Rsp += 8;
      } else {
        PVOID handler_data = nullptr;
        DWORD64 establisher = 0;
        g_unwind_fn(UNW_FLAG_NHANDLER, image_base, ctx.Rip, fn, &ctx, &handler_data,
                    &establisher, nullptr);
      }
      if (ctx.Rip == 0 || ctx.Rsp < stack_limit || ctx.Rsp >= stack_base) {
        break;
      }
      pcs[n++] = ctx.Rip;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  return n;
}

void ProfilerLoop() {
  NameCurrentThread("Perf profiler");
  // Above the sampled threads so samples land on a steady ~1 ms grid.
  SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
  struct Target {
    DWORD tid = 0;
    HANDLE handle = nullptr;
    const uint8_t* teb = nullptr;
  };
  Target targets[std::size(kProfThreads)];
  struct Tbi {
    LONG exit_status;
    PVOID teb;
    HANDLE pid;
    HANDLE tid;
    ULONG_PTR affinity;
    LONG priority;
    LONG base_priority;
  };
  using QueryFn = LONG(NTAPI*)(HANDLE, int, PVOID, ULONG, PULONG);
  const auto query = reinterpret_cast<QueryFn>(
      GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQueryInformationThread"));
  int64_t next_resolve_us = 0;
  uint64_t tick = 0;
  while (true) {
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
    const int64_t now = NowUs();
    if (!REXCVAR_GET(skate3_perf_recorder) ||
        (!REXCVAR_GET(skate3_perf_profiler) && !ManualProfileActive(now))) {
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
      continue;
    }
    if (now >= next_resolve_us) {
      // Targets by thread name (filled in by the sampler's thread table).
      next_resolve_us = now + 2'000'000;
      for (size_t k = 0; k < std::size(kProfThreads); ++k) {
        DWORD found = 0;
        {
          std::lock_guard<std::mutex> lock(NamesMutex());
          for (const auto& [tid, name] : Names()) {
            if (name.find(kProfThreads[k]) != std::string::npos) {
              found = tid;
              break;
            }
          }
        }
        if (found == 0 || found == targets[k].tid) {
          continue;
        }
        if (targets[k].handle != nullptr) {
          CloseHandle(targets[k].handle);
          targets[k] = Target{};
        }
        HANDLE h = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                                  THREAD_QUERY_INFORMATION,
                              FALSE, found);
        Tbi tbi{};
        if (h != nullptr && query != nullptr &&
            query(h, 0 /*ThreadBasicInformation*/, &tbi, sizeof(tbi), nullptr) == 0 &&
            tbi.teb != nullptr) {
          targets[k] = Target{found, h, static_cast<const uint8_t*>(tbi.teb)};
        } else if (h != nullptr) {
          CloseHandle(h);
        }
      }
    }
    if (targets[0].handle == nullptr) next_resolve_us = 0;
    ++tick;
    for (size_t k = 0; k < std::size(kProfThreads); ++k) {
      // render_thread every ms, the others every 4th.
      if (targets[k].handle == nullptr || (k != 0 && (tick & 3u) != 0)) {
        continue;
      }
      StackSample s;
      s.t_us = NowUs();
      s.since_swap_us = s.t_us - g_last_guest_us.load(std::memory_order_relaxed);
      s.thread = uint8_t(k);
      if (SuspendThread(targets[k].handle) == DWORD(-1)) {
        CloseHandle(targets[k].handle);
        targets[k] = Target{};  // thread gone; re-resolved by name later
        continue;
      }
      s.n = uint8_t(WalkSuspended(targets[k].handle, targets[k].teb, s.pcs));
      ResumeThread(targets[k].handle);
      if (s.n > 0) {
        g_prof.Push(s);
      }
    }
  }
}

// ---- Capture writer ----
struct Window {
  int64_t from_us;
  int64_t to_us;
};

std::string ThreadName(uint32_t tid) {
  std::lock_guard<std::mutex> lock(NamesMutex());
  const auto& names = Names();
  const auto it = names.find(tid);
  return it != names.end() ? it->second : std::string("thread ") + std::to_string(tid);
}

void WriteCapture(const char* kind, int64_t t0_us, Window during, Window base, float ref_fps,
                  float min_fps) {
  static int s_count = 0;
  ++s_count;
  const double cycles_per_us = g_cycles_per_us.load(std::memory_order_relaxed);
  const auto cyc_ms = [&](uint64_t c) {
    return cycles_per_us > 0.0 ? double(c) / cycles_per_us / 1000.0 : 0.0;
  };
  const std::vector<GuestRec> guest = g_guest.Snapshot();
  const std::vector<RenderRec> render = g_render.Snapshot();
  const std::vector<TickRec> ticks = g_ticks.Snapshot();
  const int64_t from = t0_us - 30'000'000;
  std::error_code ec;
  std::filesystem::create_directories("logs", ec);
  char stem[96];
  std::snprintf(stem, sizeof(stem), "logs/perf_%d_%lld", s_count,
                static_cast<long long>(std::time(nullptr)));
  const auto rel_ms = [&](int64_t t) { return double(t - t0_us) / 1000.0; };

  {
    std::ofstream f(std::string(stem) + "_guest.csv");
    f << "t_ms,dt_ms,pace_ms,work_ms,guest_thread_cpu_ms,build_ms,meshes,smooth_err_ms,"
         "smooth_period_ms,cam_x,cam_y,cam_z,event_ms,event_calls,event_max_ms,"
         "event_type,event_token,event_caller,event_callback_ms,event_callback_calls,"
         "event_callback_max_ms,event_callback_function,event_callback_subscriber,"
         "event_reg_guest_ms,event_reg_host_ms,event_reg_calls,event_resource_ms,event_resource_calls,"
         "event_setup_ms,event_setup_calls,event_lookup_ms,event_lookup_calls,"
         "event_lookup_index_calls,event_lookup_scan_nodes,"
         "event_contexts_ms,event_contexts_calls,event_parameters_ms,event_parameters_calls,"
         "event_registry_ms,event_registry_calls,event_allocation_ms,event_allocation_calls,event_reg_meshes,"
         "scene_detail,capture_ms,world_build_ms,merge_ms,retain_ms,scene_records,"
         "core_hits,core_walks,fingerprints,rebinds,"
         "event_context_materials_ms,event_context_materials_calls,"
         "event_context_factory_ms,event_context_factory_calls,"
         "event_context_bindings_ms,event_context_bindings_calls,"
         "event_context_resources_ms,event_context_resources_calls,"
         "event_context_resource_fast_calls,event_context_resource_scan_entries,"
         "core_walk_sample_ms,core_walk_sample_calls,fingerprint_sample_ms,"
         "fingerprint_sample_calls,scene_read_faults,geometry_calls,geometry_sample_calls,geometry_sample_ms,cache_sample_ms,event_setup_cpu_ms,event_primitive_dispatch_ms,event_primitive_dispatch_calls,event_primitive_clear_ms,event_primitive_clear_calls,event_primitive_collect_ms,event_primitive_collect_calls,event_primitive_insert_ms,event_primitive_insert_calls,event_primitive_create_ms,event_primitive_create_calls,event_primitive_release_ms,event_primitive_release_calls,event_primitive_request_ms,event_primitive_request_calls,event_primitive_dispatch_function,scene_item_capacity,scene_reserve_grows,scene_reserve_bytes,scene_reserve_ms,event_context_factory_max_ms,event_context_factory_key,core_miss_absent,core_miss_refresh,core_miss_pending,core_miss_invalid,core_warm_reuse,warm_started_total,warm_published_total,warm_unready_total,warm_rejected_total,warm_queue,warm_pending\n";
    char line[2048];
    for (const GuestRec& r : guest) {
      if (r.t_us < from) {
        continue;
      }
      const double dt = r.dt_us / 1000.0, pace = r.pace_us / 1000.0;
      std::snprintf(line, sizeof(line),
                    "%.2f,%.3f,%.3f,%.3f,%.3f,%.3f,%u,%.2f,%.3f,%.1f,%.1f,%.1f,"
                    "%.3f,%u,%.3f,%08X,%08X,%08X,%.3f,%u,%.3f,%08X,%08X,%.3f,%.3f,%u,%.3f,%u,%.3f,%u,%.3f,%u,%u,%u,%.3f,%u,%.3f,%u,%.3f,%u,%.3f,%u,%u,%u,%.3f,%.3f,%.3f,%.3f,%u,%u,%u,%u,%u,%.3f,%u,%.3f,%u,%.3f,%u,%.3f,%u,%u,%u,%.3f,%u,%.3f,%u,%u,%u,%u,%.3f,%.3f,%.3f,%.3f,%u,%.3f,%u,%.3f,%u,%.3f,%u,%.3f,%u,%.3f,%u,%.3f,%u,%08X,%llu,%u,%llu,%.3f,%.3f,%08X,%u,%u,%u,%u,%u,%llu,%llu,%llu,%llu,%u,%u\n",
                    rel_ms(r.t_us), dt, pace, std::max(0.0, dt - pace), cyc_ms(r.cpu_cycles),
                    r.build_us / 1000.0, r.meshes, r.smooth_err_ms, r.smooth_period_ms,
                    r.cam[0], r.cam[1], r.cam[2], r.events.total_us / 1000.0,
                    r.events.calls, r.events.max_us / 1000.0, r.events.type,
                    r.events.token, r.events.caller,
                    r.events.callbacks.total_us / 1000.0, r.events.callbacks.calls,
                    r.events.callbacks.max_us / 1000.0, r.events.callbacks.function,
                    r.events.callbacks.subscriber,
                    r.events.callbacks.registration_guest_us / 1000.0,
                    r.events.callbacks.registration_host_us / 1000.0,
                    r.events.callbacks.registration_calls,
                    r.events.callbacks.resource_us / 1000.0, r.events.callbacks.resource_calls,
                    r.events.callbacks.setup_us / 1000.0, r.events.callbacks.setup_calls,
                    r.events.callbacks.lookup_us / 1000.0, r.events.callbacks.lookup_calls,
                    r.events.callbacks.lookup_index_calls, r.events.callbacks.lookup_scan_nodes,
                    r.events.callbacks.contexts_us / 1000.0, r.events.callbacks.contexts_calls,
                    r.events.callbacks.parameters_us / 1000.0, r.events.callbacks.parameters_calls,
                    r.events.callbacks.registry_us / 1000.0, r.events.callbacks.registry_calls,
                    r.events.callbacks.allocation_us / 1000.0, r.events.callbacks.allocation_calls,
                    r.events.callbacks.registration_meshes, r.scene.valid ? 1u : 0u,
                    r.scene.capture_us / 1000.0, r.scene.world_us / 1000.0,
                    r.scene.merge_us / 1000.0, r.scene.retain_us / 1000.0,
                    r.scene.records, r.scene.core_hits, r.scene.core_walks,
                    r.scene.fingerprints, r.scene.rebinds,
                    r.events.callbacks.context_materials_us / 1000.0, r.events.callbacks.context_materials_calls,
                    r.events.callbacks.context_factory_us / 1000.0, r.events.callbacks.context_factory_calls,
                    r.events.callbacks.context_bindings_us / 1000.0, r.events.callbacks.context_bindings_calls,
                    r.events.callbacks.context_resources_us / 1000.0, r.events.callbacks.context_resources_calls,
                    r.events.callbacks.context_resource_fast_calls, r.events.callbacks.context_resource_scan_entries,
                    r.scene.walk_sample_us / 1000.0, r.scene.walk_sample_calls,
                    r.scene.fp_sample_us / 1000.0, r.scene.fp_sample_calls, r.scene.read_faults,
                    r.scene.geometry_calls, r.scene.geometry_sample_calls,
                    r.scene.geometry_sample_us / 1000.0, r.scene.cache_sample_us / 1000.0,
                    cyc_ms(r.events.callbacks.setup_cpu_cycles),
                    r.events.callbacks.primitive_dispatch_us / 1000.0, r.events.callbacks.primitive_dispatch_calls,
                    r.events.callbacks.primitive_clear_us / 1000.0, r.events.callbacks.primitive_clear_calls,
                    r.events.callbacks.primitive_collect_us / 1000.0, r.events.callbacks.primitive_collect_calls,
                    r.events.callbacks.primitive_insert_us / 1000.0, r.events.callbacks.primitive_insert_calls,
                    r.events.callbacks.primitive_create_us / 1000.0, r.events.callbacks.primitive_create_calls,
                    r.events.callbacks.primitive_release_us / 1000.0, r.events.callbacks.primitive_release_calls,
                    r.events.callbacks.primitive_request_us / 1000.0, r.events.callbacks.primitive_request_calls,
                    r.events.callbacks.primitive_dispatch_function,
                    static_cast<unsigned long long>(r.scene.item_capacity), r.scene.reserve_grows,
                    static_cast<unsigned long long>(r.scene.reserve_bytes), r.scene.reserve_us / 1000.0,
                    r.events.callbacks.context_factory_max_us / 1000.0,
                    r.events.callbacks.context_factory_key, r.scene.miss_absent, r.scene.miss_refresh,
                    r.scene.miss_pending, r.scene.miss_invalid, r.scene.warm_reuse,
                    static_cast<unsigned long long>(r.scene.warm_started_total),
                    static_cast<unsigned long long>(r.scene.warm_published_total),
                    static_cast<unsigned long long>(r.scene.warm_unready_total),
                    static_cast<unsigned long long>(r.scene.warm_rejected_total), r.scene.warm_queue,
                    r.scene.warm_pending);
      f << line;
    }
  }
  {
    std::ofstream f(std::string(stem) + "_swap.csv");
    f << "t_ms,begin_ms,original_swap_ms,original_swap_cpu_ms\n";
    char line[256];
    for (const SwapRec& r : g_swaps.Snapshot()) {
      if (r.t_us < from) continue;
      std::snprintf(line, sizeof(line), "%.3f,%.3f,%.3f,%.3f\n",
                    rel_ms(r.t_us), rel_ms(r.begin_us), (r.t_us - r.begin_us) / 1000.0,
                    cyc_ms(r.cpu_cycles));
      f << line;
    }
  }
  {
    std::ofstream f(std::string(stem) + "_render.csv");
    f << "t_ms,dt_ms,total_ms,gpu_thread_cpu_ms,pre_ms,items_ms,shadow_ms,settle_ms,"
         "commit_ms,twod_ms,tail_ms,decode_ms,mesh_decodes,tex_decodes,items,draws,path,"
         "static_sun_rebuild,world_shadow_recenter,complete,"
         "sample_items,sample_completed,sample_outside_items,sample_total_ms,sample_outside_ms,"
         "sample_mesh_ms,sample_texture_ms,sample_constants_ms,sample_submit_ms,table_lookup_calls,table_lookup_hits,output_view_borrows,output_view_reuses\n";
    char line[1024];
    for (const RenderRec& rr : render) {
      if (rr.t_us < from) {
        continue;
      }
      const RenderRecord& r = rr.r;
      std::snprintf(line, sizeof(line),
                    "%.2f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%u,%u,%u,%u,"
                    "%u,%u,%u,%d,%u,%u,%u,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%u,%u,%u,%u\n",
                    rel_ms(rr.t_us), rr.dt_us / 1000.0, r.total_ns / 1e6, cyc_ms(r.cpu_cycles),
                    r.pre_ns / 1e6, r.items_ns / 1e6, r.shadow_ns / 1e6, r.settle_ns / 1e6,
                    r.commit_ns / 1e6, r.twod_ns / 1e6, r.tail_ns / 1e6, r.decode_ns / 1e6,
                    r.mesh_decodes, r.tex_decodes, r.items, r.draws, unsigned(r.path),
                    unsigned(r.nsm_rebuild), unsigned(r.ws_recenter), r.complete ? 1 : 0,
                    r.sample_items, r.sample_completed, r.sample_outside_items,
                    r.sample_total_ns / 1e6, r.sample_outside_ns / 1e6,
                    r.sample_mesh_ns / 1e6, r.sample_texture_ns / 1e6,
                    r.sample_constants_ns / 1e6, r.sample_submit_ns / 1e6,
                    r.table_lookup_calls, r.table_lookup_hits,
                    r.output_view_borrows, r.output_view_reuses);
      f << line;
    }
  }
  {
    std::ofstream f(std::string(stem) + "_ticks.csv");
    f << "t_ms,len_ms,guest_fps,render_fps,sys_cpu_pct,proc_cpu_pct,ws_mb,private_mb,"
         "faults_s,disk_read_mb_s,disk_reads_s,disk_write_mb_s,gpu_util,gpu_mem_util,"
         "gfx_mhz,mem_mhz,gpu_span_ms,gpu_wait_ms,pstate,throttle,vram_used_mb,vram_total_mb,"
         "proc_vram_mb,proc_vram_budget_mb,q_prewarm,q_miss,q_dyn,q_results,stream_load,"
         "stream_unload,stream_load_done,stream_unload_done,file_requests,decompress,"
         "render_paused,new_san_van,prewarm_coalesced\n";
    char line[640];
    for (const TickRec& t : ticks) {
      if (t.t_us < from) {
        continue;
      }
      const double sec = std::max(1e-3, t.len_us / 1e6);
      std::snprintf(
          line, sizeof(line),
          "%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.0f,%.2f,%.0f,%.2f,%d,%d,%d,%d,%.2f,%.2f,%d,"
          "0x%llX,%.0f,%.0f,%.0f,%.0f,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u\n",
          rel_ms(t.t_us), t.len_us / 1000.0, t.guest_frames / sec, t.render_frames / sec,
          t.sys_busy_pct, t.proc_cpu_pct, t.ws_mb, t.private_mb, t.faults_s, t.io_read_mb_s,
          t.io_reads_s, t.io_write_mb_s, t.gpu_util, t.gpu_mem_util, t.gfx_mhz, t.mem_mhz,
          t.gpu_span_ms, t.gpu_wait_ms, t.pstate, static_cast<unsigned long long>(t.throttle),
          t.vram_used_mb, t.vram_total_mb, t.proc_vram_mb, t.proc_vram_budget_mb, t.queues[0],
          t.queues[1], t.queues[2], t.queues[3], t.activity[0], t.activity[1], t.activity[2],
          t.activity[3], t.activity[4], t.activity[5], unsigned(t.render_paused),
          unsigned(t.skate2), t.activity[size_t(Activity::kPrewarmCoalesced)]);
      f << line;
    }
  }
  // Per-thread CPU (long format: one row per thread per tick, threads that
  // used >= 0.05 ms in that tick), 100 = one full core.
  struct ThreadAgg {
    double during_us = 0.0;
    double base_us = 0.0;
  };
  std::unordered_map<uint32_t, ThreadAgg> agg;
  {
    std::ofstream f(std::string(stem) + "_threads.csv");
    f << "t_ms,tid,name,cpu_pct,kernel_ms,user_ms\n";
    char line[256];
    for (const TickRec& t : ticks) {
      if (t.t_us < from) {
        continue;
      }
      for (int i = 0; i < t.nthreads; ++i) {
        const ThreadSample& s = t.threads[i];
        std::string name = ThreadName(s.tid);
        std::replace(name.begin(), name.end(), ',', ' ');
        std::snprintf(line, sizeof(line), "%.1f,%u,%s,%.1f,%.3f,%.3f\n", rel_ms(t.t_us), s.tid,
                      name.c_str(), 100.0 * s.cpu_us / std::max<uint32_t>(t.len_us, 1),
                      s.kernel_us == UINT32_MAX ? -1.0 : s.kernel_us / 1000.0,
                      s.user_us == UINT32_MAX ? -1.0 : s.user_us / 1000.0);
        f << line;
        if (t.t_us > during.from_us && t.t_us <= during.to_us) {
          agg[s.tid].during_us += s.cpu_us;
        } else if (t.t_us > base.from_us && t.t_us <= base.to_us) {
          agg[s.tid].base_us += s.cpu_us;
        }
      }
    }
  }

  // ---- Log summary: the drop (during) vs the seconds before it (base) ----
  struct Stats {
    double n = 0, sum = 0, max = 0;
    void Add(double v) {
      ++n;
      sum += v;
      max = std::max(max, v);
    }
    double Avg() const { return n > 0 ? sum / n : 0.0; }
  };
  const auto in = [](int64_t t, const Window& w) { return t > w.from_us && t <= w.to_us; };
  const double during_s = std::max(1e-3, (during.to_us - during.from_us) / 1e6);
  const double base_s = std::max(1e-3, (base.to_us - base.from_us) / 1e6);
  REXLOG_WARN(
      "perf-capture #{} ({}): {:.1f} s at {:.0f} fps min (normal {:.0f}) vs {:.1f} s before; "
      "files {}_*.csv",
      s_count, kind, during_s, min_fps, ref_fps, base_s, stem);
  for (int pass = 0; pass < 2; ++pass) {
    const Window& w = pass == 0 ? during : base;
    const double secs = pass == 0 ? during_s : base_s;
    Stats dt, pace, work, gcpu, build, err;
    for (const GuestRec& r : guest) {
      if (!in(r.t_us, w)) {
        continue;
      }
      dt.Add(r.dt_us / 1000.0);
      pace.Add(r.pace_us / 1000.0);
      work.Add(std::max(0.0, (double(r.dt_us) - r.pace_us) / 1000.0));
      gcpu.Add(cyc_ms(r.cpu_cycles));
      build.Add(r.build_us / 1000.0);
      err.Add(std::fabs(r.smooth_err_ms));
    }
    Stats total, cpu, pre, items, shadow, settle, commit, twod, tail, dec, rdt;
    uint32_t decodes = 0, rebuilds = 0, recenters = 0, yields = 0;
    for (const RenderRec& rr : render) {
      if (!in(rr.t_us, w)) {
        continue;
      }
      const RenderRecord& r = rr.r;
      rdt.Add(rr.dt_us / 1000.0);
      total.Add(r.total_ns / 1e6);
      cpu.Add(cyc_ms(r.cpu_cycles));
      if (r.complete) {
        pre.Add(r.pre_ns / 1e6);
        items.Add(r.items_ns / 1e6);
        shadow.Add(r.shadow_ns / 1e6);
        settle.Add(r.settle_ns / 1e6);
        commit.Add(r.commit_ns / 1e6);
        twod.Add(r.twod_ns / 1e6);
        tail.Add(r.tail_ns / 1e6);
        dec.Add(r.decode_ns / 1e6);
      } else {
        ++yields;
      }
      decodes += r.mesh_decodes + r.tex_decodes;
      rebuilds += r.nsm_rebuild != 0 ? 1 : 0;
      recenters += r.ws_recenter;
    }
    Stats sys, proc, gpu, gfx, span, wait, vram, pvram, budget, faults, rd, q0, q1, q2;
    uint64_t throttle = 0;
    int pstate_max = -1;
    uint32_t act[size_t(Activity::kCount)] = {};
    uint32_t paused = 0;
    float ws0 = -1, ws1 = -1;
    for (const TickRec& t : ticks) {
      if (!in(t.t_us, w)) {
        continue;
      }
      sys.Add(t.sys_busy_pct);
      proc.Add(t.proc_cpu_pct);
      if (t.gpu_util >= 0) gpu.Add(t.gpu_util);
      if (t.gfx_mhz >= 0) gfx.Add(t.gfx_mhz);
      if (t.gpu_span_ms >= 0) span.Add(t.gpu_span_ms);
      if (t.gpu_wait_ms >= 0) wait.Add(t.gpu_wait_ms);
      if (t.vram_used_mb >= 0) vram.Add(t.vram_used_mb);
      if (t.proc_vram_mb >= 0) pvram.Add(t.proc_vram_mb);
      if (t.proc_vram_budget_mb >= 0) budget.Add(t.proc_vram_budget_mb);
      faults.Add(t.faults_s);
      rd.Add(t.io_read_mb_s);
      q0.Add(t.queues[0]);
      q1.Add(t.queues[1]);
      q2.Add(t.queues[2]);
      throttle |= t.throttle;
      pstate_max = std::max<int>(pstate_max, t.pstate);
      for (size_t k = 0; k < size_t(Activity::kCount); ++k) act[k] += t.activity[k];
      paused += t.render_paused;
      if (ws0 < 0) ws0 = t.ws_mb;
      ws1 = t.ws_mb;
    }
    const char* label = pass == 0 ? "DURING" : "BEFORE";
    REXLOG_WARN(
        "  {} guest: {:.1f} fps, frame {:.1f}/{:.1f} ms (avg/max), cap sleep {:.2f} ms, work "
        "{:.2f} ms, render-thread CPU {:.2f} ms, scene build {:.2f}/{:.2f} ms, smoothing err "
        "{:.1f} ms max",
        label, dt.n / secs, dt.Avg(), dt.max, pace.Avg(), work.Avg(), gcpu.Avg(), build.Avg(),
        build.max, err.max);
    REXLOG_WARN(
        "  {} native render: {:.1f}/s, {:.2f}/{:.2f} ms (avg/max), GPU-thread CPU {:.2f} ms | "
        "pre {:.2f} items {:.2f} shadow {:.2f} settle {:.2f} commit {:.2f} 2d {:.2f} tail "
        "{:.2f} decode {:.2f} ms | decodes {} sun-map rebuilds {} world-shadow recenters {} "
        "yields {} | gap max {:.1f} ms",
        label, rdt.n / secs, total.Avg(), total.max, cpu.Avg(), pre.Avg(), items.Avg(),
        shadow.Avg(), settle.Avg(), commit.Avg(), twod.Avg(), tail.Avg(), dec.Avg(), decodes,
        rebuilds, recenters, yields, rdt.max);
    REXLOG_WARN(
        "  {} system: CPU {:.0f}% (process {:.0f}% of one core), working set {:.0f}->{:.0f} MB, "
        "page faults {:.0f}/s, disk read {:.1f} MB/s | GPU {:.0f}% {:.0f} MHz, frame span "
        "{:.2f}/{:.2f} ms (avg/max), GPU-thread fence waits {:.2f}/{:.2f} ms per frame, lowest "
        "perf state P{} throttle 0x{:X} | VRAM {:.0f} MB, process {:.0f}/{:.0f} MB budget",
        label, sys.Avg(), proc.Avg(), ws0, ws1, faults.Avg(), rd.Avg(), gpu.Avg(), gfx.Avg(),
        span.Avg(), span.max, wait.Avg(), wait.max, pstate_max,
        static_cast<unsigned long long>(throttle), vram.max, pvram.max, budget.Avg());
    REXLOG_WARN(
        "  {} streaming: loads {} unloads {} done {}/{} file requests {} decompress {} | "
        "render paused {} ticks | decode queues max prewarm {:.0f} miss {:.0f} dyn {:.0f}",
        label, act[0], act[1], act[2], act[3], act[4], act[5], paused, q0.max, q1.max, q2.max);
  }
  // Coarse cross-thread spans started during this Home window. These overlap
  // across threads and nest original dispatch with its existing native hooks.
  if (REXCVAR_GET(skate3_perf_work_timeline)) {
    constexpr const char* phases[] = {"sort_capture", "sort_original", "native_decode", "guest_decompress", "native_sheet", "native_dynamic"};
    std::vector<WorkSpan> spans;
    uint64_t overwritten = 0;
    for (const auto& lane : g_work) {
      if (lane.press.load(std::memory_order_acquire) != t0_us) continue;
      const uint64_t count = lane.records.Count() - lane.first.load(std::memory_order_relaxed);
      if (count > kWorkLaneRecords) overwritten += count - kWorkLaneRecords;
      for (const WorkSpan& span : lane.records.Snapshot()) {
        if (span.press_us == t0_us && span.begin_us >= t0_us &&
            span.begin_us < t0_us + kManualCaptureUs) spans.push_back(span);
      }
    }
    std::sort(spans.begin(), spans.end(), [](const WorkSpan& a, const WorkSpan& b) {
      return a.begin_us < b.begin_us;
    });
    std::ofstream f(std::string(stem) + "_work.csv");
    f << "begin_ms,end_ms,wall_ms,cpu_ms,tid,name,phase,mesh,miss,asset_valid,fingerprint,diffuse_guid,vb_bytes,ib_count,bbox_min_x,bbox_min_y,bbox_min_z,bbox_max_x,bbox_max_y,bbox_max_z\n";
    char line[768];
    for (const WorkSpan& span : spans) {
      std::string name = ThreadName(span.tid);
      std::replace(name.begin(), name.end(), ',', ' ');
      const auto& a = span.asset;
      std::snprintf(line, sizeof(line), "%.3f,%.3f,%.3f,%.3f,%u,%s,%s,%08X,%u,%u,%016llX,%016llX,%u,%u,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g\n",
                    rel_ms(span.begin_us), rel_ms(span.end_us),
                    (span.end_us - span.begin_us) / 1000.0,
                    span.cycles == UINT64_MAX ? -1.0 : cyc_ms(span.cycles),
                    span.tid, name.c_str(), phases[size_t(span.phase)],
                    a.mesh, unsigned(a.miss), unsigned(a.valid),
                    static_cast<unsigned long long>(a.fingerprint),
                    static_cast<unsigned long long>(a.diffuse_guid), a.vb_bytes, a.ib_count,
                    a.bbox_min[0], a.bbox_min[1], a.bbox_min[2],
                    a.bbox_max[0], a.bbox_max[1], a.bbox_max[2]);
      f << line;
    }
    REXLOG_INFO("perf-capture: coarse work timeline spans={} overwritten={} unassigned_total={} ({}_{})",
                spans.size(), overwritten, g_work_unassigned.load(std::memory_order_relaxed), stem, "work.csv");
  }
  // ---- Function profile of the sampled threads ----
  // Per (thread, window): leaf counts (where the thread was executing) and
  // inclusive counts (each distinct return address once per sample: time
  // spent inside that call). Windows: during, before, and late = the during
  // samples taken over kLateUs after the last guest swap (the excess work of
  // overlong guest frames). Addresses are written module-relative; the exe's
  // are symbolized offline against skate3.pdb.
  {
    const std::vector<StackSample> prof = g_prof.Snapshot();
    struct Key {
      uint64_t addr;
      uint8_t thread, window, kind;
      bool operator==(const Key& o) const {
        return addr == o.addr && thread == o.thread && window == o.window && kind == o.kind;
      }
    };
    struct KeyHash {
      size_t operator()(const Key& k) const {
        return std::hash<uint64_t>()(k.addr * 31u + k.thread * 7u + k.window * 3u + k.kind);
      }
    };
    std::unordered_map<Key, uint32_t, KeyHash> counts;
    uint32_t totals[std::size(kProfThreads)][3] = {};
    const auto add = [&](const StackSample& s, uint8_t window) {
      ++totals[s.thread][window];
      ++counts[Key{s.pcs[0], s.thread, window, 0}];
      for (int i = 0; i < s.n; ++i) {
        bool repeat = false;
        for (int j = 0; j < i && !repeat; ++j) {
          repeat = s.pcs[j] == s.pcs[i];
        }
        if (!repeat) {
          ++counts[Key{s.pcs[i], s.thread, window, 1}];
        }
      }
    };
    for (const StackSample& s : prof) {
      if (in(s.t_us, during)) {
        add(s, 0);
        if (s.since_swap_us > kLateUs) {
          add(s, 2);
        }
      } else if (in(s.t_us, base)) {
        add(s, 1);
      }
    }
    struct Module {
      std::string name;
      uint64_t base = 0;
    };
    std::unordered_map<uint64_t, Module> modules;  // by module base
    const auto module_of = [&](uint64_t addr) -> const Module& {
      HMODULE mod = nullptr;
      static const Module kUnknown{"?", 0};
      if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                  GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                              reinterpret_cast<LPCWSTR>(addr), &mod) ||
          mod == nullptr) {
        return kUnknown;
      }
      const uint64_t mbase = reinterpret_cast<uint64_t>(mod);
      auto it = modules.find(mbase);
      if (it == modules.end()) {
        wchar_t path[MAX_PATH] = {};
        GetModuleFileNameW(mod, path, MAX_PATH);
        char utf8[MAX_PATH * 3] = {};
        WideCharToMultiByte(CP_UTF8, 0, path, -1, utf8, sizeof(utf8) - 1, nullptr, nullptr);
        std::string name = std::filesystem::path(utf8).filename().string();
        it = modules.emplace(mbase, Module{name, mbase}).first;
      }
      return it->second;
    };
    // Time-resolved samples let offline symbolization isolate the exact
    // activation frame instead of combining all six seconds into one table.
    if (!prof.empty()) {
      std::ofstream stacks(std::string(stem) + "_stacks.csv");
      stacks << "t_ms,since_swap_ms,thread,depth,module,rva\n";
      char sample_line[256];
      for (const StackSample& sample : prof) {
        if (!in(sample.t_us, during)) continue;
        for (uint32_t depth = 0; depth < sample.n; ++depth) {
          const Module& module = module_of(sample.pcs[depth]);
          std::snprintf(sample_line, sizeof(sample_line), "%.3f,%.3f,%s,%u,%s,0x%llX\n",
                        rel_ms(sample.t_us), sample.since_swap_us/1000.0,
                        kProfThreads[sample.thread], depth, module.name.c_str(),
                        static_cast<unsigned long long>(sample.pcs[depth]-module.base));
          stacks << sample_line;
        }
      }
    }
    std::ofstream f(std::string(stem) + "_profile.csv");
    f << "thread,window,kind,module,rva,count,window_samples\n";
    char line[256];
    static const char* const kWindowNames[] = {"during", "before", "late"};
    for (const auto& [k, c] : counts) {
      const Module& m = module_of(k.addr);
      std::snprintf(line, sizeof(line), "%s,%s,%s,%s,0x%llX,%u,%u\n", kProfThreads[k.thread],
                    kWindowNames[k.window], k.kind == 0 ? "leaf" : "incl",
                    m.name.c_str(), static_cast<unsigned long long>(k.addr - m.base), c,
                    totals[k.thread][k.window]);
      f << line;
    }
    REXLOG_WARN(
        "  profile: render_thread {} samples during ({} late) / {} before, GPU Commands {} / {}, "
        "Host UI {} / {} (function profile in {}_profile.csv)",
        totals[0][0], totals[0][2], totals[0][1], totals[1][0], totals[1][1], totals[2][0],
        totals[2][1], stem);
  }
  std::vector<std::pair<uint32_t, ThreadAgg>> top(agg.begin(), agg.end());
  std::sort(top.begin(), top.end(), [](const auto& a, const auto& b) {
    return a.second.during_us > b.second.during_us;
  });
  std::string line;
  for (size_t i = 0; i < top.size() && i < 12; ++i) {
    char part[200];
    std::snprintf(part, sizeof(part), "%s%s %.0f%% (before %.0f%%)", i ? ", " : "",
                  ThreadName(top[i].first).c_str(),
                  100.0 * top[i].second.during_us / (during_s * 1e6),
                  100.0 * top[i].second.base_us / (base_s * 1e6));
    line += part;
  }
  REXLOG_WARN("  threads (100% = one core): {}", line);
}

// ---- Sampler thread ----
void SamplerLoop() {
  NameCurrentThread("Perf recorder");
  // Cycle rate for QueryThreadCycleTime (TSC ticks on x64 Windows).
  {
    const uint64_t c0 = __rdtsc();
    const auto t0 = Clock::now();
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
    const uint64_t c1 = __rdtsc();
    const double us =
        std::chrono::duration<double, std::micro>(Clock::now() - t0).count();
    g_cycles_per_us.store(us > 0.0 ? double(c1 - c0) / us : 0.0, std::memory_order_relaxed);
  }
  Nvml nvml;
  const bool have_nvml = nvml.Load();
  DxgiVram dxgi;
  const bool have_dxgi = dxgi.Load();
  ThreadTable table;
  if (REXCVAR_GET(skate3_perf_recorder_system)) {
    table.Refresh();
  }
  SYSTEM_INFO si{};
  GetSystemInfo(&si);
  const double ncpu = std::max<DWORD>(1, si.dwNumberOfProcessors);
  REXLOG_INFO(
      "perf-capture: recorder running ({:.0f} MHz cycle clock, {} CPUs, NVML {}, DXGI VRAM {}); "
      "Home = manual capture",
      g_cycles_per_us.load() , int(ncpu), have_nvml ? "yes" : "no", have_dxgi ? "yes" : "no");

  FILETIME idle0{}, kern0{}, user0{};
  GetSystemTimes(&idle0, &kern0, &user0);
  FILETIME pc{}, pe{}, pk0{}, pu0{};
  GetProcessTimes(GetCurrentProcess(), &pc, &pe, &pk0, &pu0);
  PROCESS_MEMORY_COUNTERS_EX pm0{};
  K32GetProcessMemoryInfo(GetCurrentProcess(), reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&pm0),
                          sizeof(pm0));
  IO_COUNTERS io0{};
  GetProcessIoCounters(GetCurrentProcess(), &io0);
  uint64_t guest0 = g_guest.Count(), render0 = g_render.Count();
  int64_t prev_us = NowUs();
  int64_t next_refresh_us = prev_us + 1'000'000;
  int64_t manual_names_press = 0;

  // GPU (NVML) readings are polled every 10th tick (driver queries are the
  // costly part of a tick, ~19 ms per poll measured) and carried forward in
  // between.
  TickRec gpu_last;
  uint64_t tick_index = 0;
  // Sampler self-cost by section (logged once after 100 ticks).
  double cost_threads_us = 0.0, cost_gpu_us = 0.0, cost_proc_us = 0.0, cost_detect_us = 0.0;

  // Slowdown detector state.
  std::vector<float> fps_hist;  // 1-second guest fps per tick, normal periods
  std::vector<uint32_t> recent_frames, recent_len;  // last 10 ticks
  enum class State { kNormal, kSlow, kRecover } state = State::kNormal;
  int slow_ticks = 0;
  int64_t ev_start = 0, ev_end = 0, recover_since = 0;
  float ev_min = 0.0f, ev_ref = 0.0f;
  int auto_dumps = 0;

  while (true) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if (!REXCVAR_GET(skate3_perf_recorder)) {
      continue;
    }
    const int64_t now = NowUs();
    TickRec t;
    t.t_us = now;
    t.len_us = uint32_t(std::max<int64_t>(1, now - prev_us));
    prev_us = now;
    const double sec = t.len_us / 1e6;
    ++tick_index;
    const int64_t c0 = NowUs();
    const bool system = REXCVAR_GET(skate3_perf_recorder_system);
    // Discover once per Home. Optionally sample those existing handles for six
    // seconds; periodic enumeration and NVML/DXGI queries remain system-only.
    const int64_t manual_press = g_manual_press_us.load(std::memory_order_acquire);
    if (!system && ManualProfileActive(now) && manual_names_press != manual_press) {
      table.Refresh(); table.Prime(); manual_names_press = manual_press;
      if (REXCVAR_GET(skate3_perf_work_timeline)) {
        REXLOG_INFO("perf-capture: Home NSV all-thread CPU and coarse work timeline active for 6 s (no extra stack suspension or GPU queries)");
      }
      REXLOG_INFO("perf-capture: Home-only NSV stack sampling active for 6 s");
      REXLOG_INFO("perf-capture: original-swap wall/CPU timing active for NSV Home window");
      if (!REXCVAR_GET(skate3_perf_scene_detail)) {
        REXLOG_INFO("perf-capture: Home NSV scene counters active (coarse phases, no item clocks)");
      }
    }
    if (system) {
      if (now >= next_refresh_us) {
        table.Refresh();
        next_refresh_us = now + 2'000'000;
      }
    }
    if (system || (REXCVAR_GET(skate3_perf_work_timeline) && ManualProfileActive(now))) {
      table.Sample(t, g_cycles_per_us.load(std::memory_order_relaxed));
    }
    const int64_t c1 = NowUs();

    FILETIME idle{}, kern{}, user{};
    if (GetSystemTimes(&idle, &kern, &user)) {
      const double di = double(FileTimeU64(idle) - FileTimeU64(idle0));
      const double dk = double(FileTimeU64(kern) - FileTimeU64(kern0));
      const double du = double(FileTimeU64(user) - FileTimeU64(user0));
      t.sys_busy_pct = dk + du > 0.0 ? float(100.0 * (dk + du - di) / (dk + du)) : 0.0f;
      idle0 = idle;
      kern0 = kern;
      user0 = user;
    }
    FILETIME pk{}, pu{};
    if (GetProcessTimes(GetCurrentProcess(), &pc, &pe, &pk, &pu)) {
      const double d100ns = double(FileTimeU64(pk) - FileTimeU64(pk0)) +
                            double(FileTimeU64(pu) - FileTimeU64(pu0));
      t.proc_cpu_pct = float(100.0 * (d100ns / 10.0) / double(t.len_us));
      pk0 = pk;
      pu0 = pu;
    }
    PROCESS_MEMORY_COUNTERS_EX pm{};
    if (K32GetProcessMemoryInfo(GetCurrentProcess(),
                                reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&pm), sizeof(pm))) {
      t.ws_mb = float(double(pm.WorkingSetSize) / 1048576.0);
      t.private_mb = float(double(pm.PrivateUsage) / 1048576.0);
      t.faults_s = float(double(pm.PageFaultCount - pm0.PageFaultCount) / sec);
      pm0 = pm;
    }
    IO_COUNTERS io{};
    if (GetProcessIoCounters(GetCurrentProcess(), &io)) {
      t.io_read_mb_s =
          float(double(io.ReadTransferCount - io0.ReadTransferCount) / 1048576.0 / sec);
      t.io_reads_s = float(double(io.ReadOperationCount - io0.ReadOperationCount) / sec);
      t.io_write_mb_s =
          float(double(io.WriteTransferCount - io0.WriteTransferCount) / 1048576.0 / sec);
      io0 = io;
    }
    const int64_t c2 = NowUs();
    if (system && tick_index % 10 == 1) {
      nvml.Sample(gpu_last);
      dxgi.Sample(gpu_last);
    }
    t.gpu_util = gpu_last.gpu_util;
    t.gpu_mem_util = gpu_last.gpu_mem_util;
    t.gfx_mhz = gpu_last.gfx_mhz;
    t.mem_mhz = gpu_last.mem_mhz;
    t.pstate = gpu_last.pstate;
    if (system) {
      SamplePresenter(t);
    }
    t.vram_used_mb = gpu_last.vram_used_mb;
    t.vram_total_mb = gpu_last.vram_total_mb;
    t.throttle = gpu_last.throttle;
    t.proc_vram_mb = gpu_last.proc_vram_mb;
    t.proc_vram_budget_mb = gpu_last.proc_vram_budget_mb;
    const int64_t c3 = NowUs();
    skate3::native_scene::GetDecodeQueueDepths(t.queues);
    for (size_t k = 0; k < size_t(Activity::kCount); ++k) {
      t.activity[k] = g_activity[k].exchange(0, std::memory_order_relaxed);
    }
    t.render_paused = g_render_paused.load(std::memory_order_relaxed) ? 1 : 0;
    t.skate2 = skate3::world_loading::IsSkate2Gameplay() ? 1 : 0;
    const uint64_t guest1 = g_guest.Count(), render1 = g_render.Count();
    t.guest_frames = uint32_t(guest1 - guest0);
    t.render_frames = uint32_t(render1 - render0);
    guest0 = guest1;
    render0 = render1;
    g_ticks.Push(t);
    const int64_t c4 = NowUs();
    skate3::freeze_capture::Poll(now - g_last_native_us.load(std::memory_order_relaxed) < 1'000'000,
                                 t.render_paused != 0);
    cost_threads_us += double(c1 - c0);
    cost_proc_us += double(c2 - c1) + double(c4 - c3);
    cost_gpu_us += double(c3 - c2);

    // ---- Slowdown detection on the 1-second guest frame rate ----
    recent_frames.push_back(t.guest_frames);
    recent_len.push_back(t.len_us);
    if (recent_frames.size() > 10) {
      recent_frames.erase(recent_frames.begin());
      recent_len.erase(recent_len.begin());
    }
    uint64_t frames1 = 0, len1 = 0;
    for (size_t k = 0; k < recent_frames.size(); ++k) {
      frames1 += recent_frames[k];
      len1 += recent_len[k];
    }
    const float fps1 = len1 > 0 ? float(frames1 * 1e6 / double(len1)) : 0.0f;
    // Only judge gameplay: the native renderer drew a full frame recently
    // (loading screens and menus yield to the emulated output).
    const bool gameplay =
        now - g_last_native_us.load(std::memory_order_relaxed) < 1'000'000;
    float ref = 0.0f;
    if (fps_hist.size() >= 50) {
      std::vector<float> sorted = fps_hist;
      std::sort(sorted.begin(), sorted.end());
      ref = sorted[size_t(double(sorted.size() - 1) * 0.9)];
    }
    switch (state) {
      case State::kNormal:
        if (gameplay && recent_frames.size() == 10) {
          fps_hist.push_back(fps1);
          if (fps_hist.size() > 600) {
            fps_hist.erase(fps_hist.begin());
          }
        }
        slow_ticks = gameplay && ref > 20.0f && fps1 < 0.8f * ref ? slow_ticks + 1 : 0;
        if (slow_ticks >= 5) {
          state = State::kSlow;
          ev_start = now - 1'500'000;
          ev_min = fps1;
          ev_ref = ref;
          REXLOG_WARN("perf-capture: slowdown detected ({:.0f} fps vs normal {:.0f})", fps1,
                      ref);
        }
        break;
      case State::kSlow:
        ev_min = std::min(ev_min, fps1);
        if (fps1 >= 0.92f * ev_ref) {
          state = State::kRecover;
          recover_since = now;
          ev_end = now;
        } else if (now - ev_start > 60'000'000) {
          ev_end = now;
          state = State::kRecover;
          recover_since = now - 4'000'000;  // dump now: a very long drop
        }
        break;
      case State::kRecover:
        if (fps1 < 0.85f * ev_ref && now - ev_start <= 60'000'000) {
          state = State::kSlow;
        } else if (now - recover_since >= 4'000'000) {
          if (REXCVAR_GET(skate3_perf_recorder_auto) && auto_dumps < 8) {
            ++auto_dumps;
            WriteCapture("automatic", ev_start, Window{ev_start, ev_end},
                         Window{ev_start - 20'000'000, ev_start - 2'000'000}, ev_ref, ev_min);
          }
          state = State::kNormal;
          slow_ticks = 0;
        }
        break;
    }

    cost_detect_us += double(NowUs() - c4);
    if (tick_index == 100) {
      REXLOG_INFO(
          "perf-capture: sampler cost per tick (avg of 100): threads {:.0f} us, process "
          "counters {:.0f} us, GPU/VRAM queries {:.0f} us, detector {:.0f} us",
          cost_threads_us / 100.0, cost_proc_us / 100.0, cost_gpu_us / 100.0,
          cost_detect_us / 100.0);
    }

    // ---- Manual capture: ~6 s after the press ----
    const int64_t press = g_manual_press_us.load(std::memory_order_acquire);
    if (press != 0 && now - press >= kManualCaptureUs) {
      g_manual_press_us.store(0, std::memory_order_release);
      float min_fps = fps1;
      for (const TickRec& tk : g_ticks.Snapshot()) {
        if (tk.t_us > press && tk.t_us <= press + kManualCaptureUs && tk.len_us > 0) {
          min_fps = std::min(min_fps, float(tk.guest_frames * 1e6 / tk.len_us));
        }
      }
      WriteCapture("Home key", press, Window{press, press + kManualCaptureUs},
                   Window{press - 20'000'000, press}, ref, min_fps);
    }
  }
}

void StartSampler() {
  std::thread(SamplerLoop).detach();
  std::thread(ProfilerLoop).detach();
}

#else  // !_WIN32

void StartSampler() {}

#endif

}  // namespace
}  // namespace skate3::perf_recorder
