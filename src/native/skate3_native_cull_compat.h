#pragma once

#include <cmath>
#include <array>
#include <algorithm>
#include <bit>
#include <rex/ppc/context.h>

#include "skate3_world_loading.h"
#include "native/skate3_native_guest_read.h"

// Called once per vectorized job, immediately after loading v55 (the area
// projection direction), before the original routine splats/absolutes it.
// NSV's job block can retain {0,0,1} while its world-space camera planes rotate.
// S2 planar meshes then have zero projected area along that fixed axis and
// disappear beyond the contribution engage distance, even when facing the view.
// Use this job's own normalized near-plane normal, already loaded into v20,
// v19 and v18. Preserve all six planes, extra occlusion volumes, area records,
// thresholds, distances, original instructions/calls and shared guest memory.
inline void Skate3NsvCullContributionDirection(PPCContext& ctx) {
  if (!skate3::world_loading::IsSkate2Gameplay() || !ctx.r27.u32 || !ctx.r6.u32)
    return;
  const float x = ctx.v20.f32[0], y = ctx.v19.f32[0], z = ctx.v18.f32[0];
  const float length_sq = x * x + y * y + z * z;
  // Unexpected/non-camera blocks retain their original direction. Actual
  // perspective camera planes are normalized (including widened side planes).
  if (!std::isfinite(length_sq) || length_sq < 0.99f || length_sq > 1.01f)
    return;
  const float inv_length = 1.0f / std::sqrt(length_sq);
  // VMX float4 words are stored in reverse lane order on the host. Leave W.
  ctx.v55.f32[3] = x * inv_length;
  ctx.v55.f32[2] = y * inv_length;
  ctx.v55.f32[1] = z * inv_length;
}

// A centre-view direction alone underestimates surfaces near a perspective
// frustum's sides. Bound the perspective area over the WHOLE world-space box:
// sum(area[i] * max(abs(box[i] - eye[i]))) / nearest_view_depth. The original
// routine then applies its existing projection_scale / nearest_depth^2. This
// bounds the directional numerator and reciprocal depth over every box point,
// including off-axis surfaces. It can only raise the original size estimate;
// frustum/occlusion verdicts and the configured size/distance rules remain.
class Skate3NsvCullProjectionBound {
 public:
  Skate3NsvCullProjectionBound(uint8_t* base, uint32_t block) {
    if (!skate3::world_loading::IsSkate2Gameplay() || block < 0x10000 ||
        block >= 0xE0000000u - 5280) return;
    uint32_t raw[32];  // eye, old axis, six original planes
    if (!skate3::native_scene::GuestTryCopy(raw, base + block + 5120, sizeof(raw))) return;
    float v[32];
    for (size_t i = 0; i < 32; ++i) {
      v[i] = std::bit_cast<float>(__builtin_bswap32(raw[i]));
      if (!std::isfinite(v[i])) return;
    }
    if (v[3] != 1.0f) return;
    // Perspective side planes pass through this job's eye. Orthographic and
    // unexpected jobs keep the original estimate; never infer a global camera.
    for (size_t off : {8u, 12u}) {
      const float side = v[off]*v[0] + v[off+1]*v[1] + v[off+2]*v[2] + v[off+3];
      if (std::fabs(side) > 0.05f) return;
    }
    const float n2 = v[24]*v[24] + v[25]*v[25] + v[26]*v[26];
    if (n2 < .99f || n2 > 1.01f) return;
    for (size_t i = 0; i < 3; ++i) { eye_[i] = v[i]; near_[i] = v[24+i]; }
    near_[3] = v[27];
    valid_ = true;
  }
  std::array<float,4> Area(const float* min_x, const float* min_y, const float* min_z,
                           const float* max_x, const float* max_y, const float* max_z,
                           const float* area_x, const float* area_y, const float* area_z) const {
    std::array<float,4> result{};
    if (!valid_) return result;
    const float* low[3] = {min_x,min_y,min_z};
    const float* high[3] = {max_x,max_y,max_z};
    const float* area[3] = {area_x,area_y,area_z};
    for (size_t lane = 0; lane < 4; ++lane) {
      float depth = near_[3], numerator = 0;
      bool sane = true;
      for (size_t i = 0; i < 3; ++i) {
        const float a=low[i][lane], b=high[i][lane], c=area[i][lane];
        if (!std::isfinite(a) || !std::isfinite(b) || !std::isfinite(c) || a>b || c<0) {
          sane = false; break;
        }
        depth += near_[i] * (near_[i]>=0 ? a : b);
        numerator += c * (std::max)(std::fabs(a-eye_[i]),std::fabs(b-eye_[i]));
      }
      if (sane && std::isfinite(depth) && std::isfinite(numerator))
        result[lane] = numerator / (std::max)(depth,1.0f);
    }
    return result;
  }
  static void Apply(float* projected_area, const std::array<float,4>& bound) {
    for (size_t i=0; i<4; ++i)
      if (bound[i] > 0.0f && std::isfinite(projected_area[i]) && std::isfinite(bound[i]))
        projected_area[i] = (std::max)(projected_area[i],bound[i]);
  }
 private:
  bool valid_ = false;
  float eye_[3]{}, near_[4]{};
};
