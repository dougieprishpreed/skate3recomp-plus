#pragma once
#include <filesystem>
#include <string>
#include <vector>

namespace skate3 {
// Direct argv invocation, never a shell. Waits on the install worker, not the UI.
bool RunSetupProcess(const std::filesystem::path& executable,
                     const std::vector<std::string>& arguments,
                     const std::filesystem::path& log_path, std::string& error);
}
