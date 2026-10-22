#pragma once

#include <cstdint>
#include <string_view>

namespace skate3::native_policy {

inline bool IsNsvDlc(std::string_view stream) {
  return stream == "DLC_School" || stream == "DLC_Community_Center" ||
         stream == "DLC_Parkade" || stream == "DLC_Rob_Fantasy_Plaza" ||
         stream == "DLC_MaloofMoneyCup";
}

inline uint64_t AmbienceFallback(uint64_t original, uint32_t layer, bool nsv,
                                bool audio_ready, std::string_view stream,
                                uint64_t bed) {
  // A real painted value always wins. Never replace zones/reverb or a
  // retail map; native bed start/fade/stop and world lifetime remain owners.
  return original == 0 && layer == 7 && nsv && audio_ready && IsNsvDlc(stream)
             ? bed : original;
}

inline uint64_t DlcAmbienceBed(std::string_view stream, uint64_t old_town,
                               uint64_t downtown) {
  return stream == "DLC_Parkade" || stream == "DLC_Rob_Fantasy_Plaza"
             ? downtown : old_town;
}

}  // namespace skate3::native_policy
