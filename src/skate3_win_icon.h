#pragma once

#include <filesystem>

namespace skate3 {

// Windows only (no-op elsewhere). Prefer the executable's embedded MAINICON
// for the window/taskbar/Alt-Tab icon; Explorer reads that same resource.
// Builds without an embedded icon retain the legacy background fallback:
// extract the user's own title art from game/nxeart and patch the file once.
void ApplyGameIconFromGameData(const std::filesystem::path& game_data_root,
                               void* native_window_handle);

}  // namespace skate3
