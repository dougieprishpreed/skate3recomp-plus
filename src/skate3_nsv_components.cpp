#include "skate3_nsv_components.h"

#include <algorithm>
#include <set>

namespace skate3 {
namespace fs = std::filesystem;
namespace {
constexpr const char* kContent = "0000000000000000/454108E6";
constexpr std::string_view kContentPrefix = "CONTENT/";

fs::path Resolve(std::string_view marker, const fs::path& mods, const fs::path& content_root) {
  if (marker.substr(0, kContentPrefix.size()) == kContentPrefix) {
    return content_root / kContent / std::string(marker.substr(kContentPrefix.size()));
  }
  return mods / std::string(marker);
}
}  // namespace

const std::vector<NsvComponent>& NsvComponents() {
  // Build order. Keep in step with tools/nsv_components.py.
  static const std::vector<NsvComponent> components = {
      {"sky", {"sky-water-v1/SKY_WATER_COMPLETE_V1"}, true, false, {"city", "dlc_maps"}},
      {"city",
       {"world-support-gvr-v1/BAM_COMPLETE_V2", "world-support-gvr-v1/NAVIGATION_COMPLETE_V4"},
       true, false, {"livingworld"}},
      {"skyline", {"proxy-v1/manifest.json"}, true, false, {}},
      {"dlc_maps",
       {"dlc-maps-v1/DLC_MAPS_COMPLETE_V1", "dlc-maps-v1/DLC_School/NAVIGATION_COMPLETE_V4",
        "dlc-maps-v1/DLC_Community_Center/NAVIGATION_COMPLETE_V4",
        "dlc-maps-v1/DLC_Parkade/NAVIGATION_COMPLETE_V4",
        "dlc-maps-v1/DLC_School/LIVINGWORLD_COMPLETE_V9",
        "dlc-maps-v1/DLC_Community_Center/LIVINGWORLD_COMPLETE_V9",
        "dlc-maps-v1/DLC_Parkade/LIVINGWORLD_COMPLETE_V9"},
       true, true, {}},
      {"audio", {"audio-v1/AUDIO_COMPLETE_V2"}, true, true, {"menu"}},
      {"livingworld", {"livingworld-v1/LIVINGWORLD_COMPLETE_V1"}, true, false, {"menu"}},
      {"jumbotron", {"jumbotron/attract.s2jv"}, true, false, {}},
      {"menu",
       {"nsv_spawns.txt",
        "CONTENT/00000002/4E53564D4F442D4E455753414E56414E2D534B3245/newsanvan_00000000.big"},
       false, false, {}},
  };
  return components;
}

NsvStatus GetNewSanVanStatus(const fs::path& app_root, const fs::path& content_root) {
  NsvStatus status;
  // An interrupted publication is recovered by the full setup flow.
  if (fs::exists(app_root / "install-state/nsv-transaction.json")) return status;
  const auto mods = app_root / "mods/skate2";
  std::set<std::string> stale;
  bool any_current = false;
  for (const auto& component : NsvComponents()) {
    const bool current = std::all_of(component.markers.begin(), component.markers.end(),
                                     [&](const char* marker) {
                                       return fs::is_regular_file(
                                           Resolve(marker, mods, content_root));
                                     });
    if (current) any_current = true;
    else stale.insert(component.id);
  }
  if (stale.empty()) {
    status.state = NsvInstallState::kComplete;
    return status;
  }
  // Nothing of New San Van present at all: a first installation.
  if (!any_current) return status;
  // Out-of-date components plus everything derived from them.
  std::vector<std::string> pending(stale.begin(), stale.end());
  while (!pending.empty()) {
    const std::string id = pending.back();
    pending.pop_back();
    for (const auto& component : NsvComponents()) {
      if (id != component.id) continue;
      for (const char* dependent : component.dependents)
        if (stale.insert(dependent).second) pending.push_back(dependent);
    }
  }
  status.state = NsvInstallState::kUpdateRequired;
  for (const auto& component : NsvComponents()) {
    if (!stale.count(component.id)) continue;
    status.components.push_back(component.id);
    status.needs_skate2 |= component.needs_skate2;
    status.needs_skate2_dlc |= component.needs_skate2_dlc;
  }
  return status;
}

std::string JoinNsvComponents(const std::vector<std::string>& ids) {
  std::string out;
  for (const auto& id : ids) {
    if (!out.empty()) out += ',';
    out += id;
  }
  return out;
}

}  // namespace skate3
