#pragma once
#include <filesystem>
#include <rex/filesystem.h>

namespace skate3 {
// Read-only resources and writable content have different roots in desktop packages.
inline std::filesystem::path SetupResourceRoot() {
  const auto executable = rex::filesystem::GetExecutableFolder();
#if defined(__APPLE__)
  const auto contents = executable.parent_path();
  if (executable.filename() == "MacOS" && contents.filename() == "Contents" &&
      contents.parent_path().extension() == ".app") return contents / "Resources";
#endif
  return rex::filesystem::GetAppRootFolder();
}

inline std::filesystem::path ContentInstallRoot(const std::filesystem::path& user_root) {
#if defined(_WIN32)
  return rex::filesystem::GetAppRootFolder();
#else
  return user_root;
#endif
}
}  // namespace skate3
