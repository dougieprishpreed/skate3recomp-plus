#pragma once
#include <rex/cvar.h>

REXCVAR_DECLARE(bool, skate3_native_performance_skate3);
REXCVAR_DECLARE(bool, skate3_native_performance_nsv);

namespace skate3::native_scene {
// Session policy: changing queue/preparation policy requires a restart. The
// existing individual hot overrides still work, and retain all their checks.
inline bool PerformanceOptimizationsEnabled(bool nsv) {
  static const bool skate3_enabled = REXCVAR_GET(skate3_native_performance_skate3);
  static const bool nsv_enabled = REXCVAR_GET(skate3_native_performance_nsv);
  return nsv ? nsv_enabled : skate3_enabled;
}
}  // namespace skate3::native_scene
