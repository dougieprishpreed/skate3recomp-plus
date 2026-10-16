#pragma once

#include <cstdint>
#include <cstring>

namespace skate3::native_scene {

// Call-local snapshots, never a shader-address cache across draws or frames.
// Water/scroll/ocean probes inspect the same PS and VS debug paths repeatedly.
// Share their bounded read while keeping each probe's own positive family gate.
class DrawShaderPaths {
 public:
  template <typename Read>
  bool Contains(uint32_t object, const char* needle, Read&& read) {
    for (unsigned i = 0; i < count_; ++i)
      if (entries_[i].object == object)
        return std::strstr(entries_[i].text, needle) != nullptr;
    // Only the bound PS/VS are normally requested. A changed binding within
    // a call replaces a slot, rather than making the cache grow or go stale.
    Entry& entry = entries_[count_ < 2 ? count_++ : 1];
    entry.object = object;
    read(entry.text);
    entry.text[119] = '\0';
    return std::strstr(entry.text, needle) != nullptr;
  }
 private:
  struct Entry { uint32_t object; char text[120]; };
  Entry entries_[2];
  unsigned count_ = 0;
};

}  // namespace skate3::native_scene
