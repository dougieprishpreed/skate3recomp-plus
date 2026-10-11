#include "skate3_setup_sources.h"
#include "skate3_iso_installer.h"
#include "skate3_nsv_installer.h"
#include <algorithm>
#include <array>
#include <fstream>
#include <map>
#include <queue>
#include <set>
#include <sstream>
#include <rex/filesystem/devices/stfs_container_device.h>
#include <rex/filesystem/entry.h>
#include <rex/system/xam/content_manager.h>

namespace skate3 {
namespace fs = std::filesystem;
namespace {
const std::array<std::string, 3> kS2 = {"skate1_classic_xbox2.big", "maloof_money_cup_xbox2.big",
                                      "rob_fantasy_plaza_xbox2.big"};
std::string Lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return char(std::tolower(c)); });
  return s;
}
bool ArchiveName(const std::string& name, uint32_t title) {
  if (title == 0x4541087F) return std::find(kS2.begin(), kS2.end(), name) != kS2.end();
  return name.ends_with("_00000000.big") && name != "newsanvan_00000000.big";
}
uint32_t Be32(const uint8_t* p) { return uint32_t(p[0]) << 24 | uint32_t(p[1]) << 16 | uint32_t(p[2]) << 8 | p[3]; }
bool XexTitle(const fs::path& path, uint32_t title) {
  std::ifstream in(path, std::ios::binary);
  std::array<uint8_t, 24> h{};
  if (!in.read(reinterpret_cast<char*>(h.data()), h.size()) ||
      std::string_view(reinterpret_cast<char*>(h.data()), 4) != "XEX2") return false;
  auto count = Be32(h.data() + 20);
  if (count > 4096) return false;
  for (uint32_t i = 0; i < count; ++i) {
    std::array<uint8_t, 8> entry{};
    if (!in.read(reinterpret_cast<char*>(entry.data()), entry.size())) return false;
    if (Be32(entry.data()) != 0x40006) continue;
    in.seekg(Be32(entry.data() + 4));
    return bool(in.read(reinterpret_cast<char*>(h.data()), h.size())) && Be32(h.data() + 12) == title;
  }
  return false;
}
}

bool ValidateSetupGame(SetupGameSource& source, uint32_t title, std::string& error) {
  try {
    if (!source.extracted) return ValidateXboxIso(source.path, title, error);
    if (Lower(source.path.filename().string()) == "data" && fs::is_regular_file(source.path.parent_path() / "default.xex"))
      source.path = source.path.parent_path();
    if (!XexTitle(source.path / "default.xex", title)) {
      error = "Choose the extracted disc folder containing the correct default.xex and data folder.";
      return false;
    }
    const auto data = source.path / "data";
    const std::vector<fs::path> required = title == 0x4541087F
        ? std::vector<fs::path>{"content/worldbam.big", "content/worldproxy.big", "content/missions.big",
                              "content/livingworld.big", "db/db.big", "big/miscboot.big", "movies"}
        : std::vector<fs::path>{"big/db.big", "big/miscload.big", "audio/audiofiles.big", "content"};
    for (const auto& name : required) if (!fs::exists(data / name)) {
      error = "The extracted disc is incomplete: missing data/" + name.generic_string(); return false;
    }
    return true;
  } catch (const std::exception& e) { error = e.what(); return false; }
}

bool DiscoverSetupDlc(const fs::path& folder, uint32_t title,
                      std::vector<SetupDlcSource>& result, std::string& message) {
  result.clear();
  try {
    std::vector<SetupDlcSource> candidates;
    std::map<fs::path, std::vector<std::string>> loose;
    for (const auto& entry : fs::recursive_directory_iterator(folder)) {
      if (!entry.is_regular_file() || entry.is_symlink()) continue;
      const auto p = entry.path();
      const auto name = Lower(p.filename().string());
      std::ifstream in(p, std::ios::binary);
      std::array<char, 4> magic{};
      if (!in.read(magic.data(), magic.size())) continue;
      if (ArchiveName(name, title)) {
        std::array<uint8_t,32> archive_header{};
        in.seekg(0);
        if (!in.read(reinterpret_cast<char*>(archive_header.data()),archive_header.size())) {
          message="The DLC archive is truncated: "+p.string(); return false;
        }
        const bool eb = magic == std::array<char,4>{'E','B',0,3};
        const bool big = magic == std::array<char,4>{'B','I','G','4'} ||
                         magic == std::array<char,4>{'B','I','G','F'};
        const auto count=Be32(archive_header.data()+8), end=Be32(archive_header.data()+12);
        const bool valid=eb ? Be32(archive_header.data()+28)==entry.file_size() :
            big && end>=16 && end<=entry.file_size() && count>0 && count<=(end-16)/9;
        if (!valid) { message="Invalid DLC archive: "+p.string(); return false; }
        loose[p.parent_path()].push_back(name);
        continue;
      }
      std::string_view tag(magic.data(), 4);
      if (tag != "CON " && tag != "LIVE" && tag != "PIRS") continue;
      auto header = rex::filesystem::StfsContainerDevice::ReadPackageHeader(p);
      if (!header || header->metadata.execution_info.title_id != title ||
          header->metadata.content_type != rex::system::XContentType::kMarketplaceContent) continue;
      rex::filesystem::StfsContainerDevice device("", p);
      if (!device.Initialize()) { message = "Unable to read DLC package: " + p.string(); return false; }
      auto* root = device.ResolvePath("");
      if (!root) { message = "DLC package has no readable directory: " + p.string(); return false; }
      SetupDlcSource found{p, false, {}};
      std::queue<rex::filesystem::Entry*> pending; pending.push(root);
      while (!pending.empty()) {
        auto* item = pending.front(); pending.pop();
        auto file_name = Lower(fs::path(item->path()).filename().string());
        if (ArchiveName(file_name, title)) found.archives.push_back(file_name);
        for (const auto& child : item->children()) pending.push(child.get());
      }
      if (!found.archives.empty() || title == 0x454108E6) candidates.push_back(std::move(found));
    }
    // Prefer the extracted representation when both versions of the same pack
    // are selected. Identity is its payload, never its outer folder/file name.
    for (auto& [path, names] : loose) result.push_back({path, true, std::move(names)});
    std::set<std::string> seen;
    for (const auto& source : result) for (const auto& name : source.archives)
      if (!seen.insert(name).second) { message = "Multiple extracted copies of " + name + "; choose a narrower folder."; return false; }
    for (const auto& candidate : candidates) {
      bool overlap = false;
      for (const auto& name : candidate.archives) overlap |= seen.contains(name);
      if (overlap) continue;
      result.push_back(candidate);
      seen.insert(candidate.archives.begin(), candidate.archives.end());
    }
    if (title == 0x4541087F) {
      std::string missing;
      for (const auto& name : kS2) if (!seen.contains(name)) missing += (missing.empty() ? "" : ", ") + name;
      if (!missing.empty()) { message = "Missing required map packs: " + missing; return false; }
    }
    if (result.empty()) { message = "No downloadable content found in this folder."; return false; }
    message = std::to_string(result.size()) + " packs found";
    return true;
  } catch (const std::exception& e) { message = e.what(); return false; }
}

bool StageSetupDlc(const SetupDlcSource& source, const fs::path& stage,
                   uint32_t title, std::atomic<uint64_t>& copied, std::string& error) {
  if (!source.extracted) return ExtractDlcForSetup(source.path, stage, title, copied, error);
  try {
    auto names = source.archives;
    std::sort(names.begin(), names.end());
    uint64_t id = 1469598103934665603ull;
    for (const auto& name : names) for (unsigned char c : name) { id ^= c; id *= 1099511628211ull; }
    std::ostringstream key; key << "EXTRACTED_" << std::hex << std::uppercase << id;
    const auto name = key.str();
    const auto dest = title == 0x454108E6
        ? stage / "0000000000000000/454108E6/00000002" / name : stage / name;
    if (fs::exists(dest)) { error = "Duplicate DLC content."; return false; }
    fs::create_directories(dest);
    // Archive and its accompanying files (including spa.bin) form one package.
    // Do not copy adjacent packages or an entire user's selected directory tree.
    for (const auto& entry : fs::directory_iterator(source.path)) {
      if (!entry.is_regular_file() || entry.is_symlink()) continue;
      fs::copy_file(entry.path(), dest / entry.path().filename());
      copied += entry.file_size();
    }
    if (title == 0x454108E6) {
      rex::system::xam::XCONTENT_AGGREGATE_DATA header{};
      header.device_id = 1; header.content_type = rex::system::XContentType::kMarketplaceContent;
      header.title_id = title; header.xuid = 0;
      header.set_file_name(name);
      header.set_display_name(fs::path(names.front()).stem().u16string());
      auto path = stage / "0000000000000000/454108E6/Headers/00000002" / (name + ".header");
      fs::create_directories(path.parent_path());
      std::ofstream out(path, std::ios::binary);
      out.write(reinterpret_cast<const char*>(&header), sizeof(header));
      const uint32_t mask = 0xFFFFFFFF;
      out.write(reinterpret_cast<const char*>(&mask), sizeof(mask));
      out.close();
      if (!out) { error = "Unable to write extracted DLC metadata."; return false; }
    }
    return true;
  } catch (const std::exception& e) { error = e.what(); return false; }
}
}
