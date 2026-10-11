#pragma once
#include <cstdint>

namespace skate3::texture {
// Xenos k_5_6_5 stores X in the low five bits. DXGI B5G6R5 stores R
// in the high five bits. These operations precede the guest fetch swizzle.
constexpr uint16_t Host565(uint16_t v) {
  return uint16_t((v & 0x07e0u) | ((v & 31u) << 11) | (v >> 11));
}
inline void Decode565(uint16_t v, uint8_t* rgba) {
  const uint8_t x = uint8_t(v & 31u), y = uint8_t((v >> 5) & 63u), z = uint8_t(v >> 11);
  rgba[0] = uint8_t((x << 3) | (x >> 2));
  rgba[1] = uint8_t((y << 2) | (y >> 4));
  rgba[2] = uint8_t((z << 3) | (z >> 2));
  rgba[3] = rgba[2]; // k_5_6_5's guest W is Z, matching the 2D RGBB view.
}
}
