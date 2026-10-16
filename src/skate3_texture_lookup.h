#pragma once

#include <cstdint>

struct PPCContext;

namespace skate3::world_loading {

// The texture binding table remains guest-owned. The index only lives during
// one original collection activation callback, including nested activation.
class TextureLookupBatch {
 public:
  TextureLookupBatch();
  ~TextureLookupBatch();
  TextureLookupBatch(const TextureLookupBatch&) = delete;
  TextureLookupBatch& operator=(const TextureLookupBatch&) = delete;

 private:
  bool entered_ = false;
  bool native_entered_ = false;
};

// Snapshotted once by the original NSV scenery activation scope. Inline
// TLS query adds no per-hash cvar call, atomic exchange or logging.
extern thread_local bool g_native_scenery_registers;
inline bool NativeSceneryRegistersEnabled() { return g_native_scenery_registers; }

// Only the find in 8280AC58 can use this index. Other GUID tables and calls
// retain the generated implementation, including its trap handling.
bool TryIndexedTextureLookup(PPCContext& ctx, uint8_t* base);

class TextureLookupResetScope {
 public:
  explicit TextureLookupResetScope(uint32_t table);
  ~TextureLookupResetScope();
  TextureLookupResetScope(const TextureLookupResetScope&) = delete;
  TextureLookupResetScope& operator=(const TextureLookupResetScope&) = delete;
 private:
  bool entered_ = false;
};

}  // namespace skate3::world_loading
