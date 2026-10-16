#pragma once
#include <cstddef>
#include <cstdint>
#include <new>
#include <stdexcept>
#include <vector>

namespace skate3::native_scene {
// Per decoding thread, only ordinary cached host memory. No guest/resource
// identity or previous verdict is retained. Large inputs use call-local storage.
struct MeshDecodeOutputScratch {
  static constexpr size_t kVertexBytes = 4 * 1024 * 1024;
  static constexpr size_t kIndexBytes = 1024 * 1024;
  std::vector<float> vertices;
  std::vector<uint16_t> indices;
  template <class T>
  static T* Resize(std::vector<T>& cache, std::vector<T>& overflow,
                   size_t count, size_t byte_limit) {
    auto& target = count <= byte_limit / sizeof(T) ? cache : overflow;
    // Exact reserve requests keep retained capacity below the byte limit even
    // after a series of small increases near the cap.
    if (target.capacity() < count) target.reserve(count);
    target.resize(count);
    return target.data();
  }
  bool Prepare(size_t vertex_floats, size_t index_count,
               std::vector<float>& overflow_vertices,
               std::vector<uint16_t>& overflow_indices,
               float*& vertex_output, uint16_t*& index_output) {
    try {
      vertex_output = Resize(vertices, overflow_vertices, vertex_floats, kVertexBytes);
      index_output = Resize(indices, overflow_indices, index_count, kIndexBytes);
      return true;
    } catch (const std::bad_alloc&) {
    } catch (const std::length_error&) {
    }
    vertex_output = nullptr; index_output = nullptr;
    return false;  // retain the original direct decode on host allocation failure
  }
};
}  // namespace skate3::native_scene
