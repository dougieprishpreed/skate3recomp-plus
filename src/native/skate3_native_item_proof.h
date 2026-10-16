#pragma once

#include <cstdint>
#include <cstring>
#include <vector>

namespace skate3::native_scene {

// A bounded, one-shot proof of the actual bytes consumed by a worker's item
// walk. Generation checks establish arena lifetime; this catches late fixups,
// channel rebinding and in-place descriptor/material edits within that lifetime.
// No guest pointers are dereferenced without the caller's guarded read.
struct ItemCoreProof {
  struct Span { uint32_t address, offset, size; };
  static constexpr uint32_t kMaxSpans = 96, kMaxBytes = 8192;
  bool valid = true;
  std::vector<Span> spans;
  std::vector<uint8_t> bytes;

  void Add(uint32_t address, const void* data, uint32_t size) {
    if (!valid) return;
    if (!address || !size || address > UINT32_MAX - size ||
        spans.size() >= kMaxSpans || size > kMaxBytes - bytes.size()) {
      valid = false;
      return;
    }
    spans.push_back({address, uint32_t(bytes.size()), size});
    const auto* p = static_cast<const uint8_t*>(data);
    bytes.insert(bytes.end(), p, p + size);
  }

  template <class Read> bool Matches(Read&& read) const {
    if (!valid || spans.empty()) return false;
    // Largest recorded region is the 32-channel array (1024 bytes).
    uint8_t scratch[1024];
    for (const Span& span : spans) {
      if (span.size > sizeof(scratch) ||
          !read(span.address, scratch, span.size) ||
          std::memcmp(scratch, bytes.data() + span.offset, span.size))
        return false;
    }
    return true;
  }
};

}  // namespace skate3::native_scene
