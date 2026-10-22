#pragma once
// New San Van data components and their required versions (completion markers).
// Mirrors tools/nsv_components.py; tools/test_nsv_components.py keeps them identical.
// A release that changes a component's prepared output bumps that component's
// marker here and in its builder; existing installs then update only that
// component (and the ones derived from it) at startup.
#include <filesystem>
#include <string>
#include <vector>

namespace skate3 {

struct NsvComponent {
  const char* id;
  std::vector<const char*> markers;    // under mods/skate2, or CONTENT/... under the content root
  bool needs_skate2;                   // the Skate 2 game files
  bool needs_skate2_dlc;               // the Skate 2 DLC packages
  std::vector<const char*> dependents; // rebuilt together with this one
};

const std::vector<NsvComponent>& NsvComponents();

enum class NsvInstallState {
  kMissing,         // never installed (or an interrupted publication): full setup
  kComplete,        // every component current
  kUpdateRequired,  // installed, but some components are older than this build needs
};

struct NsvStatus {
  NsvInstallState state = NsvInstallState::kMissing;
  std::vector<std::string> components;  // out of date, plus their dependents, in build order
  bool needs_skate2 = false;
  bool needs_skate2_dlc = false;
};

NsvStatus GetNewSanVanStatus(const std::filesystem::path& app_root,
                             const std::filesystem::path& content_root);

// "a,b,c" for tools/prepare_new_san_van.py --components.
std::string JoinNsvComponents(const std::vector<std::string>& ids);

}  // namespace skate3
