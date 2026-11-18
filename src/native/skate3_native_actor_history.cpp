#include "native/skate3_native_actor_history.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <bit>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <thread>
#include <type_traits>
#include <vector>

#include <rex/cvar.h>
#include <rex/logging.h>

#include "native/skate3_native_entity.h"
#include "skate3_native_scene.h"

REXCVAR_DEFINE_BOOL(skate3_actor_history_capture, false, "Skate 3",
    "Keep up to 20 seconds of actor/camera history in bounded memory. "
    "Ctrl+Insert saves it asynchronously; no disk writes until requested")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DECLARE(bool, skate3_native_render_scene_smooth_entities);
REXCVAR_DECLARE(bool, skate3_native_render_scene_smooth_camera);

namespace skate3::actor_history {
namespace {
constexpr size_t kActors = 48, kBones = 8, kFrames = 2048;
constexpr double kSeconds = 20.0;
constexpr uint32_t kBoneIds[kBones] = {0, 1, 2, 4, 8, 16, 32, 64};
struct Pose {
  uint32_t present = 0, bone_count = 0, crc = 0, flags = 0;
  float world[16]{}, bones[kBones][12]{}, colors[8]{};
};
struct Actor {
  uint32_t mesh = 0, ctx = 0, owner = 0, instance = 0, cls = 0, family = 0;
  uint32_t vb = 0, ib = 0, ids[kBones]{}, authority_count = 0, authority_crc = 0;
  uint64_t history_key = 0;
  uint32_t history_count = 0, events = 0;  // events: PartEvent bits
  double period = 0, play = 0;
  float age_ms = -1, reserved = 0;  // age_ms: newest history sample's age on entry
  float owner_world[12]{};
  Pose raw{}, final{};
};
struct Frame {
  uint64_t generation = 0, world = 0;
  double time = 0, camera_tick = 0, camera_play = 0;
  uint32_t count = 0, smooth_entities = 0, smooth_camera = 0, retimed = 0;
  uint32_t camera_events = 0, render_paused = 0;  // camera_events: CameraEvent bits
  float camera_err_ms = 0, camera_age_ms = -1, camera_period_ms = 0, reserved = 0;
  uint32_t stream_loads = 0, stream_unloads = 0, stream_loads_done = 0, stream_unloads_done = 0;
  float raw_camera[3]{}, final_camera[3]{}, raw_vp[16]{}, final_vp[16]{};
  double raw_view_t = 0;  // when the build read raw_view (the frame's own camera)
  float raw_view[16]{};
  Actor actors[kActors]{};
};
static_assert(std::is_trivially_copyable_v<Frame>);
static_assert(sizeof(Frame) * kFrames < 128 * 1024 * 1024);
// A part as the game published it (Begin), before gap fill and smoothing.
// Camera keeps the ones nearest the game's camera once that is known.
struct Staged {
  uint32_t mesh = 0, ctx = 0, family = 0, vb = 0, ib = 0;
  Pose raw{};
};
// Camera samples the smoother received (CameraSample), any thread.
constexpr size_t kCameraSamples = 16384, kTickReads = size_t(1) << 20;
struct CamSample {
  double t = 0;
  uint64_t guest_ticks = 0, trace_serial = 0;
  uint32_t source = 0, reserved = 0;
  float view[16]{}, proj[16]{};
};
static_assert(std::is_trivially_copyable_v<CamSample> && std::is_trivially_copyable_v<TickRead>);
std::mutex camera_mutex;
std::deque<CamSample> camera_samples;
std::atomic<bool> capturing{false};
ClockAccess clock_access;
bool tracing = false;
std::deque<std::unique_ptr<Frame>> frames;
std::unique_ptr<Frame> current;
std::vector<Staged> staged;
uint8_t* guest = nullptr;
bool selected = false;
std::atomic<uint32_t> request{0};
std::atomic<bool> writing{false};
std::thread writer;
// Use the existing project's thread facilities; no stop-token library/OS
// dependency. Normal app shutdown joins earlier while logging is alive.
struct JoinWriterAtExit {
  ~JoinWriterAtExit() { if (writer.joinable()) writer.join(); }
} join_writer_at_exit;
std::mutex writer_mutex;
bool stopping = false;
uint64_t world = 0;

uint32_t Fingerprint(const float* values, size_t count) {
  uint32_t hash = 2166136261u;
  for (size_t i = 0; i < count; ++i) hash = (hash ^ std::bit_cast<uint32_t>(values[i])) * 16777619u;
  return hash;
}

void Sample(const native_scene::DrawItem& item, const uint32_t ids[kBones], Pose& pose) {
  pose.present = 1;
  pose.bone_count = uint32_t(item.bones.size() / 12);
  pose.crc = Fingerprint(item.bones.data(), item.bones.size());
  pose.flags = (item.skinned ? 1u : 0u) | (item.ropa ? 2u : 0u) |
      (item.caster_bank ? 4u : 0u) | (item.pending ? 8u : 0u) |
      (item.retained ? 16u : 0u) | (uint32_t(item.dbg_src) << 8) |
      (uint32_t(item.rows_src) << 16);
  std::copy_n(item.world, 16, pose.world);
  std::copy_n(item.char_rows + 48, 8, pose.colors);
  for (size_t i = 0; i < kBones; ++i)
    if (ids[i] < pose.bone_count) std::copy_n(item.bones.data() + ids[i] * 12, 12, pose.bones[i]);
}

// Bone 0's translation for skinned parts, the world row otherwise.
float Distance2(const Pose& pose, const float camera[3]) {
  const bool bone = (pose.flags & 1) && pose.bone_count > 0;
  float value = 0;
  for (size_t j = 0; j < 3; ++j) {
    const float d = (bone ? pose.bones[0][j * 4 + 3] : pose.world[12 + j]) - camera[j];
    value += d * d;
  }
  return value;
}

void Select(const float camera[3]) {
  selected = true;
  std::array<std::pair<float, size_t>, kActors> nearest;
  nearest.fill({INFINITY, 0});
  size_t count = 0;
  for (size_t i = 0; i < staged.size(); ++i) {
    const float distance = Distance2(staged[i].raw, camera);
    if (!std::isfinite(distance)) continue;
    auto candidate = std::make_pair(distance, i);
    for (size_t j = 0; j < kActors; ++j) if (candidate < nearest[j]) std::swap(candidate, nearest[j]);
    count = std::min(count + 1, kActors);
  }
  current->count = uint32_t(count);
  for (size_t i = 0; i < count; ++i) {
    Actor& actor = current->actors[i]; const Staged& part = staged[nearest[i].second];
    actor.mesh = part.mesh; actor.ctx = part.ctx; actor.family = part.family;
    actor.vb = part.vb; actor.ib = part.ib; std::copy_n(kBoneIds, kBones, actor.ids);
    native_entity::CtxInfo ident;
    if (native_entity::LookupCtx(part.ctx, &ident)) {
      actor.owner = ident.entity; actor.instance = ident.instance; actor.cls = uint32_t(ident.cls);
      native_entity::ReadEntityWorldRows(guest, part.ctx, actor.owner_world);
      float palette[96 * 12]; actor.authority_count = native_entity::ServeInstancePalette(guest, part.ctx, palette, 96);
      actor.authority_crc = Fingerprint(palette, actor.authority_count * 12);
    }
    actor.raw = part.raw;
  }
}

void Save(std::deque<std::unique_ptr<Frame>> saved, uint32_t marker, std::vector<CamSample> cams,
          std::vector<TickRead> ticks) {
  try {
    const std::filesystem::path directory = "logs";
    std::filesystem::create_directories(directory);
    const auto tick = uint64_t(saved.back()->time * 1000000.0);
    const auto stem = directory / ("actor_history_" + std::to_string(marker) + "_" + std::to_string(tick));
    std::ofstream binary(stem.string() + ".bin", std::ios::binary);
    binary.exceptions(std::ios::badbit | std::ios::failbit);
    binary.write("S3ACT234", 8);
    const uint32_t header[] = {uint32_t(saved.size()), uint32_t(sizeof(Frame)), uint32_t(sizeof(Actor)), uint32_t(sizeof(Pose))};
    binary.write(reinterpret_cast<const char*>(header), sizeof(header));
    for (const auto& frame : saved) binary.write(reinterpret_cast<const char*>(frame.get()), sizeof(Frame));
    const size_t cams_at = 24 + saved.size() * sizeof(Frame);
    binary.write(reinterpret_cast<const char*>(cams.data()), std::streamsize(cams.size() * sizeof(CamSample)));
    const size_t ticks_at = cams_at + cams.size() * sizeof(CamSample);
    binary.write(reinterpret_cast<const char*>(ticks.data()), std::streamsize(ticks.size() * sizeof(TickRead)));
    binary.close();
    // Explicit offsets make the POD dump readable across compilers/platforms.
    std::ofstream schema(stem.string() + ".json");
    schema.exceptions(std::ios::badbit | std::ios::failbit);
    schema << "{\"version\":234,\"byte_order\":\"little\",\"header_bytes\":24,\"max_actors\":" << kActors
           << ",\"bone_slots\":" << kBones << ",\"window_seconds\":" << kSeconds
           << ",\"camera_events\":{\"smoothed\":1,\"few_samples\":2,\"stale\":4,\"ring_gap\":8,\"ring_jump\":16,"
              "\"ring_turn\":32,\"clock_snap\":64,\"disabled\":128,\"built_sample\":256,\"outlier\":512}"
           << ",\"part_events\":{\"reset\":1,\"raw_short\":2,\"raw_size\":4,\"caster_hold\":8,\"interpolated\":16,\"held\":32}"
           << ",\"guest_tick_frequency\":" << clock_access.tick_frequency
           << ",\"camera_samples\":{\"offset\":" << cams_at << ",\"count\":" << cams.size()
           << ",\"size\":" << sizeof(CamSample) << ",\"sources\":{\"set_view_matrix\":1,\"sampler\":2},\"fields\":{";
#define FIELD(T, name) schema << "\"" #name "\":" << offsetof(T, name)
    FIELD(CamSample, t); schema << ','; FIELD(CamSample, guest_ticks); schema << ','; FIELD(CamSample, trace_serial); schema << ',';
    FIELD(CamSample, source); schema << ','; FIELD(CamSample, view); schema << ','; FIELD(CamSample, proj);
    schema << "}},\"tick_reads\":{\"offset\":" << ticks_at << ",\"count\":" << ticks.size()
           << ",\"size\":" << sizeof(TickRead) << ",\"fields\":{";
    FIELD(TickRead, ticks); schema << ','; FIELD(TickRead, thread); schema << ','; FIELD(TickRead, serial); schema << ',';
    FIELD(TickRead, site); schema << ','; FIELD(TickRead, kind); schema << ','; FIELD(TickRead, arg);
    schema << "},\"kinds\":{\"read\":0,\"wait_begin\":1,\"wait_end\":2,\"signal\":3,\"timer_set\":4,\"title\":5}"
           << ",\"title_events\":{\"camera_set\":1,\"frame_end_begin\":2,\"swap_call\":3,\"pace_begin\":4,\"pace_end\":5,\"sim_tick\":6,\"sim_rate\":7}"
           << "},\"frame\":{";
    FIELD(Frame, generation); schema << ','; FIELD(Frame, world); schema << ','; FIELD(Frame, time); schema << ',';
    FIELD(Frame, camera_tick); schema << ','; FIELD(Frame, camera_play); schema << ','; FIELD(Frame, count); schema << ',';
    FIELD(Frame, smooth_entities); schema << ','; FIELD(Frame, smooth_camera); schema << ','; FIELD(Frame, retimed); schema << ',';
    FIELD(Frame, camera_events); schema << ','; FIELD(Frame, render_paused); schema << ',';
    FIELD(Frame, camera_err_ms); schema << ','; FIELD(Frame, camera_age_ms); schema << ','; FIELD(Frame, camera_period_ms); schema << ',';
    FIELD(Frame, stream_loads); schema << ','; FIELD(Frame, stream_unloads); schema << ',';
    FIELD(Frame, stream_loads_done); schema << ','; FIELD(Frame, stream_unloads_done); schema << ',';
    FIELD(Frame, raw_camera); schema << ','; FIELD(Frame, final_camera); schema << ','; FIELD(Frame, raw_vp); schema << ',';
    FIELD(Frame, final_vp); schema << ','; FIELD(Frame, raw_view_t); schema << ','; FIELD(Frame, raw_view); schema << ',';
    FIELD(Frame, actors); schema << "},\"actor\":{";
    FIELD(Actor, mesh); schema << ','; FIELD(Actor, ctx); schema << ','; FIELD(Actor, owner); schema << ',';
    FIELD(Actor, instance); schema << ','; FIELD(Actor, cls); schema << ','; FIELD(Actor, family); schema << ',';
    FIELD(Actor, vb); schema << ','; FIELD(Actor, ib); schema << ','; FIELD(Actor, ids); schema << ',';
    FIELD(Actor, authority_count); schema << ','; FIELD(Actor, authority_crc); schema << ','; FIELD(Actor, owner_world); schema << ',';
    FIELD(Actor, history_key); schema << ','; FIELD(Actor, history_count); schema << ','; FIELD(Actor, events); schema << ',';
    FIELD(Actor, period); schema << ','; FIELD(Actor, play); schema << ','; FIELD(Actor, age_ms); schema << ',';
    FIELD(Actor, raw); schema << ','; FIELD(Actor, final); schema << "},\"pose\":{";
    FIELD(Pose, present); schema << ','; FIELD(Pose, bone_count); schema << ','; FIELD(Pose, crc); schema << ',';
    FIELD(Pose, flags); schema << ','; FIELD(Pose, world); schema << ','; FIELD(Pose, bones); schema << ',';
    FIELD(Pose, colors); schema << "}}\n";
#undef FIELD
    schema.close();
    REXLOG_WARN("actor-history: saved {}.bin + .json frames={} seconds={:.3f} camera_samples={} clock_reads={} marker={}",
        stem.string(), saved.size(), saved.back()->time - saved.front()->time, cams.size(), ticks.size(), marker);
  } catch (const std::exception& error) {
    REXLOG_WARN("actor-history: save failed: {}", error.what());
  }
  writing.store(false, std::memory_order_release);
}
}  // namespace

void Begin(uint8_t* base, const native_scene::FrameScene& scene, double now, uint64_t guest_frame) {
  current.reset();
  staged.clear();
  const bool on = REXCVAR_GET(skate3_actor_history_capture);
  if (on != tracing && clock_access.set_trace) clock_access.set_trace(on);
  tracing = on;
  capturing.store(on, std::memory_order_relaxed);
  if (!on) {
    frames.clear();
    std::lock_guard lock(camera_mutex);
    camera_samples.clear();
    return;
  }
  if (world != scene.world_identity || (!frames.empty() && now < frames.back()->time)) frames.clear();
  world = scene.world_identity;
  if (!frames.empty() && (frames.size() >= kFrames || now - frames.front()->time > kSeconds)) {
    current = std::move(frames.front()); frames.pop_front();
  } else {
    current = std::make_unique<Frame>();
  }
  while (!frames.empty() && now - frames.front()->time > kSeconds) frames.pop_front();
  *current = {};
  current->generation = guest_frame; current->world = scene.world_identity; current->time = now;
  current->smooth_entities = REXCVAR_GET(skate3_native_render_scene_smooth_entities);
  current->smooth_camera = REXCVAR_GET(skate3_native_render_scene_smooth_camera);
  guest = base; selected = false;
  for (const auto& item : scene.items) {
    if (!item.ropa && item.char_family == 0) continue;
    Staged& part = staged.emplace_back();
    part.mesh = item.mesh; part.ctx = item.ctx; part.family = item.char_family;
    part.vb = item.vb_obj; part.ib = item.ib_obj;
    Sample(item, kBoneIds, part.raw);
  }
}

void Camera(const native_scene::FrameScene& scene, const float view[16], double view_t) {
  if (!current || selected) return;
  std::copy_n(scene.cam_pos, 3, current->raw_camera); std::copy_n(scene.view_proj, 16, current->raw_vp);
  std::copy_n(view, 16, current->raw_view); current->raw_view_t = view_t;
  Select(scene.cam_pos);
}

void CameraSample(double t, const float view[16], const float proj[16], uint32_t source,
                  uint64_t guest_ticks, uint64_t trace_serial) {
  if (!capturing.load(std::memory_order_relaxed)) return;
  CamSample sample;
  sample.t = t; sample.guest_ticks = guest_ticks; sample.trace_serial = trace_serial; sample.source = source;
  std::copy_n(view, 16, sample.view); std::copy_n(proj, 16, sample.proj);
  std::lock_guard lock(camera_mutex);
  camera_samples.push_back(sample);
  while (camera_samples.size() > kCameraSamples || camera_samples.front().t < t - kSeconds - 1.0)
    camera_samples.pop_front();
}

void SetClockAccess(const ClockAccess& access) { clock_access = access; }

void Retime(const native_scene::DrawItem& item, uint64_t key, uint32_t count, double period, double play) {
  if (!current) return;
  for (uint32_t i = 0; i < current->count; ++i) {
    Actor& a = current->actors[i];
    if (a.mesh == item.mesh && a.ctx == item.ctx) {
      a.history_key = key; a.history_count = count; a.period = period; a.play = play;
      if (play != 0.0) a.events |= kPartInterpolated;
    }
  }
}

void Note(const native_scene::DrawItem& item, uint32_t part_events, float age_ms) {
  if (!current) return;
  for (uint32_t i = 0; i < current->count; ++i) {
    Actor& a = current->actors[i];
    if (a.mesh == item.mesh && a.ctx == item.ctx) {
      a.events |= part_events;
      if (age_ms >= 0.0f) a.age_ms = age_ms;
    }
  }
}

void End(const native_scene::FrameScene& scene, double camera_tick, bool retimed, const FrameState& state) {
  if (!current) {
    if (request.exchange(0)) REXLOG_WARN("actor-history: enable Actor glitch capture before reproducing the issue");
    return;
  }
  if (!selected) Select(scene.cam_pos);  // no Camera call this frame: nearest to the final camera
  current->camera_tick = camera_tick; current->retimed = retimed;
  current->camera_events = state.camera_events; current->camera_err_ms = state.camera_err_ms;
  current->camera_age_ms = state.camera_age_ms; current->camera_period_ms = state.camera_period_ms;
  current->camera_play = state.camera_play; current->render_paused = state.render_paused;
  current->stream_loads = state.stream_loads; current->stream_unloads = state.stream_unloads;
  current->stream_loads_done = state.stream_loads_done; current->stream_unloads_done = state.stream_unloads_done;
  std::copy_n(scene.cam_pos, 3, current->final_camera); std::copy_n(scene.view_proj, 16, current->final_vp);
  for (uint32_t i = 0; i < current->count; ++i) {
    Actor& actor = current->actors[i];
    for (const auto& item : scene.items) if (actor.mesh == item.mesh && actor.ctx == item.ctx) {
      Sample(item, actor.ids, actor.final); break;
    }
  }
  // Publish gap-fill can introduce a part absent from Begin's raw list.
  // Record it explicitly (raw.present=0); otherwise the very frame that
  // needs diagnosis would have no final-pose row. Keep the nearest-part cap.
  for (const auto& item : scene.items) {
    if (!item.ropa && item.char_family == 0) continue;
    bool found = false;
    for (uint32_t i = 0; i < current->count; ++i)
      found |= current->actors[i].mesh == item.mesh && current->actors[i].ctx == item.ctx;
    if (found) continue;
    Pose pose;
    Sample(item, kBoneIds, pose);
    const float candidate_distance = Distance2(pose, scene.cam_pos);
    if (!std::isfinite(candidate_distance)) continue;
    uint32_t slot = current->count;
    if (slot == kActors) {
      float farthest = candidate_distance;
      for (uint32_t i = 0; i < current->count; ++i) {
        const Actor& other = current->actors[i];
        if (const float d = Distance2(other.final.present ? other.final : other.raw, scene.cam_pos); d > farthest) {
          farthest = d; slot = i;
        }
      }
      if (slot == kActors) continue;
    } else ++current->count;
    Actor& actor = current->actors[slot]; actor = {};
    actor.mesh = item.mesh; actor.ctx = item.ctx; actor.family = item.char_family;
    actor.vb = item.vb_obj; actor.ib = item.ib_obj;
    std::copy_n(kBoneIds, kBones, actor.ids);
    native_entity::CtxInfo ident;
    if (native_entity::LookupCtx(item.ctx, &ident)) {
      actor.owner = ident.entity; actor.instance = ident.instance; actor.cls = uint32_t(ident.cls);
    }
    actor.final = pose;
  }
  frames.push_back(std::move(current));
  if (const uint32_t marker = request.exchange(0)) {
    std::lock_guard lock(writer_mutex);
    if (stopping) return;
    if (writing.exchange(true)) {
      REXLOG_WARN("actor-history: previous save still running; history continues, press again when saved");
      return;
    }
    if (writer.joinable()) writer.join(); // completed (writing was false)
    std::deque<std::unique_ptr<Frame>> saved; saved.swap(frames);
    std::vector<CamSample> cams;
    {
      std::lock_guard camera_lock(camera_mutex);
      for (const auto& sample : camera_samples)
        if (sample.t >= saved.front()->time - 0.5) cams.push_back(sample);
    }
    std::vector<TickRead> ticks;
    if (clock_access.copy_trace && !cams.empty()) {
      ticks.resize(kTickReads);
      ticks.resize(clock_access.copy_trace(cams.front().trace_serial, ticks.data(), ticks.size()));
    }
    REXLOG_WARN("actor-history: saving marker={} frames={} (prior 20 s, maximum 2048 frames/48 nearby parts)", marker, saved.size());
    writer = std::thread([saved = std::move(saved), marker, cams = std::move(cams), ticks = std::move(ticks)]() mutable {
      Save(std::move(saved), marker, std::move(cams), std::move(ticks));
    });
  }
}

void Request() {
  static std::atomic<uint32_t> serial{0};
  request.store(++serial, std::memory_order_release);
}

void Shutdown() {
  std::lock_guard lock(writer_mutex);
  stopping = true;
  if (writer.joinable()) writer.join();
}
}  // namespace skate3::actor_history
