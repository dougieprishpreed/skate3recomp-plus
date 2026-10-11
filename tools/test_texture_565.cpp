#include "../src/skate3_texture_565.h"
#include <cassert>
#include <cstdint>
#include <cstdio>

int main() {
  using namespace skate3::texture;
  uint8_t c[4];
  Decode565(0x001f, c); // guest X, independent of the final material swizzle
  assert(c[0] == 255 && c[1] == 0 && c[2] == 0);
  Decode565(0x07e0, c);
  assert(c[0] == 0 && c[1] == 255 && c[2] == 0);
  Decode565(0xf800, c);
  assert(c[0] == 0 && c[1] == 0 && c[2] == 255);
  // Every possible source texel: the expanded cube and the existing packed
  // 2D path must expose the same quantized channels, including guest W=Z.
  for (unsigned i = 0; i < 65536; ++i) {
    const auto v = uint16_t(i), host = Host565(v);
    Decode565(v, c);
    assert((c[0] >> 3) == (host >> 11));
    assert((c[1] >> 2) == ((host >> 5) & 63));
    assert((c[2] >> 3) == (host & 31));
    assert(c[3] == c[2]);
    assert(Host565(host) == v);
  }
  std::puts("565: primary colours and all 65,536 packed/expanded texels agree");
}
