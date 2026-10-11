#pragma once

#include <filesystem>

// New San Vanelona entries in the pause-menu Locations list (see
// skate3_nsv_menu.cpp and tools/build_nsv_dlc.py).
namespace skate3::nsv_menu {

// Reads <app_root>/mods/skate2/nsv_spawns.txt; call before guest execution.
void Install(const std::filesystem::path& app_root);

}  // namespace skate3::nsv_menu
