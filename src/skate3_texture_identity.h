#pragma once
#include <cstdint>
#include <charconv>
#include <istream>
#include <string>
#include <unordered_set>

namespace skate3::world_loading {
// BAM Texture exports use bit0 for the min-mip variant. Skate3's texture
// notification consumer (8279EE58) uses bit62 and keys both variants with
// that bit cleared (8280AC58). Keep archive/asset-manager identities intact;
// translate only the render binding notification and its matching removal.
// Lighting IDs are content hashes, including bit62. Only IDs inventoried
// from original BAM lighting exports may use this broader translation.
// Apply once at the notification boundary: encoded low IDs can coincide
// with original high IDs, so repeated translation is not meaningful.
constexpr uint64_t TranslateBamLowTexture(uint64_t id, bool lighting_id = false) {
  if (lighting_id) {
    constexpr uint64_t flag = uint64_t{1} << 62;
    return (id & ~(flag | uint64_t{1})) | ((id & 1) ? flag : 0);
  }
  return (id >> 48) == 0 && uint32_t(id) == 0x03E3870B
      ? (id & ~uint64_t{1}) | (uint64_t{1} << 62) : id;
}
inline bool TranslateBamTextureBind(bool enabled, uint32_t caller,
                                    uint64_t& argument, uint64_t& saved_id,
                                    bool lighting_id = false) {
  if (!enabled || caller != 0x8279EE58 || argument != saved_id) return false;
  const auto translated = TranslateBamLowTexture(argument, lighting_id);
  if (translated == argument) return false;
  argument = saved_id = translated;
  return true;
}
inline bool TranslateBamTextureUnbind(bool enabled, uint32_t caller, uint64_t& id,
                                      bool lighting_id = false) {
  if (!enabled || caller != 0x8279F2D8) return false;
  const auto translated = TranslateBamLowTexture(id, lighting_id);
  if (translated == id) return false;
  id = translated;
  return true;
}
// Strict, bounded startup manifest. Failure leaves the destination untouched.
// Generated locally by tools/build_bam_lighting_ids.py; no asset bytes shipped.
inline bool ReadBamLightingIds(std::istream& input, std::unordered_set<uint64_t>& ids) {
  std::string token;
  uint32_t count = 0;
  if (!(input >> token) || token != "BAM_LIGHTING_TEXTURE_IDS_V1" ||
      !(input >> count) || count == 0 || count > 65536) return false;
  std::unordered_set<uint64_t> parsed;
  for (uint32_t i = 0; i < count; ++i) {
    if (!(input >> token) || token.size() != 16) return false;
    uint64_t id = 0;
    const auto result = std::from_chars(token.data(), token.data() + token.size(), id, 16);
    if (result.ec != std::errc{} || result.ptr != token.data() + token.size() ||
        !id || !parsed.insert(id).second) return false;
  }
  if (input >> token) return false;
  ids = std::move(parsed);
  return true;
}
}  // namespace skate3::world_loading
