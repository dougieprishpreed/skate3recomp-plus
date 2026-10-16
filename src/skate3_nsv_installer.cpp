#include "skate3_nsv_installer.h"
#include "skate3_install_paths.h"
#include "skate3_setup_process.h"
#include "skate3_iso_installer.h"
#include "skate3_setup_sources.h"
#include "skate3_title_update_installer.h"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <memory>
#include <queue>
#include <random>
#include <set>
#include <thread>
#include <vector>

#include <rex/filesystem.h>
#include <rex/filesystem/devices/stfs_container_device.h>
#include <rex/filesystem/entry.h>
#include <rex/filesystem/file.h>
#include <rex/logging.h>
#include <rex/system/xam/content_manager.h>
#include <rex/ui/overlay/acquire_wizard_overlay.h>
#include <rex/ui/windowed_app_context.h>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <commdlg.h>
#include <rex/ui/window_win.h>
#endif

namespace skate3 {
namespace fs = std::filesystem;
namespace {
constexpr auto kContent = "0000000000000000/454108E6";
constexpr auto kMenu = "00000002/4E53564D4F442D4E455753414E56414E2D534B3245/newsanvan_00000000.big";

bool SafeRelative(const fs::path& path) {
  if (path.is_absolute() || path.has_root_name()) return false;
  for (const auto& part : path) {
    const auto s = part.string();
    if (s == ".." || s == "." || s.find(':') != std::string::npos) return false;
  }
  return true;
}
}

bool ExtractDlcForSetup(const fs::path& source, const fs::path& stage, uint32_t expected_title,
                        std::atomic<uint64_t>& copied, std::string& error) {
  try {
    // Use the same read-only STFS implementation as ContentManager::InstallContent.
    rex::filesystem::StfsContainerDevice device("", source);
    if (!device.Initialize() || device.header().metadata.execution_info.title_id != expected_title ||
        device.header().metadata.content_type != rex::system::XContentType::kMarketplaceContent) {
      error = "Select downloadable content for the requested title: " + source.filename().string();
      return false;
    }
    std::string name;
    // Canonical content identity also accepts packages renamed by the user.
    constexpr char hex[] = "0123456789ABCDEF";
    for (uint8_t c : device.header().header.content_id) {
      name += hex[c >> 4]; name += hex[c & 15];
    }
    const fs::path base = expected_title == 0x454108E6
        ? stage / kContent / "00000002" / name : stage / name;
    if (fs::exists(base)) {
      error = "The same DLC package was selected more than once.";
      return false;
    }
    fs::create_directories(base);
    std::queue<rex::filesystem::Entry*> pending;
    auto* root = device.ResolvePath("");
    if (!root) { error = "Unable to read DLC directory."; return false; }
    pending.push(root);
    std::set<fs::path> seen;
    std::vector<uint8_t> buffer(4 * 1024 * 1024);
    while (!pending.empty()) {
      auto* entry = pending.front(); pending.pop();
      for (const auto& child : entry->children()) pending.push(child.get());
      auto relative = fs::path(rex::string::utf8_fix_path_separators(entry->path()));
      if (!SafeRelative(relative) || !seen.insert(relative).second) {
        error = "DLC contains an unsafe or duplicate path."; return false;
      }
      auto dest = base / relative;
      if (entry->attributes() & rex::filesystem::kFileAttributeDirectory) {
        fs::create_directories(dest); continue;
      }
      fs::create_directories(dest.parent_path());
      rex::filesystem::File* raw = nullptr;
      if (entry->Open(rex::filesystem::FileAccess::kFileReadData, &raw) != 0 || !raw) {
        error = "Unable to open DLC payload."; return false;
      }
      auto close = [](rex::filesystem::File* file) { file->Destroy(); };
      std::unique_ptr<rex::filesystem::File, decltype(close)> file(raw, close);
      std::ofstream out(dest, std::ios::binary);
      size_t offset = 0;
      while (offset < entry->size()) {
        size_t got = 0;
        const size_t count = std::min<size_t>(buffer.size(), entry->size() - offset);
        const auto status = file->ReadSync(std::span<uint8_t>(buffer.data(), count), offset, &got);
        if (status != 0 || got == 0 || got > count) {
          error = "DLC payload is truncated or unreadable."; return false;
        }
        out.write(reinterpret_cast<char*>(buffer.data()), got);
        if (!out) { error = "Unable to write DLC payload."; return false; }
        offset += got; copied += got;
      }
      out.close();
      if (!out) { error = "Unable to finish writing DLC payload."; return false; }
    }
    if (expected_title == 0x454108E6) {
      rex::system::xam::XCONTENT_AGGREGATE_DATA header{};
      header.device_id = 1;
      header.content_type = rex::system::XContentType::kMarketplaceContent;
      header.title_id = expected_title; header.xuid = 0;
      header.set_file_name(name);
      header.set_display_name(device.header().metadata.display_name(rex::system::XLanguage::kEnglish));
      uint32_t mask = 0;
      for (const auto& license : device.header().header.licenses)
        if (license.license_flags) mask |= license.license_bits;
      auto path = stage / kContent / "Headers/00000002" / (name + ".header");
      fs::create_directories(path.parent_path());
      std::ofstream out(path, std::ios::binary);
      out.write(reinterpret_cast<const char*>(&header), sizeof(header));
      out.write(reinterpret_cast<const char*>(&mask), sizeof(mask));
      out.close();
      if (!out) { error = "Unable to write DLC content header."; return false; }
    }
    return true;
  } catch (const std::exception& e) { error = e.what(); return false; }
}

bool IsNewSanVanInstalled(const fs::path& app_root, const fs::path& content_root) {
  if (fs::exists(app_root / "install-state/nsv-transaction.json")) return false;
  const auto mods = app_root / "mods/skate2";
  // Older navigation policies need the water/road/crosswalk rebake.
  const char* required[] = {"world-support-gvr-v1/BAM_COMPLETE_V2",
      "world-support-gvr-v1/NAVIGATION_COMPLETE_V4", "proxy-v1/manifest.json",
      "sky-water-v1/SKY_WATER_COMPLETE_V1", "jumbotron/attract.s2jv",
      "dlc-maps-v1/DLC_MAPS_COMPLETE_V1",
      "dlc-maps-v1/DLC_School/NAVIGATION_COMPLETE_V4", "dlc-maps-v1/DLC_School/LIVINGWORLD_COMPLETE_V5",
      "dlc-maps-v1/DLC_Community_Center/NAVIGATION_COMPLETE_V4", "dlc-maps-v1/DLC_Community_Center/LIVINGWORLD_COMPLETE_V5",
      "dlc-maps-v1/DLC_Parkade/NAVIGATION_COMPLETE_V4", "dlc-maps-v1/DLC_Parkade/LIVINGWORLD_COMPLETE_V5",
      "audio-v1/AUDIO_COMPLETE_V2",
      "livingworld-v1/LIVINGWORLD_COMPLETE_V1", "nsv_spawns.txt"};
  for (auto name : required) if (!fs::is_regular_file(mods / name)) return false;
  return fs::is_regular_file(content_root / kContent / kMenu);
}

namespace {
bool Prepare(const SetupInputs& input, const rex::PathConfig& paths,
             std::atomic<uint64_t>& copied, std::atomic<uint64_t>& total, std::string& error) {
  fs::path scratch;
  const auto app = ContentInstallRoot(paths.user_data_root);
  const auto resources = SetupResourceRoot();
  try {
    #if defined(_WIN32)
    const auto python = resources / "installer/python/python.exe";
    const auto media = resources / "installer/media/ffmpeg.exe";
    const auto baker = resources / "installer/tools/nsv_nav_bake.exe";
#else
    const auto python = resources / "installer/python/bin/python3";
    const auto media = resources / "installer/media/ffmpeg";
    const auto baker = resources / "installer/tools/nsv_nav_bake";
#endif
    const auto script = resources / "installer/tools/prepare_new_san_van.py";
    if (!fs::is_regular_file(python) || !fs::is_regular_file(script) ||
        !fs::is_regular_file(media) || !fs::is_regular_file(baker)) {
      error = "The installer folder is missing. Extract the complete release package.";
      return false;
    }
    fs::create_directories(app / "install-state");
    scratch = app / "install-state" / ("nsv-" + std::to_string(std::random_device{}()));
    if (!fs::create_directory(scratch)) { error = "Unable to create installer scratch."; return false; }
    fs::path source = input.s2.path;
    bool ok = true;
    if (!input.s2.extracted) {
      source = scratch / "source";
      ok = ExtractXboxIso(input.s2.path, source, 0x4541087F, copied, total, error);
    }
    // Preparation has no useful byte total; show the wizard's indeterminate state.
    total = 0;
    for (const auto& p : input.dlc2)
      if (ok) ok = StageSetupDlc(p, scratch / "dlc", 0x4541087F, copied, error);
    for (const auto& p : input.dlc3)
      if (ok) ok = StageSetupDlc(p, scratch / "prepared", 0x454108E6, copied, error);
    if (ok) {
      const std::vector<std::string> args = {"-E", "-s", rex::path_to_utf8(script),
          "--source", rex::path_to_utf8(fs::absolute(source / "data")),
          "--native-data", rex::path_to_utf8(fs::absolute(paths.game_data_root / "data")),
          "--dlc-source", rex::path_to_utf8(scratch / "dlc"),
          "--stage", rex::path_to_utf8(scratch / "prepared"),
          "--root", rex::path_to_utf8(app), "--content-root", rex::path_to_utf8(paths.user_data_root),
          "--ffmpeg", rex::path_to_utf8(media), "--nav-baker", rex::path_to_utf8(baker)};
      ok = RunSetupProcess(python, args, app / "install-state/new-san-van.log", error);
    }
    std::error_code ec; fs::remove_all(scratch, ec);
    return ok;
  } catch (const std::exception& e) {
    error = e.what();
    if (!scratch.empty()) { std::error_code ec; fs::remove_all(scratch, ec); }
    return false;
  }
}
}

bool InstallSetupInputs(const SetupInputs& input, const rex::PathConfig& paths,
                         std::atomic<uint64_t>& copied, std::atomic<uint64_t>& total,
                         std::string& error) {
  fs::path staged;
  try {
    const auto game = fs::absolute(paths.game_data_root);
    const auto suffix = std::to_string(std::random_device{}());
    auto s3=input.s3, s2=input.s2;
    if (!ValidateSetupGame(s3,0x454108E6,error) || !ValidateSetupGame(s2,0x4541087F,error) ||
        !IsTitleUpdateInstalled(input.verified_tu)) {
      if (error.empty()) error="Select a verified Title Update 3 before installing.";
      return false;
    }
    // TU may be sourced from the old game directory. Pin its verified payloads
    // before replacing that directory, and remove only scratch created here.
    struct UpdateScratch {
      fs::path path;
      ~UpdateScratch() { if (!path.empty()) { std::error_code ec; fs::remove_all(path,ec); } }
    } update;
    const auto tu_path=game.parent_path()/"install-state"/("verified-tu-"+suffix);
    fs::create_directories(tu_path.parent_path());
    if (!fs::create_directory(tu_path)) throw std::runtime_error("Update staging already exists");
    update.path=tu_path;
    for (const auto& rel : {fs::path("default.xexp"),fs::path("data/webkit/EAWebkit.xexp")}) {
      fs::create_directories((update.path/rel).parent_path());
      fs::copy_file(input.verified_tu/rel,update.path/rel);
    }
    // Disc installation is a complete directory swap. Existing files are kept
    // on the same volume, so publishing and restoring are atomic renames.
    if (!input.s3.extracted || !(fs::exists(game) && fs::equivalent(input.s3.path, game))) {
      staged = game.parent_path() / (".game-setup-" + suffix);
      if (fs::exists(staged)) { staged.clear(); throw std::runtime_error("Setup staging already exists"); }
      if (input.s3.extracted) {
        auto relative=staged.lexically_relative(fs::absolute(input.s3.path));
        if (!relative.empty() && *relative.begin() != "..") {
          staged.clear(); throw std::runtime_error("The disc source must be outside the installation folder.");
        }
      }
      bool ok = true;
      if (input.s3.extracted) {
        fs::create_directories(staged);
        for (const auto& entry : fs::recursive_directory_iterator(input.s3.path)) {
          if (entry.is_symlink()) throw std::runtime_error("Extracted disc contains a symbolic link");
          const auto dest = staged / entry.path().lexically_relative(input.s3.path);
          if (entry.is_directory()) fs::create_directories(dest);
          else if (entry.is_regular_file()) { fs::copy_file(entry.path(), dest); copied += entry.file_size(); }
        }
      } else ok = ExtractXboxIso(input.s3.path, staged, 0x454108E6, copied, total, error);
      if (!ok) { std::error_code ec; fs::remove_all(staged, ec); return false; }
      const auto backup = game.parent_path() / "install-backups" / ("game-" + suffix);
      if (fs::exists(game)) { fs::create_directories(backup.parent_path()); fs::rename(game, backup); }
      try { fs::rename(staged, game); }
      catch (...) { if (fs::exists(backup)) fs::rename(backup, game); throw; }
      staged.clear();
    }
    if (!IsTitleUpdateInstalled(game)) {
      for (const auto& rel : {fs::path("default.xexp"), fs::path("data/webkit/EAWebkit.xexp")}) {
        const auto dest = game / rel;
        if (fs::exists(dest)) {
          const auto backup = game.parent_path() / "install-backups" / ("tu-" + suffix) / rel;
          fs::create_directories(backup.parent_path()); fs::copy_file(dest, backup);
        }
        fs::create_directories(dest.parent_path());
        fs::copy_file(update.path / rel, dest, fs::copy_options::overwrite_existing);
      }
    }
    if (!IsTitleUpdateInstalled(game)) { error = "Title Update 3 verification failed."; return false; }
    return Prepare(input, paths, copied, total, error);
  } catch (const std::exception& e) {
    error = e.what();
    if (!staged.empty()) { std::error_code ec; fs::remove_all(staged, ec); }
    return false;
  }
}
}
