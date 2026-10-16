#pragma once

#include <cstdint>

struct PPCContext;

namespace skate3::world_loading {

enum class ContextResourceSearch { kFallback, kCreate, kReady };

// Only replaces the read-only search prefix of original 827E1608, after its
// prologue. Guest construction, cache insertion and reference ownership stay
// in the original function. No retained guest pointers or host lookup cache.
ContextResourceSearch TryFastContextResourceSearch(PPCContext& ctx, uint8_t* base);

}  // namespace skate3::world_loading
