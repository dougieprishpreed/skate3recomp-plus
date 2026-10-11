#pragma once

#include <filesystem>
#include <cstdint>
#include <string>

namespace rex::filesystem { class VirtualFileSystem; }
namespace skate3::world_loading {
void Install(rex::filesystem::VirtualFileSystem* vfs, const std::filesystem::path& app_root);
void RequestLoad();
// Locations-menu New San Van pick: the world key to hand the retail teleport
// (loaded key inside New San Van on the same stream, else an armed BAM
// template district). stream: the Skate 2 world stream to run (BAM or an
// installed DLC map).
bool ArmMenuSpawn(uint8_t* base, const std::string& label, const std::string& stream,
                  uint64_t& world_key);
// Before the retail location teleport: leaving New San Van must reload.
void BeforeRetailTeleport(uint8_t* base, uint64_t world);
bool IsSkate2Gameplay();
bool OriginalPedestriansActive();
// New San Vanelona sky choice: after Install, copies the selected sky's two
// dome textures into guest memory for the native renderer: Skate 3's
// (skate3_skate2_sky=false; miscload.big DIST_skybox_Textures: panorama and
// sun gradient) or the night sky (skate3_skate2_night; the Sanitarium
// download's DIST_skyboxnightdlc_Textures: night panorama and moon gradient,
// found among the content packages under content_root).
void PrepareSky(const std::filesystem::path& game_data_root,
                const std::filesystem::path& content_root);
// The two prepared texture headers (guest addresses) the native renderer
// draws the Skate 2 sky dome with; false when Skate 2's sky is selected or
// the textures are unavailable.
bool DomeSkyTextures(uint32_t* diffuse, uint32_t* specular);
// The night sky is prepared: New San Van runs the native renderer's night
// light (its world's baked light at the Sanitarium's night levels).
bool NightSky();
// Verified in Danny Way DLC material records (log058): trees, mountains, mist.
// Keep sky dome0964DD9C85BDC24E and every BAM texture untouched.
constexpr bool IsDannyWayBackdrop(uint64_t guid) {
  return guid == 0x0964DD9C85AAC1D3ull || guid == 0x0964DD9C85AAC1D4ull ||
         guid == 0x0964DD9C85B2886Dull;
}
// Original district proxy SFIL material inventories also contain tree,
// animated.tree, alphatest and bark materials. Exact source IDs, not a
// blanket foliage-family filter; native gameplay gating is at every caller.
constexpr bool IsNativeProxyFoliage(uint64_t guid) {
  switch (guid) {
    case 0x80007DC203E3870Aull:
    case 0x80007DF003E3870Aull:
    case 0x80007E3003E3870Aull:
    case 0x80007E3203E3870Aull:
    case 0x80007E3503E3870Aull:
    case 0x80007E3A03E3870Aull:
    case 0xAC70170A000505BFull:
    case 0xAC70170A00054B14ull:
    case 0xAC70170A00054B15ull:
    case 0xAC70170A00054B16ull:
    case 0xAC70170A00054B17ull:
    case 0xAC70170A00054B18ull:
    case 0xAC70170A00054B19ull:
    case 0xAC70170A00054B1Aull:
    case 0xAC70170A00054B1Bull:
    case 0xAC70170A00054B1Cull:
    case 0xAC70170A00054B23ull:
    case 0xAC70170A00054B24ull:
    case 0xAC70170A00054B25ull:
    case 0xAC70170A00054B26ull:
    case 0xAC70170A00054B43ull:
    case 0xAC70170A00054B44ull:
    case 0xAC70170A00054B5Dull:
    case 0xAC70170A000700DFull:
      return true;
    default: return false;
  }
}
// Skate 2 world art (material texture references): the base game's
// 0000xxxx03E3870A/B (bit0 = min-mip, log053) and its DLC maps'
// 4000xxxx03E3870A (bit62 marks the DLC namespace; no min-mip variants).
// Skate3 worlds export 4000xxxx03E3870A only as cPres min-mips of cTex
// 0000xxxx03E3870A details, which is what their materials reference.
constexpr bool IsSkate2TextureNamespace(uint64_t guid) {
  return ((guid >> 48) == 0 && (uint32_t(guid) & ~1u) == 0x03E3870A) ||
      ((guid >> 48) == 0x4000 && uint32_t(guid) == 0x03E3870A);
}
// Native proxyworld materials use non-Skate-2 texture identities: all 145
// unique source PROXY texture IDs in Skate2 are 0000xxxx03E3870A/B, and the
// Rob Dyrdek plaza's DLC freeways (proxyworld.default) are 4000xxxx03E3870A;
// none of the 1400 exports in Skate3's three district proxy archives use
// either form (hashed, or 8000xxxx03E3870A).
constexpr bool IsInheritedBackdrop(uint64_t guid, uint8_t env_family) {
  return IsDannyWayBackdrop(guid) || IsNativeProxyFoliage(guid) ||
      (env_family == 11 && !IsSkate2TextureNamespace(guid));
}
}
