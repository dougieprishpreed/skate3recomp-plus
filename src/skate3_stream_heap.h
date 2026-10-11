#pragma once
#include <array>
#include <cstdint>

namespace skate3::world_loading {
// Startup experiment: original allocator still owns, allocates and frees the
// backing region. Never enlarge the size field of an already-created heap.
inline bool ExpandStreamerHeapConfig(bool enabled, std::array<uint32_t, 5>& row) {
  constexpr std::array<uint32_t, 5> original{
      0x82175E68, 16, 0, 1, 39 * 1024 * 1024};
  if (!enabled || row != original) return false;
  row[4] = 64 * 1024 * 1024;
  return true;
}
// Same startup rule for AudioHeap (allocator 0x17, memory category 14
// "Audio"). Run142 entered New San Van with 29.6 of 30 MiB in use and then
// failed audio-voice allocations; the pool routes have no separate budget.
inline bool ExpandAudioHeapConfig(bool enabled, std::array<uint32_t, 5>& row) {
  constexpr std::array<uint32_t, 5> original{
      0x82175EBC, 0x17, 0, 1, 30 * 1024 * 1024};
  if (!enabled || row != original) return false;
  row[4] = 34 * 1024 * 1024;
  return true;
}
}  // namespace skate3::world_loading
