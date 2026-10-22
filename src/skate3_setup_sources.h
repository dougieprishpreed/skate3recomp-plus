#pragma once
#include <atomic>
#include <filesystem>
#include <string>
#include <vector>
#include <rex/rex_app.h>

namespace skate3 {
struct SetupGameSource {
  std::filesystem::path path;
  bool extracted = false;
};
struct SetupDlcSource {
  std::filesystem::path path;
  bool extracted = false;
  std::vector<std::string> archives;
};
struct SetupInputs {
  SetupGameSource s3, s2;
  std::filesystem::path verified_tu;
  std::vector<SetupDlcSource> dlc3, dlc2;
};
bool ValidateSetupGame(SetupGameSource& source, uint32_t title, std::string& error);
bool DiscoverSetupDlc(const std::filesystem::path& folder, uint32_t title,
                      std::vector<SetupDlcSource>& result, std::string& message);
bool StageSetupDlc(const SetupDlcSource& source, const std::filesystem::path& stage,
                   uint32_t title, std::atomic<uint64_t>& copied, std::string& error);
bool InstallSetupInputs(const SetupInputs& input, const rex::PathConfig& paths,
                         std::atomic<uint64_t>& copied, std::atomic<uint64_t>& total,
                         std::string& error);
// Updates only the given New San Van components (skate3_nsv_components.h) of an
// existing installation, from the Skate 2 sources they need.
bool UpdateNewSanVan(const SetupInputs& input, const rex::PathConfig& paths,
                     const std::vector<std::string>& components, std::atomic<uint64_t>& copied,
                     std::atomic<uint64_t>& total, std::string& error);
}
