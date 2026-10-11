#pragma once
#include <array>
#include <cmath>

namespace skate3::sim_extent {
// X/Z query [minX,minZ,maxX,maxZ]. Preserve the stock five-unit inset
// while adapting the 100-unit grid assumption to BAM's 200-unit grid.
inline bool Expand(std::array<float, 4>& bounds) {
  for (float v : bounds) if (!std::isfinite(v)) return false;
  if (std::abs((bounds[2] - bounds[0]) - 190.0f) > 0.01f ||
      std::abs((bounds[3] - bounds[1]) - 190.0f) > 0.01f) return false;
  bounds[0] -= 100.0f;
  bounds[1] -= 100.0f;
  bounds[2] += 100.0f;
  bounds[3] += 100.0f;
  return true;
}
}
