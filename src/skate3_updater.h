#pragma once
// In-game updater: checks GitHub Releases for a newer build, downloads this
// platform's package, verifies it, replaces the program files in place and
// restarts. Game files, New San Van data, settings and saves are never touched;
// if the new build needs newer New San Van data, it asks for that at startup.
#include <atomic>
#include <cstdint>
#include <filesystem>
#include <string>

namespace skate3::updater {

struct Release {
  std::string tag;         // "v0.2b"
  std::string display;     // "v0.2b"
  std::string page_url;    // the release page
  std::string asset_name;  // this platform's package
  std::string asset_url;
  std::string sha256;      // lowercase hex, from GitHub's asset digest
  uint64_t size = 0;
};

enum class CheckResult { kUpToDate, kAvailable, kUnavailable };

// Short blocking request to the GitHub API (call from a worker thread).
CheckResult CheckForUpdate(Release& release, std::string& detail);

// "v0.2a" for this build.
std::string CurrentVersionDisplay();

// False (with a reason) when the program folder cannot be replaced in place.
bool CanInstallInPlace(std::string& reason);

enum class Phase { kDownloading, kVerifying, kInstalling };

// Downloads, verifies and installs; the new files are in place on success and
// take effect after Relaunch(). On failure the installation is unchanged.
bool DownloadAndInstall(const Release& release, std::atomic<uint64_t>& done,
                        std::atomic<uint64_t>& total, std::atomic<int>& phase, std::string& error);

// Starts the (new) program and returns; the caller then quits.
bool Relaunch(std::string& error);

// Startup: waits for the instance that just updated to exit, and removes the
// files an earlier update replaced.
void FinishPreviousUpdate();

void OpenInBrowser(const std::string& url);

}  // namespace skate3::updater
