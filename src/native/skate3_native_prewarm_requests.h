#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

namespace skate3::native_scene {

// The result's priority and its ownership of an in-flight key are separate.
// Dynamic jobs also bypass the commit cap, but never own a static miss key.
struct PrewarmMissRequest {
  uint32_t mesh = 0, tex = 0;
  uint64_t words = 0;
  template <class Entry> static PrewarmMissRequest From(const Entry& e) {
    return e.miss ? PrewarmMissRequest{e.mesh, e.tex, e.wkey} : PrewarmMissRequest{};
  }
  template <class MeshSet, class TexSet, class WordsSet>
  void Release(MeshSet& meshes, TexSet& textures, WordsSet& fetches) const {
    if (mesh) meshes.erase(mesh);
    if (tex) textures.erase(tex);
    if (words) fetches.erase(words);
  }
};

struct PrewarmRequestScratch {
  static constexpr size_t kMaxEntries = 65536;
  std::vector<std::pair<uint32_t, uint32_t>> misses;
};

// Both requests are still queued under the SAME queue mutex. Keep the visible
// FIFO request, give it the larger retry budget, and remove the redundant
// speculative request. It will parse and fingerprint the live payload normally
// when selected. No active/decoded result, GPU resource or lifetime is reused.
// Core-proof jobs retain their own lifetime/completion and are never removed.
// Stable erase preserves the remaining registration LIFO order. Scratch is
// bounded and reused; only the render thread calls this under the queue lock.
template <class Entry>
uint32_t CoalesceQueuedMeshRequests(std::vector<Entry>& prewarm,
                                    std::vector<Entry>& misses,
                                    PrewarmRequestScratch& scratch) {
  scratch.misses.clear();
  if (prewarm.empty() || misses.empty() ||
      prewarm.size() > PrewarmRequestScratch::kMaxEntries ||
      misses.size() > PrewarmRequestScratch::kMaxEntries) return 0;
  const auto plain_mesh = [](const Entry& e) {
    return e.mesh && !e.tex && !e.wkey && !e.cube && !e.ui &&
           !e.core_only && !e.core_generation;
  };
  // reserve() requests the exact bounded count, without unbounded growth when
  // a large backlog is reached after a sequence of smaller captures.
  if (scratch.misses.capacity() < misses.size()) scratch.misses.reserve(misses.size());
  for (uint32_t i = 0; i < misses.size(); ++i)
    if (misses[i].miss && plain_mesh(misses[i])) scratch.misses.emplace_back(misses[i].mesh, i);
  if (scratch.misses.empty()) return 0;
  std::sort(scratch.misses.begin(), scratch.misses.end());
  const auto end = std::remove_if(prewarm.begin(), prewarm.end(), [&](const Entry& e) {
    if (e.miss || !plain_mesh(e)) return false;
    const auto it = std::lower_bound(scratch.misses.begin(), scratch.misses.end(),
                                    std::pair<uint32_t, uint32_t>{e.mesh, 0});
    if (it == scratch.misses.end() || it->first != e.mesh) return false;
    auto& pending = misses[it->second];
    pending.retries = std::max(pending.retries, e.retries);
    return true;
  });
  const auto removed = uint32_t(prewarm.end() - end);
  prewarm.erase(end, prewarm.end());
  return removed;
}

}  // namespace skate3::native_scene
