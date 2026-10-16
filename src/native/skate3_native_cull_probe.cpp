// Capture ORIGINAL guest visibility inputs/results, without changing any cull
// verdict or calling guest work twice. Only armed by a manual NSV Insert.
#include "native/skate3_native_cull_probe.h"
#include "native/skate3_native_guest_read.h"
#include "skate3_world_loading.h"
#include "generated/skate3_init.h"

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <memory>
#include <mutex>
#include <utility>
#include <vector>
#include <rex/logging.h>

namespace skate3::native_cull_probe {
namespace {
constexpr uint32_t kItemLimit = 65536, kGroupLimit = 256;
constexpr uint32_t kBlockBytes = 5280;
struct Packet {
  uint64_t epoch = 0;
  uint32_t job = 0, block = 0, extra = 0, extra_count = 0;
  uint32_t boxes = 0, contribution = 0, output = 0, count = 0, rounded = 0;
  std::vector<uint8_t> descriptor, block_data, box_data, contribution_data, extra_data, results;
};
std::atomic<uint32_t> requested{0}, active{0};
std::mutex mutex;
uint64_t epoch = 0;
uint32_t reserved_items = 0, reserved_groups = 0, dropped = 0;
std::vector<Packet> packets;
uint32_t Word(const uint8_t* p) {
  return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3];
}
bool Copy(uint8_t* base, uint32_t address, uint32_t bytes, std::vector<uint8_t>& dst) {
  if (address < 0x10000 || uint64_t(address) + bytes > (uint64_t(1) << 32)) return false;
  dst.resize(bytes);
  return skate3::native_scene::GuestTryCopy(dst.data(), base + address, bytes);
}
std::unique_ptr<Packet> Before(uint8_t* base, uint32_t job, uint32_t block,
                              uint32_t extra, uint32_t extra_count) {
  if (!active.load(std::memory_order_acquire) ||
      !skate3::world_loading::IsSkate2Gameplay()) return {};
  auto packet = std::make_unique<Packet>();
  packet->job = job; packet->block = block; packet->extra = extra;
  packet->extra_count = extra_count;
  {
    std::lock_guard lock(mutex);
    if (!active.load(std::memory_order_relaxed)) return {};
    packet->epoch = epoch;
  }
  const auto failed = [&]() {
    std::lock_guard lock(mutex);
    if (active.load(std::memory_order_relaxed) && packet->epoch == epoch) ++dropped;
    return std::unique_ptr<Packet>{};
  };
  if (extra_count > 32 || !Copy(base, job, 20, packet->descriptor)) return failed();
  const auto* d = packet->descriptor.data();
  packet->boxes = Word(d); packet->contribution = Word(d + 4); packet->output = Word(d + 12);
  packet->count = Word(d + 16) >> 16;
  packet->rounded = (packet->count + 3) & ~3u;
  if (!packet->count) return {};
  if (packet->rounded > 8192) return failed();
  {
    std::lock_guard lock(mutex);
    if (!active.load(std::memory_order_relaxed) || packet->epoch != epoch) return {};
    if (reserved_groups >= kGroupLimit || packet->rounded > kItemLimit - reserved_items) {
      ++dropped; return {};
    }
    ++reserved_groups; reserved_items += packet->rounded;
  }
  if (!Copy(base, block, kBlockBytes, packet->block_data) ||
      !Copy(base, packet->boxes, packet->rounded * 32, packet->box_data) ||
      (packet->contribution && !Copy(base, packet->contribution, packet->rounded * 16,
                                     packet->contribution_data)) ||
      (extra_count && !Copy(base, extra, extra_count * 480, packet->extra_data))) {
    std::lock_guard lock(mutex);
    if (packet->epoch == epoch) ++dropped;
    return {};
  }
  return packet;
}
void After(uint8_t* base, std::unique_ptr<Packet> packet) {
  if (!packet) return;
  if (!Copy(base, packet->output, packet->rounded * 2, packet->results)) {
    std::lock_guard lock(mutex);
    if (packet->epoch == epoch) ++dropped;
    return;
  }
  std::lock_guard lock(mutex);
  if (active.load(std::memory_order_relaxed) && packet->epoch == epoch)
    packets.push_back(std::move(*packet));
}
void U32(std::ofstream& out, uint32_t n) {
  const char b[4] = {char(n), char(n >> 8), char(n >> 16), char(n >> 24)};
  out.write(b, 4);
}
void Bytes(std::ofstream& out, const std::vector<uint8_t>& data) {
  U32(out, uint32_t(data.size()));
  if (!data.empty()) out.write(reinterpret_cast<const char*>(data.data()), std::streamsize(data.size()));
}
void Dump(uint32_t marker, uint64_t frame, uint32_t lost, const std::vector<Packet>& data) {
  char path[160];
  std::snprintf(path, sizeof(path), "logs/cull_%u_%lld_%llu.bin", marker,
                static_cast<long long>(std::time(nullptr)), static_cast<unsigned long long>(frame));
  std::ofstream out(path, std::ios::binary);
  out.write("NSVCULL1", 8);
  U32(out, 1); U32(out, marker); U32(out, uint32_t(frame)); U32(out, uint32_t(frame >> 32));
  U32(out, uint32_t(data.size())); U32(out, lost);
  uint32_t total = 0;
  for (const auto& p : data) {
    U32(out, p.job); U32(out, p.block); U32(out, p.extra); U32(out, p.extra_count);
    U32(out, p.boxes); U32(out, p.contribution); U32(out, p.output);
    U32(out, p.count); U32(out, p.rounded);
    Bytes(out, p.descriptor); Bytes(out, p.block_data); Bytes(out, p.box_data);
    Bytes(out, p.contribution_data); Bytes(out, p.extra_data); Bytes(out, p.results);
    total += p.count;
  }
  out.flush();
  if (out) REXLOG_WARN("native-cull-probe: marker={} groups={} items={} dropped={} -> {}",
                       marker, data.size(), total, lost, path);
  else REXLOG_WARN("native-cull-probe: could not write {}", path);
}
}  // namespace
void Request(uint32_t marker) {
  if (skate3::world_loading::IsSkate2Gameplay())
    requested.store(std::max(marker, 1u), std::memory_order_release);
}
void OnGuestFrame(uint64_t frame) {
  if (!active.load(std::memory_order_acquire) && !requested.load(std::memory_order_acquire)) return;
  const bool nsv = skate3::world_loading::IsSkate2Gameplay();
  uint32_t completed = 0, lost = 0;
  std::vector<Packet> data;
  {
    std::lock_guard lock(mutex);
    completed = active.exchange(0, std::memory_order_acq_rel);
    data.swap(packets); lost = dropped;
  }
  if (completed && nsv) Dump(completed, frame, lost, data);
  const uint32_t next = requested.exchange(0, std::memory_order_acq_rel);
  if (next && nsv) {
    std::lock_guard lock(mutex);
    ++epoch; reserved_items = 0; reserved_groups = 0; dropped = 0;
    active.store(next, std::memory_order_release);
  }
}
}  // namespace skate3::native_cull_probe

// Preserve the entire original PPC call and its outputs. No plane, threshold,
// contribution flag, visibility result, range, pointer or guest register edits.
extern "C" REX_FUNC(sub_82816A40) {
  auto packet = skate3::native_cull_probe::Before(
      base, ctx.r3.u32, ctx.r4.u32, ctx.r5.u32, ctx.r6.u32);
  __imp__sub_82816A40(ctx, base);
  if (packet) skate3::native_cull_probe::After(base, std::move(packet));
}
