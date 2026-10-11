#include "../src/skate3_texture_identity.h"
#include <cassert>
#include <iostream>
#include <sstream>
#include <fstream>

int main(int argc, char** argv) {
  using namespace skate3::world_loading;
  constexpr uint64_t high = 0x00006A7103E3870A, low = high | 1;
  constexpr uint64_t flag = uint64_t{1} << 62;
  assert(TranslateBamLowTexture(low) == (high | flag));
  for (auto id : {high, high | flag, uint64_t{0x4E8AAC7B89E95463},
                  uint64_t{0x00016A7103E3870B}, uint64_t{0x00006A7103E3870D}})
    assert(TranslateBamLowTexture(id) == id);
  auto arg = low, saved = low;
  assert(!TranslateBamTextureBind(false, 0x8279EE58, arg, saved));
  assert(!TranslateBamTextureBind(true, 0x8280AAB4, arg, saved));
  assert(arg == low && saved == low);
  saved = high;
  assert(!TranslateBamTextureBind(true, 0x8279EE58, arg, saved));
  saved = low;
  assert(TranslateBamTextureBind(true, 0x8279EE58, arg, saved));
  assert(arg == saved && arg == (high | flag));
  // These are the guest consumer's actual slot selection and lookup mask.
  assert((saved & flag) != 0 && (arg & ~flag) == high);
  auto removal = low;
  assert(!TranslateBamTextureUnbind(true, 0x8279EE58, removal));
  assert(TranslateBamTextureUnbind(true, 0x8279F2D8, removal));
  assert((removal & ~flag) == (arg & ~flag));
  assert(!TranslateBamTextureUnbind(true, 0x8279F2D8, removal));
  // Real log056 lightmap/chromaticity identities, with both values of bit62.
  for (uint64_t original : {uint64_t{0x90E5397BB29C1A12}, uint64_t{0x7962212EEDACAFC0}}) {
    const auto key = original & ~flag;
    assert(TranslateBamLowTexture(original) == original); // Unknown hashes untouched.
    assert(TranslateBamLowTexture(original, true) == key);
    assert(TranslateBamLowTexture(original | 1, true) == (key | flag));
    for (auto source : {original, original | 1}) {
      auto a = source, b = source, u = source;
      TranslateBamTextureBind(true, 0x8279EE58, a, b, true);
      TranslateBamTextureUnbind(true, 0x8279F2D8, u, true);
      assert(a == b && b == u && (a & ~flag) == key);
      assert(bool(b & flag) == bool(source & 1));
      a = b = source;
      assert(!TranslateBamTextureBind(false, 0x8279EE58, a, b, true));
      assert(!TranslateBamTextureBind(true, 0x8280AAB4, a, b, true));
      assert(a == source && b == source);
    }
  }
  std::unordered_set<uint64_t> ids;
  std::istringstream valid("BAM_LIGHTING_TEXTURE_IDS_V1 2 7962212EEDACAFC0 7962212EEDACAFC1");
  assert(ReadBamLightingIds(valid, ids) && ids.size() == 2);
  for (const char* bad : {"wrong 1 7962212EEDACAFC0", "BAM_LIGHTING_TEXTURE_IDS_V1 65537",
      "BAM_LIGHTING_TEXTURE_IDS_V1 1 0000000000000000", "BAM_LIGHTING_TEXTURE_IDS_V1 1 abc",
      "BAM_LIGHTING_TEXTURE_IDS_V1 2 7962212EEDACAFC0", "BAM_LIGHTING_TEXTURE_IDS_V1 1 7962212EEDACAFCZ",
      "BAM_LIGHTING_TEXTURE_IDS_V1 1 7962212EEDACAFC0 extra",
      "BAM_LIGHTING_TEXTURE_IDS_V1 2 7962212EEDACAFC0 7962212EEDACAFC0"}) {
    std::istringstream input(bad);
    assert(!ReadBamLightingIds(input, ids) && ids.size() == 2);
  }
  if (argc > 1) {
    std::ifstream manifest(argv[1]);
    assert(ReadBamLightingIds(manifest, ids));
    assert(ids.contains(0x7962212EEDACAFC0) && ids.contains(0x7962212EEDACAFC1));
    for (auto source : ids) {
      const auto encoded = TranslateBamLowTexture(source, true);
      assert((encoded & ~flag) == (source & ~(flag | uint64_t{1})));
      assert(bool(encoded & flag) == bool(source & 1));
    }
    std::cout << "Validated " << ids.size() << " inventoried lighting IDs.\n";
  }
  std::cout << "Texture notification identity, scope and removal guards passed.\n";
}
