#pragma once

#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

namespace skate3::native_scene {

// Imported low-detail pedestrian bodies omit both normals and tangent frames.
// Restrict repair to that missing-data case in New San Van. Authored normals,
// native Skate 3 actors and simulated cloth keep their existing decode.
template <class Item>
inline bool MissingPedestrianNormals(bool skate2_gameplay, const Item& item) {
  return skate2_gameplay && item.char_family == 3 && item.skinned &&
         !item.ropa && !item.cloth_quads && item.normal_fmt == 0 &&
         item.tb_fmt == 0 && item.tan_fmt == 0;
}

struct MeshNormalResult {
  bool valid = false;
  uint32_t generated_vertices = 0;
};

// Native scene vertices have 14 floats: position at 0, normal at 9. Generate
// area-weighted bind-pose normals from the same big-endian indices/draw ranges
// as the upload, then let the existing vertex shader skin them. Validate the
// complete mesh before writing; unsupported/invalid geometry keeps its fallback.
template <class Draw>
inline MeshNormalResult ReconstructBodyNormals(
    std::span<float> vertices, std::span<const uint8_t> indices,
    std::span<const Draw> draws) {
  constexpr size_t kStride = 14;
  if (vertices.empty() || vertices.size() % kStride || indices.size() % 2 ||
      draws.empty()) return {};
  const size_t count = vertices.size() / kStride;
  const size_t index_count = indices.size() / 2;
  for (size_t v = 0; v < count; ++v)
    for (size_t c = 0; c < 3; ++c)
      if (!std::isfinite(vertices[v * kStride + c])) return {};
  const auto index = [&](size_t i, uint32_t base) {
    return uint64_t(base) + (uint32_t(indices[i * 2]) << 8) + indices[i * 2 + 1];
  };
  for (const auto& draw : draws) {
    if (draw.prim != 4 || draw.index_count % 3 ||
        uint64_t(draw.start_index) + draw.index_count > index_count) return {};
    for (uint64_t i = draw.start_index; i < uint64_t(draw.start_index) + draw.index_count; ++i)
      if (index(size_t(i), draw.base_vertex) >= count) return {};
  }

  using Vec = std::array<double, 3>;
  std::vector<Vec> sum(count), first(count);
  const auto length2 = [](const Vec& n) {
    return n[0] * n[0] + n[1] * n[1] + n[2] * n[2];
  };
  for (const auto& draw : draws) {
    for (uint64_t i = draw.start_index; i < uint64_t(draw.start_index) + draw.index_count; i += 3) {
      const size_t a = size_t(index(size_t(i), draw.base_vertex));
      const size_t b = size_t(index(size_t(i + 1), draw.base_vertex));
      const size_t c = size_t(index(size_t(i + 2), draw.base_vertex));
      Vec ab{}, ac{};
      for (size_t k = 0; k < 3; ++k) {
        ab[k] = double(vertices[b * kStride + k]) - vertices[a * kStride + k];
        ac[k] = double(vertices[c * kStride + k]) - vertices[a * kStride + k];
      }
      const Vec n{ab[1] * ac[2] - ab[2] * ac[1],
                  ab[2] * ac[0] - ab[0] * ac[2],
                  ab[0] * ac[1] - ab[1] * ac[0]};
      if (length2(n) <= 1e-24) continue;
      for (size_t v : {a, b, c}) {
        if (length2(first[v]) == 0.0) first[v] = n;
        for (size_t k = 0; k < 3; ++k) sum[v][k] += n[k];
      }
    }
  }
  MeshNormalResult result{true, 0};
  for (size_t v = 0; v < count; ++v) {
    // Opposing faces can cancel. Use a valid face there; an unreferenced or
    // wholly degenerate vertex stays untouched and retains the shader fallback.
    const Vec& n = length2(sum[v]) > 1e-24 ? sum[v] : first[v];
    const double len2 = length2(n);
    if (len2 <= 1e-24) continue;
    const double inv = 1.0 / std::sqrt(len2);
    for (size_t k = 0; k < 3; ++k) vertices[v * kStride + 9 + k] = float(n[k] * inv);
    ++result.generated_vertices;
  }
  return result;
}

}  // namespace skate3::native_scene
