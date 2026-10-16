#pragma once
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace skate3::native_scene {
struct SheetTwinScratch {
  static constexpr size_t kMaxTriangles = 4096, kMaxNodes = 512;
  struct Node {
    float lo[3], hi[3], nlo[3], nhi[3], edge;
    uint32_t first, last, left = 0, right = 0;
  };
  std::vector<Node> nodes;
  std::vector<uint8_t> used;
};
struct SheetTwinStats { uint64_t pairs = 0; };

template <class Tri>
bool SheetTwinMatch(const Tri& a, const Tri& b) {
  const float dot = a.n[0] * b.n[0] + a.n[1] * b.n[1] + a.n[2] * b.n[2];
  if (!(dot < -0.9f)) return false;
  const float dx = a.c[0] - b.c[0];
  const float dy = a.c[1] - b.c[1];
  const float dz = a.c[2] - b.c[2];
  const float d2 = dx * dx + dy * dy + dz * dz;
  const float along = std::fabs(dx * a.n[0] + dy * a.n[1] + dz * a.n[2]);
  const float lat_limit = 0.5f * (a.edge + b.edge);
  if (along > 0.05f || d2 - along * along > lat_limit * lat_limit) return false;
  return true;
}
template <class Tri>
uint32_t BuildSheetTwinNode(const std::vector<Tri>& tris, SheetTwinScratch& s,
                            uint32_t first, uint32_t last) {
  const uint32_t index = uint32_t(s.nodes.size());
  s.nodes.emplace_back();
  auto& node = s.nodes[index]; node.first = first; node.last = last;
  if (last - first > 16) {
    const uint32_t mid = first + (last - first) / 2;
    node.left = BuildSheetTwinNode(tris, s, first, mid);
    node.right = BuildSheetTwinNode(tris, s, mid, last);
    const auto& a = s.nodes[node.left]; const auto& b = s.nodes[node.right];
    node.edge = std::max(a.edge, b.edge);
    for (int k = 0; k < 3; ++k) {
      node.lo[k] = std::min(a.lo[k], b.lo[k]); node.hi[k] = std::max(a.hi[k], b.hi[k]);
      node.nlo[k] = std::min(a.nlo[k], b.nlo[k]); node.nhi[k] = std::max(a.nhi[k], b.nhi[k]);
    }
  } else {
    node.edge = 0;
    for (int k = 0; k < 3; ++k) {
      node.lo[k] = node.hi[k] = tris[first].c[k];
      node.nlo[k] = node.nhi[k] = tris[first].n[k];
    }
    for (uint32_t j = first; j < last; ++j) {
      node.edge = std::max(node.edge, tris[j].edge);
      for (int k = 0; k < 3; ++k) {
        node.lo[k] = std::min(node.lo[k], tris[j].c[k]); node.hi[k] = std::max(node.hi[k], tris[j].c[k]);
        node.nlo[k] = std::min(node.nlo[k], tris[j].n[k]); node.nhi[k] = std::max(node.nhi[k], tris[j].n[k]);
      }
    }
  }
  return index;
}

template <bool Count, class Tri>
size_t FindSheetTwin(const std::vector<Tri>& tris, SheetTwinScratch& s, uint32_t node_index,
                     uint32_t i, SheetTwinStats* stats) {
  const auto& node = s.nodes[node_index];
  if (node.last <= i + 1) return tris.size();
  const auto& t = tris[i];
  const float lat = 0.5f * (t.edge + node.edge);
  const double radius = (double(lat) + 0.05) * 1.00001;
  double min_dot = 0;
  for (int k = 0; k < 3; ++k) {
    if (double(t.c[k]) + radius < node.lo[k] || double(t.c[k]) - radius > node.hi[k])
      return tris.size();
    min_dot += double(t.n[k]) * (t.n[k] < 0 ? node.nhi[k] : node.nlo[k]);
  }
  if (min_dot > -0.89999) return tris.size();
  if (node.left) {
    const auto match = FindSheetTwin<Count>(tris, s, node.left, i, stats);
    if (match != tris.size()) return match;
    return FindSheetTwin<Count>(tris, s, node.right, i, stats);
  }
  for (uint32_t j = std::max(node.first, i + 1); j < node.last; ++j) {
    if (s.used[j]) continue;
    if constexpr (Count) ++stats->pairs;
    if (SheetTwinMatch(t, tris[j])) return j;
  }
  return tris.size();
}

// The tree groups CONTIGUOUS original index ranges. Traversal visits them in
// ascending order: the lowest unused matching j wins, exactly as before.
// It only rejects impossible pairs. Matching requires along<=.05 and
// d2-along^2<=lat_limit^2, hence each axis is within lat_limit+.05. Use each
// node's maximum edge and padded double endpoints. The normal dot lower bound
// also has conservative float slack. Finite limits prevent float overflow;
// unusual inputs keep the complete search. Retained scratch is hard bounded.
template <bool Count = false, class Tri>
size_t CountSheetTwins(const std::vector<Tri>& tris, SheetTwinScratch& scratch,
                      bool indexed = true, SheetTwinStats* stats = nullptr) {
  indexed = indexed && tris.size() >= 32 && tris.size() <= SheetTwinScratch::kMaxTriangles;
  if (indexed) {
    for (const auto& t : tris) {
      if (!std::isfinite(t.edge) || t.edge < 0 || t.edge > 1e12f) indexed = false;
      for (int k = 0; k < 3; ++k)
        if (!std::isfinite(t.c[k]) || std::fabs(t.c[k]) > 1e12f ||
            !std::isfinite(t.n[k]) || std::fabs(t.n[k]) > 2) indexed = false;
    }
  }
  std::vector<uint8_t> fallback;
  auto* used = &fallback;
  if (indexed) {
    if (scratch.used.capacity() < SheetTwinScratch::kMaxTriangles)
      scratch.used.reserve(SheetTwinScratch::kMaxTriangles);
    if (scratch.nodes.capacity() < SheetTwinScratch::kMaxNodes)
      scratch.nodes.reserve(SheetTwinScratch::kMaxNodes);
    used = &scratch.used;
    scratch.nodes.clear();
    BuildSheetTwinNode(tris, scratch, 0, uint32_t(tris.size()));
  }
  used->assign(tris.size(), 0);
  size_t twins = 0;
  for (size_t i = 0; i < tris.size(); ++i) {
    if ((*used)[i]) continue;
    size_t match = tris.size();
    if (indexed) {
      match = FindSheetTwin<Count>(tris, scratch, 0, uint32_t(i), stats);
    } else {
      for (size_t j = i + 1; j < tris.size(); ++j) {
        if ((*used)[j]) continue;
        if constexpr (Count) ++stats->pairs;
        if (SheetTwinMatch(tris[i], tris[j])) { match = j; break; }
      }
    }
    if (match != tris.size()) { (*used)[i] = (*used)[match] = 1; twins += 2; }
  }
  return twins;
}
}  // namespace skate3::native_scene
