#pragma once
// Release identity rules for the updater, kept free of runtime dependencies so
// tools/test_updater.py can compile them directly.
//
// Release tags are v<major>.<minor>[letter]; the letter is the patch number
// (v0.2 = 0.2.0, v0.2a = 0.2.1, v0.2b = 0.2.2), the same numeric version CMake
// gives the build (project VERSION).
#include <array>
#include <cctype>
#include <cstdint>
#include <string>
#include <string_view>

namespace skate3::update_version {

using Version = std::array<int, 3>;

// "v0.2a", "0.2a", "v1.4" -> {major, minor, patch}. False for anything else.
inline bool ParseTag(std::string_view tag, Version& out) {
  if (!tag.empty() && (tag.front() == 'v' || tag.front() == 'V')) tag.remove_prefix(1);
  int part[2] = {0, 0};
  size_t i = 0;
  for (int k = 0; k < 2; ++k) {
    if (i >= tag.size() || !std::isdigit(static_cast<unsigned char>(tag[i]))) return false;
    int value = 0;
    while (i < tag.size() && std::isdigit(static_cast<unsigned char>(tag[i]))) {
      value = value * 10 + (tag[i] - '0');
      if (value > 9999) return false;
      ++i;
    }
    part[k] = value;
    if (k == 0) {
      if (i >= tag.size() || tag[i] != '.') return false;
      ++i;
    }
  }
  int patch = 0;
  if (i < tag.size() && std::isalpha(static_cast<unsigned char>(tag[i]))) {
    patch = std::tolower(static_cast<unsigned char>(tag[i])) - 'a' + 1;
    ++i;
  }
  if (i != tag.size()) return false;
  out = {part[0], part[1], patch};
  return true;
}

// "0.2.1" (the build's numeric version) -> {0, 2, 1}.
inline bool ParseNumeric(std::string_view text, Version& out) {
  Version v{};
  size_t i = 0;
  for (int k = 0; k < 3; ++k) {
    if (i >= text.size() || !std::isdigit(static_cast<unsigned char>(text[i]))) return false;
    int value = 0;
    while (i < text.size() && std::isdigit(static_cast<unsigned char>(text[i]))) {
      value = value * 10 + (text[i] - '0');
      if (value > 9999) return false;
      ++i;
    }
    v[k] = value;
    if (k < 2) {
      if (i >= text.size() || text[i] != '.') return false;
      ++i;
    }
  }
  if (i != text.size()) return false;
  out = v;
  return true;
}

inline bool Newer(const Version& candidate, const Version& current) { return candidate > current; }

// Display form of a version: {0, 2, 1} -> "v0.2a".
inline std::string Display(const Version& v) {
  std::string out = "v" + std::to_string(v[0]) + "." + std::to_string(v[1]);
  if (v[2] > 0 && v[2] <= 26) out += char('a' + v[2] - 1);
  else if (v[2] > 26) out += "." + std::to_string(v[2]);
  return out;
}

enum class Platform { kWindows, kLinux, kMacOS };

inline std::string Lower(std::string_view text) {
  std::string out(text);
  for (char& c : out) c = char(std::tolower(static_cast<unsigned char>(c)));
  return out;
}

// Release asset for this platform: a .zip whose name carries the platform
// (Skate3Recomp-Plus-v0.2-Windows-x64.zip, ...-Linux-x64.zip, ...-macOS-arm64.zip;
// the runner names win-amd64 / linux-amd64 / macos-arm64 are accepted too).
inline bool MatchesPlatform(std::string_view asset_name, Platform platform) {
  const std::string name = Lower(asset_name);
  if (name.size() < 4 || name.compare(name.size() - 4, 4, ".zip") != 0) return false;
  if (name.find("symbols") != std::string::npos) return false;
  switch (platform) {
    case Platform::kWindows:
      return name.find("windows") != std::string::npos || name.find("win-amd64") != std::string::npos;
    case Platform::kLinux:
      return name.find("linux") != std::string::npos;
    case Platform::kMacOS:
      return name.find("macos") != std::string::npos;
  }
  return false;
}

// GitHub's asset digest is "sha256:<64 hex>"; returns the lowercase hex or "".
inline std::string Sha256FromDigest(std::string_view digest) {
  constexpr std::string_view kPrefix = "sha256:";
  if (digest.size() != kPrefix.size() + 64 || Lower(digest.substr(0, kPrefix.size())) != kPrefix) {
    return {};
  }
  std::string hex = Lower(digest.substr(kPrefix.size()));
  for (char c : hex)
    if (!std::isxdigit(static_cast<unsigned char>(c))) return {};
  return hex;
}

}  // namespace skate3::update_version
