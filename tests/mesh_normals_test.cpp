#include "native/mesh_normals.h"

#include <algorithm>
#include <cassert>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>

using namespace skate3::native_scene;

struct Draw { uint32_t prim, base_vertex, start_index, index_count; };
struct Item {
  uint8_t char_family = 3;
  bool skinned = true, ropa = false, cloth_quads = false;
  uint32_t normal_fmt = 0, tb_fmt = 0, tan_fmt = 0;
};

std::vector<float> Vertices(std::initializer_list<std::array<float, 3>> positions) {
  std::vector<float> v;
  for (const auto& p : positions) {
    for (size_t k = 0; k < 14; ++k) v.push_back(float(v.size() + 1) / 100.0f);
    std::copy(p.begin(), p.end(), v.end() - 14);
    std::fill(v.end() - 5, v.end() - 2, 0.0f);
  }
  return v;
}

std::vector<uint8_t> Indices(std::initializer_list<uint16_t> values) {
  std::vector<uint8_t> indices;
  for (uint16_t i : values) {
    indices.push_back(uint8_t(i >> 8));
    indices.push_back(uint8_t(i));
  }
  return indices;
}

MeshNormalResult Repair(std::vector<float>& v, const std::vector<uint8_t>& i,
                        const std::vector<Draw>& d) {
  return ReconstructBodyNormals(std::span<float>(v), std::span<const uint8_t>(i),
                               std::span<const Draw>(d));
}

void CheckOtherFields(const std::vector<float>& v, const std::vector<float>& before) {
  assert(v.size() == before.size());
  for (size_t i = 0; i < v.size(); ++i)
    if (i % 14 < 9 || i % 14 > 11)
      assert(std::memcmp(&v[i], &before[i], sizeof(float)) == 0);
}

void TestEligibility() {
  Item item;
  assert(MissingPedestrianNormals(true, item));
  assert(!MissingPedestrianNormals(false, item));
  for (auto field : {&Item::normal_fmt, &Item::tb_fmt, &Item::tan_fmt}) {
    item.*field = 16;
    assert(!MissingPedestrianNormals(true, item));
    item.*field = 0;
  }
  for (auto field : {&Item::ropa, &Item::cloth_quads}) {
    item.*field = true;
    assert(!MissingPedestrianNormals(true, item));
    item.*field = false;
  }
  item.skinned = false;
  assert(!MissingPedestrianNormals(true, item));
  item.skinned = true;
  for (uint8_t family : {0, 1, 2, 4, 5, 6}) {
    item.char_family = family;
    assert(!MissingPedestrianNormals(true, item));
  }
}

void TestGeometry() {
  // Unequal perpendicular triangles: shared normals use actual triangle area.
  auto v = Vertices({{0, 0, 0}, {2, 0, 0}, {0, 1, 0}, {0, 0, 2}, {9, 9, 9}});
  const auto before = v;
  const auto indices = Indices({0, 1, 2, 0, 3, 1});
  auto result = Repair(v, indices, {{4, 0, 0, 6}});
  assert(result.valid && result.generated_vertices == 4);
  CheckOtherFields(v, before);
  assert(std::fabs(v[10] - 2.0 / std::sqrt(5.0)) < 1e-6);
  assert(std::fabs(v[11] - 1.0 / std::sqrt(5.0)) < 1e-6);
  assert(v[2 * 14 + 11] == 1 && v[3 * 14 + 10] == 1);
  assert(std::equal(v.end() - 14, v.end(), before.end() - 14));

  // Base vertex and start offset are part of the draw, not raw global indices.
  auto based = Vertices({{100, 100, 100}, {0, 0, 0}, {1, 0, 0}, {0, 1, 0}});
  result = Repair(based, Indices({999, 0, 1, 2}), {{4, 1, 1, 3}});
  assert(result.valid && result.generated_vertices == 3);
  assert(based[11] == 0 && based[14 + 11] == 1);

  // Degenerate triangles and opposing faces must never produce NaNs.
  auto opposed = Vertices({{0, 0, 0}, {1, 0, 0}, {0, 1, 0}});
  result = Repair(opposed, Indices({0, 0, 1, 0, 1, 2, 0, 2, 1}), {{4, 0, 0, 9}});
  assert(result.valid && result.generated_vertices == 3);
  for (size_t i = 0; i < 3; ++i) assert(opposed[i * 14 + 11] == 1);
  auto degenerate = Vertices({{0, 0, 0}, {1, 0, 0}, {2, 0, 0}});
  const auto degenerate_before = degenerate;
  result = Repair(degenerate, Indices({0, 1, 2}), {{4, 0, 0, 3}});
  assert(result.valid && result.generated_vertices == 0 && degenerate == degenerate_before);

  // An invalid later range cannot leave a partially repaired vertex buffer.
  for (Draw bad : {Draw{5, 0, 0, 3}, Draw{4, 0, 0, 2}, Draw{4, 0, 4, 3},
                   Draw{4, 0xffffffffu, 0, 3}}) {
    auto invalid = before;
    result = Repair(invalid, indices, {{4, 0, 0, 3}, bad});
    assert(!result.valid && invalid == before);
  }
  auto invalid = before;
  result = Repair(invalid, Indices({0, 1, 2, 0, 1, 5}), {{4, 0, 0, 6}});
  assert(!result.valid && invalid == before);
  invalid[14] = std::numeric_limits<float>::quiet_NaN();
  const auto invalid_before = invalid;
  result = Repair(invalid, indices, {{4, 0, 0, 6}});
  assert(!result.valid && std::memcmp(invalid.data(), invalid_before.data(), invalid.size() * 4) == 0);
}

void TestAsset(const std::filesystem::path& path) {
  std::ifstream file(path, std::ios::binary);
  const auto read = [&](void* p, size_t n) {
    file.read(static_cast<char*>(p), std::streamsize(n));
    assert(file.good());
  };
  std::array<uint32_t, 4> header{};
  read(header.data(), sizeof(header));
  const auto [nv, ni, nd, expected_count] = header;
  std::vector<float> vertices(size_t(nv) * 14), expected(size_t(nv) * 3);
  std::vector<uint8_t> indices(size_t(ni) * 2);
  std::vector<Draw> draws(nd);
  read(vertices.data(), vertices.size() * 4);
  read(indices.data(), indices.size());
  read(draws.data(), draws.size() * sizeof(Draw));
  read(expected.data(), expected.size() * 4);
  const auto before = vertices;
  const auto result = Repair(vertices, indices, draws);
  assert(result.valid && result.generated_vertices == expected_count);
  CheckOtherFields(vertices, before);
  for (size_t v = 0; v < nv; ++v) {
    float length2 = 0;
    for (size_t c = 0; c < 3; ++c) {
      const float n = vertices[v * 14 + 9 + c];
      assert(std::isfinite(n) && std::fabs(n - expected[v * 3 + c]) < 2e-5f);
      length2 += n * n;
    }
    assert(length2 < 1e-8f || std::fabs(length2 - 1.0f) < 2e-6f);
  }
}

int main(int argc, char** argv) {
  TestEligibility();
  TestGeometry();
  unsigned assets = 0;
  if (argc == 2)
    for (const auto& entry : std::filesystem::directory_iterator(argv[1]))
      if (entry.path().extension() == ".bin") { TestAsset(entry.path()); ++assets; }
  std::cout << "Missing-normal eligibility, geometry safety and " << assets << " real body fixtures passed.\n";
}
