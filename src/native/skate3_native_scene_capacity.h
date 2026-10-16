#pragma once

#include <bit>
#include <cstddef>
#include <limits>

namespace skate3::native_scene {

// Keep pooled native scene buffers from reallocating for every small increase
// in submissions. This changes host capacity only; size/order/items are not
// affected. Off and unrepresentable rounded capacities use exact reserve.
inline size_t SceneItemReserveTarget(size_t capacity, size_t needed,
                                     size_t max_size, bool geometric) {
  if (!geometric || needed <= capacity) return needed;
  constexpr size_t highest_power =
      size_t{1} << (std::numeric_limits<size_t>::digits - 1);
  if (needed > highest_power) return needed;
  const size_t rounded = std::bit_ceil(needed);
  return rounded <= max_size ? rounded : needed;
}

}  // namespace skate3::native_scene
