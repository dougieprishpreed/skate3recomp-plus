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

// Original guest D3D swap only (after native scene build, before pacing).
// NSV Home window only; pairs wall time with consumed thread cycles so busy
// progress polling is not mistaken for independent scene-construction work.
class GuestSwapScope {
 public:
  explicit GuestSwapScope(bool native);
  ~GuestSwapScope();
  GuestSwapScope(const GuestSwapScope&) = delete;
  GuestSwapScope& operator=(const GuestSwapScope&) = delete;
 private:
  int64_t begin_us_ = 0;
  uint64_t begin_cycles_ = 0;
};

// Home-only coarse cross-thread work spans. No draw/item sampling, extra thread
// suspension or resource scheduling changes. Disabled by default.
enum class WorkPhase : uint8_t { kSortCapture, kSortOriginal, kNativeDecode, kGuestDecompress, kNativeSheet, kNativeDynamic };
// Already prepared metadata only: no guest reads, paths or geometry dumps.
// Helps join worker jobs to original assets and identify repeated payloads.
struct WorkAsset {
  uint64_t fingerprint = 0, diffuse_guid = 0;
  uint32_t mesh = 0, vb_bytes = 0, ib_count = 0;
  float bbox_min[3] = {}, bbox_max[3] = {};
  bool miss = false, valid = false;
};
class WorkTimelineScope {
 public:
  explicit WorkTimelineScope(WorkPhase phase, bool enabled = true);
  ~WorkTimelineScope();
  WorkTimelineScope(const WorkTimelineScope&) = delete;
  WorkTimelineScope& operator=(const WorkTimelineScope&) = delete;
  void SetMesh(uint32_t mesh, bool miss);
  void SetAsset(uint32_t mesh, uint64_t fingerprint, uint64_t diffuse_guid,
                uint32_t vb_bytes, uint32_t ib_count,
                const float* bbox_min, const float* bbox_max);
 private:
  int64_t begin_us_ = 0, press_us_ = 0;
  uint64_t begin_cycles_ = 0;
  uint32_t lane_ = ~uint32_t{0};
  WorkPhase phase_;
  WorkAsset asset_;
};

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
  // Exact-key Vulkan lookup coverage, NSV opt-in only; no per-lookup clocks.
  uint32_t table_lookup_calls = 0, table_lookup_hits = 0;
  uint32_t output_view_borrows = 0, output_view_reuses = 0;
  // CPU-only rotating 1-in-64 item sample, enabled only for NSV scene detail.
  // Raw sample totals, never whole-frame or GPU timings. Early returns count
  // in sample_total_ns; only completed items contribute the four stages.
  uint64_t sample_total_ns = 0, sample_outside_ns = 0;
  uint64_t sample_mesh_ns = 0, sample_texture_ns = 0;
  uint64_t sample_constants_ns = 0, sample_submit_ns = 0;
  uint32_t sample_items = 0, sample_outside_items = 0, sample_completed = 0;
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
  kPrewarmCoalesced,
  kCount,
};
void Count(Activity a, uint32_t amount = 1);
void SetRenderPaused(bool paused);

// This thread's consumed CPU cycles (monotonic per thread; converted to time
// at dump with the calibrated cycle rate). 0 where unsupported.
uint64_t ThreadCycles();
// Names the calling thread for the per-thread CPU breakdown.
void NameCurrentThread(const char* name);

// Capture hotkey: dump the recent history now (plus a few seconds after).
void RequestCapture();

// Cooperative timing of outer NSV guest event dispatches. No thread
// suspension, stack unwinding or output on the measured thread. Events from
// this thread are folded into its next OnGuestFrame record (other threads'
// records are not mixed in). Begin returns 0 when the recorder is disabled.
uint64_t BeginGuestEvent();
void EndGuestEvent(uint64_t begin_us, uint32_t type, uint32_t token, uint32_t caller);
void NoteIndexedTextureLookup(uint32_t scanned_nodes);
bool RegistrationDetailEnabled();
void NoteRegistrationMeshes(uint32_t meshes);
void NoteFastContextResourceSearch(uint32_t scanned_entries);

// Full sampled detail only. Home also enables coarse build phases and cache
// counters for six seconds, without per-item clocks or render-item samples.
bool SceneDetailEnabled(bool nsv);
bool SceneCountersActive();
void NoteSceneWarmState(uint64_t started, uint64_t published, uint64_t unready,
                        uint64_t rejected, uint32_t queued, uint32_t pending);
enum class SceneCacheWork : uint8_t {
  kHit, kWalk, kFingerprint, kRebind,
  kMissAbsent, kMissRefresh, kMissPending, kMissInvalid, kWarmReuse
};
void NoteSceneCacheWork(SceneCacheWork work);
// The initial scene-vector reserve only: retained capacity, count/bytes/time
// of real growth allocations. No clocks here; only active NSV scene detail.
void NoteSceneItemCapacity(uint64_t capacity, uint64_t reserved_bytes, uint64_t reserve_ns);
// Rotating 1-in-64 CPU sample of full walks / fingerprints during NSV build.
// Raw sampled timings, not totals or GPU time; no clocks outside scene detail.
class GuestSceneCacheScope {
 public:
  explicit GuestSceneCacheScope(SceneCacheWork work);
  ~GuestSceneCacheScope();
  GuestSceneCacheScope(const GuestSceneCacheScope&) = delete;
  GuestSceneCacheScope& operator=(const GuestSceneCacheScope&) = delete;
 private:
  uint64_t begin_us_ = 0;
  SceneCacheWork work_;
};
// Same rotating sample for complete geometry and its nested cache operation.
// Only armed inside an active NSV scene-detail build, including early returns.
class GuestSceneGeometryScope {
 public:
  GuestSceneGeometryScope();
  ~GuestSceneGeometryScope();
  void BeginCache();
  void EndCache();
  GuestSceneGeometryScope(const GuestSceneGeometryScope&) = delete;
  GuestSceneGeometryScope& operator=(const GuestSceneGeometryScope&) = delete;
 private:
  uint64_t begin_us_ = 0, cache_begin_us_ = 0;
};
enum class SceneBuildPhase : uint8_t { kWorld, kMerge, kRetain };
class GuestSceneScope {
 public:
  GuestSceneScope(bool nsv, uint64_t capture_ns, uint32_t records);
  ~GuestSceneScope();
  GuestSceneScope(const GuestSceneScope&) = delete;
  GuestSceneScope& operator=(const GuestSceneScope&) = delete;
 private:
  bool entered_ = false;
  uint64_t begin_read_faults_ = 0;
};
class GuestScenePhaseScope {
 public:
  explicit GuestScenePhaseScope(SceneBuildPhase phase);
  ~GuestScenePhaseScope();
  void End();
  GuestScenePhaseScope(const GuestScenePhaseScope&) = delete;
  GuestScenePhaseScope& operator=(const GuestScenePhaseScope&) = delete;
 private:
  uint64_t begin_us_ = 0;
  SceneBuildPhase phase_;
};

// Read-only callback timing within a cooperatively timed NSV dispatch.
// Nested callbacks are included in their parent once; all state is TLS.
// These scopes do no clock reads when no recorded NSV dispatch is active.
class GuestEventCallbackScope {
 public:
  GuestEventCallbackScope(uint32_t function, uint32_t subscriber);
  ~GuestEventCallbackScope();
  GuestEventCallbackScope(const GuestEventCallbackScope&) = delete;
  GuestEventCallbackScope& operator=(const GuestEventCallbackScope&) = delete;
 private:
  uint64_t begin_us_ = 0;
  uint32_t function_ = 0, subscriber_ = 0;
  bool entered_ = false;
};
// The original event listener may be a generic forwarding thunk. Record
// its actual target without invoking it again or touching guest registers.
void NoteGuestEventCallbackTarget(uint32_t function);
enum class GuestEventWork : uint8_t {
  kRenderInstance, kNativePrewarm, kResourcePresentation, kPrimitiveSetup, kGuidLookup,
  kInstanceContexts, kInstanceParameters, kInstanceRegistry, kInstanceAllocation,
  kContextMaterials, kContextFactory, kContextBindings, kContextResources,
  kPrimitiveDispatch, kPrimitiveClear, kPrimitiveCollect, kPrimitiveInsert,
  kPrimitiveCreate, kPrimitiveRelease, kPrimitiveRequest
};
class GuestEventWorkScope {
 public:
  explicit GuestEventWorkScope(GuestEventWork work, uint32_t target = 0);
  ~GuestEventWorkScope();
  GuestEventWorkScope(const GuestEventWorkScope&) = delete;
  GuestEventWorkScope& operator=(const GuestEventWorkScope&) = delete;
 private:
  uint64_t begin_us_ = 0;
  GuestEventWork work_;
  bool registration_entered_ = false, primitive_entered_ = false;
  uint64_t begin_cycles_ = 0;
  uint32_t target_ = 0;
};

// Presenter whose guest-frame GPU stats (frame span on the GPU timeline,
// GPU command thread fence waits) the recorder samples. Set on the UI
// thread at startup; cleared at shutdown before the presenter goes away.
void SetPresenter(rex::ui::Presenter* presenter);

}  // namespace skate3::perf_recorder
