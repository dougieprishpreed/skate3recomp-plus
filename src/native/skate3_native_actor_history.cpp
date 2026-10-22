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

#include <rex/cvar.h>
#include <rex/logging.h>

#include "native/skate3_native_entity.h"
#include "skate3_native_scene.h"

REXCVAR_DEFINE_BOOL(skate3_actor_history_capture, false, "Skate 3",
    "Keep up to 8 seconds of actor/camera history in bounded memory. "
    "Ctrl+Insert saves it asynchronously; no disk writes until requested")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);
REXCVAR_DECLARE(bool, skate3_native_render_scene_smooth_entities);
REXCVAR_DECLARE(bool, skate3_native_render_scene_smooth_camera);

namespace skate3::actor_history {
namespace {
constexpr size_t kActors = 48, kBones = 8, kFrames = 2048;
constexpr double kSeconds = 8.0;
struct Pose {
  uint32_t present = 0, bone_count = 0, crc = 0, flags = 0;
  float world[16]{}, bones[kBones][12]{}, colors[8]{};
};
struct Actor {
  uint32_t mesh = 0, ctx = 0, owner = 0, instance = 0, cls = 0, family = 0;
  uint32_t vb = 0, ib = 0, ids[kBones]{}, authority_count = 0, authority_crc = 0;
  uint64_t history_key = 0;
  uint32_t history_count = 0, reserved = 0;
  double period = 0, play = 0;
  float owner_world[12]{};
  Pose raw{}, final{};
};
struct Frame {
  uint64_t generation = 0, world = 0;
  double time = 0, camera_tick = 0;
  uint32_t count = 0, smooth_entities = 0, smooth_camera = 0, retimed = 0;
  float raw_camera[3]{}, final_camera[3]{}, raw_vp[16]{}, final_vp[16]{};
  Actor actors[kActors]{};
};
static_assert(std::is_trivially_copyable_v<Frame>);
static_assert(sizeof(Frame) * kFrames < 128 * 1024 * 1024);
std::deque<std::unique_ptr<Frame>> frames;
std::unique_ptr<Frame> current;
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
      (item.retained ? 16u : 0u) | (uint32_t(item.dbg_src) << 8);
  std::copy_n(item.world, 16, pose.world);
  std::copy_n(item.char_rows + 48, 8, pose.colors);
  for (size_t i = 0; i < kBones; ++i)
    if (ids[i] < pose.bone_count) std::copy_n(item.bones.data() + ids[i] * 12, 12, pose.bones[i]);
}

void Save(std::deque<std::unique_ptr<Frame>> saved, uint32_t marker) {
  try {
    const std::filesystem::path directory = "logs";
    std::filesystem::create_directories(directory);
    const auto tick = uint64_t(saved.back()->time * 1000000.0);
    const auto stem = directory / ("actor_history_" + std::to_string(marker) + "_" + std::to_string(tick));
    std::ofstream binary(stem.string() + ".bin", std::ios::binary);
    binary.exceptions(std::ios::badbit | std::ios::failbit);
    binary.write("S3ACT219", 8);
    const uint32_t header[] = {uint32_t(saved.size()), uint32_t(sizeof(Frame)), uint32_t(sizeof(Actor)), uint32_t(sizeof(Pose))};
    binary.write(reinterpret_cast<const char*>(header), sizeof(header));
    for (const auto& frame : saved) binary.write(reinterpret_cast<const char*>(frame.get()), sizeof(Frame));
    binary.close();
    // Explicit offsets make the POD dump readable across compilers/platforms.
    std::ofstream schema(stem.string() + ".json");
    schema.exceptions(std::ios::badbit | std::ios::failbit);
    schema << "{\"version\":219,\"byte_order\":\"little\",\"header_bytes\":24,\"max_actors\":" << kActors
           << ",\"bone_slots\":" << kBones << ",\"window_seconds\":8,\"frame\":{";
#define FIELD(T, name) schema << "\"" #name "\":" << offsetof(T, name)
    FIELD(Frame, generation); schema << ','; FIELD(Frame, world); schema << ','; FIELD(Frame, time); schema << ',';
    FIELD(Frame, camera_tick); schema << ','; FIELD(Frame, count); schema << ','; FIELD(Frame, smooth_entities); schema << ',';
    FIELD(Frame, smooth_camera); schema << ','; FIELD(Frame, retimed); schema << ','; FIELD(Frame, raw_camera); schema << ',';
    FIELD(Frame, final_camera); schema << ','; FIELD(Frame, raw_vp); schema << ','; FIELD(Frame, final_vp); schema << ',';
    FIELD(Frame, actors); schema << "},\"actor\":{";
    FIELD(Actor, mesh); schema << ','; FIELD(Actor, ctx); schema << ','; FIELD(Actor, owner); schema << ',';
    FIELD(Actor, instance); schema << ','; FIELD(Actor, cls); schema << ','; FIELD(Actor, family); schema << ',';
    FIELD(Actor, vb); schema << ','; FIELD(Actor, ib); schema << ','; FIELD(Actor, ids); schema << ',';
    FIELD(Actor, authority_count); schema << ','; FIELD(Actor, authority_crc); schema << ','; FIELD(Actor, owner_world); schema << ',';
    FIELD(Actor, history_key); schema << ','; FIELD(Actor, history_count); schema << ','; FIELD(Actor, period); schema << ',';
    FIELD(Actor, play); schema << ','; FIELD(Actor, raw); schema << ','; FIELD(Actor, final); schema << "},\"pose\":{";
    FIELD(Pose, present); schema << ','; FIELD(Pose, bone_count); schema << ','; FIELD(Pose, crc); schema << ',';
    FIELD(Pose, flags); schema << ','; FIELD(Pose, world); schema << ','; FIELD(Pose, bones); schema << ',';
    FIELD(Pose, colors); schema << "}}\n";
#undef FIELD
    schema.close();
    REXLOG_WARN("actor-history: saved {}.bin + .json frames={} seconds={:.3f} marker={}",
        stem.string(), saved.size(), saved.back()->time - saved.front()->time, marker);
  } catch (const std::exception& error) {
    REXLOG_WARN("actor-history: save failed: {}", error.what());
  }
  writing.store(false, std::memory_order_release);
}
}  // namespace

void Begin(uint8_t* base, const native_scene::FrameScene& scene, double now, uint64_t guest_frame) {
  current.reset();
  if (!REXCVAR_GET(skate3_actor_history_capture)) {
    frames.clear();
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
  std::copy_n(scene.cam_pos, 3, current->raw_camera); std::copy_n(scene.view_proj, 16, current->raw_vp);
  std::array<std::pair<float, size_t>, kActors> nearest;
  nearest.fill({INFINITY, 0});
  size_t count = 0;
  for (size_t i = 0; i < scene.items.size(); ++i) {
    const auto& item = scene.items[i];
    if (!item.ropa && item.char_family == 0) continue;
    const bool skin = item.skinned && item.bones.size() >= 12;
    float distance = 0;
    for (size_t j = 0; j < 3; ++j) {
      const float d = (skin ? item.bones[j * 4 + 3] : item.world[12 + j]) - scene.cam_pos[j];
      distance += d * d;
    }
    if (!std::isfinite(distance)) continue;
    auto candidate = std::make_pair(distance, i);
    for (size_t j = 0; j < kActors; ++j) if (candidate < nearest[j]) std::swap(candidate, nearest[j]);
    count = std::min(count + 1, kActors);
  }
  current->count = uint32_t(count);
  constexpr uint32_t ids[kBones] = {0, 1, 2, 4, 8, 16, 32, 64};
  for (size_t i = 0; i < count; ++i) {
    Actor& actor = current->actors[i]; const auto& item = scene.items[nearest[i].second];
    actor.mesh = item.mesh; actor.ctx = item.ctx; actor.family = item.char_family;
    actor.vb = item.vb_obj; actor.ib = item.ib_obj; std::copy_n(ids, kBones, actor.ids);
    native_entity::CtxInfo ident;
    if (native_entity::LookupCtx(item.ctx, &ident)) {
      actor.owner = ident.entity; actor.instance = ident.instance; actor.cls = uint32_t(ident.cls);
      native_entity::ReadEntityWorldRows(base, item.ctx, actor.owner_world);
      float palette[96 * 12]; actor.authority_count = native_entity::ServeInstancePalette(base, item.ctx, palette, 96);
      actor.authority_crc = Fingerprint(palette, actor.authority_count * 12);
    }
    Sample(item, actor.ids, actor.raw);
  }
}

void Retime(const native_scene::DrawItem& item, uint64_t key, uint32_t count, double period, double play) {
  if (!current) return;
  for (uint32_t i = 0; i < current->count; ++i) {
    Actor& a = current->actors[i];
    if (a.mesh == item.mesh && a.ctx == item.ctx) {
      a.history_key = key; a.history_count = count; a.period = period; a.play = play;
    }
  }
}

void End(const native_scene::FrameScene& scene, double camera_tick, bool retimed) {
  if (!current) {
    if (request.exchange(0)) REXLOG_WARN("actor-history: enable Actor glitch capture before reproducing the issue");
    return;
  }
  current->camera_tick = camera_tick; current->retimed = retimed;
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
    const auto distance = [&](const float* position) {
      float value = 0;
      for (size_t j = 0; j < 3; ++j) { const float d = position[j] - scene.cam_pos[j]; value += d * d; }
      return value;
    };
    const bool skin = item.skinned && item.bones.size() >= 12;
    const float position[3] = {skin ? item.bones[3] : item.world[12],
                              skin ? item.bones[7] : item.world[13],
                              skin ? item.bones[11] : item.world[14]};
    const float candidate_distance = distance(position);
    if (!std::isfinite(candidate_distance)) continue;
    uint32_t slot = current->count;
    if (slot == kActors) {
      float farthest = candidate_distance;
      for (uint32_t i = 0; i < current->count; ++i) {
        const Pose& pose = current->actors[i].final.present ? current->actors[i].final : current->actors[i].raw;
        const float p[3] = {pose.flags & 1 ? pose.bones[0][3] : pose.world[12],
                            pose.flags & 1 ? pose.bones[0][7] : pose.world[13],
                            pose.flags & 1 ? pose.bones[0][11] : pose.world[14]};
        if (const float d = distance(p); d > farthest) { farthest = d; slot = i; }
      }
      if (slot == kActors) continue;
    } else ++current->count;
    Actor& actor = current->actors[slot]; actor = {};
    actor.mesh = item.mesh; actor.ctx = item.ctx; actor.family = item.char_family;
    actor.vb = item.vb_obj; actor.ib = item.ib_obj;
    constexpr uint32_t ids[kBones] = {0, 1, 2, 4, 8, 16, 32, 64};
    std::copy_n(ids, kBones, actor.ids);
    native_entity::CtxInfo ident;
    if (native_entity::LookupCtx(item.ctx, &ident)) {
      actor.owner = ident.entity; actor.instance = ident.instance; actor.cls = uint32_t(ident.cls);
    }
    Sample(item, actor.ids, actor.final);
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
    REXLOG_WARN("actor-history: saving marker={} frames={} (prior8s, maximum2048 frames/48 nearby parts)", marker, saved.size());
    writer = std::thread([saved = std::move(saved), marker]() mutable { Save(std::move(saved), marker); });
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
