#pragma once
#include <filesystem>
#include <string>
#include <vector>

namespace skate3 {
// Direct argv invocation, never a shell. Waits on the install worker, not the UI.
bool RunSetupProcess(const std::filesystem::path& executable,
                     const std::vector<std::string>& arguments,
                     const std::filesystem::path& log_path, std::string& error);

// A bundled program copied without its Unix permissions (for example through a
// Windows PC) is made executable again before it runs. Nothing to do on Windows.
void EnsureExecutable(const std::filesystem::path& file);
}
