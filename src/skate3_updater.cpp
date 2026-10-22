#include "skate3_updater.h"

#include "skate3_install_paths.h"
#include "skate3_setup_process.h"
#include "skate3_update_version.h"
#include "skate3_version.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <random>
#include <string_view>
#include <thread>
#include <vector>

#include <rex/cvar.h>
#include <rex/filesystem.h>
#include <rex/logging.h>

#include "third_party/rexglue-sdk/thirdparty/crypto/sha256.h"
#include "third_party/rexglue-sdk/thirdparty/inja/third_party/include/nlohmann/json.hpp"

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>
#include <winhttp.h>
#else
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#endif

REXCVAR_DEFINE_BOOL(skate3_update_check, true, "Skate 3",
                    "Check for a newer version of Skate 3 Recompiled+ when the game starts");
// Not prefixed skate3_: launch/testing values that must not be saved to settings.toml.
REXCVAR_DEFINE_STRING(update_repository, "dougieprishpreed/skate3recomp-plus", "Updater",
                      "GitHub repository whose latest release the updater checks");
REXCVAR_DEFINE_INT32(update_wait_pid, 0, "Updater",
                     "Set by the updater when it restarts the game: wait for this process to "
                     "exit, then finish the update");

namespace skate3::updater {
namespace fs = std::filesystem;
using update_version::Version;

namespace {

constexpr const char* kStagingName = ".skate3-update";
constexpr const char* kUserAgent = "Skate3Recompiled-Plus-Updater";

#if defined(_WIN32)
constexpr auto kPlatform = update_version::Platform::kWindows;
#elif defined(__APPLE__)
constexpr auto kPlatform = update_version::Platform::kMacOS;
#else
constexpr auto kPlatform = update_version::Platform::kLinux;
#endif

// Where the program lives: `container` receives the package's top-level
// entries; on macOS the package's .app replaces the running `bundle`.
struct Layout {
  fs::path container;
  fs::path bundle;
  fs::path executable;  // what Relaunch() starts
};

Layout GetLayout() {
  Layout layout;
  const fs::path exe_dir = rex::filesystem::GetExecutableFolder();
#if defined(__APPLE__)
  const fs::path contents = exe_dir.parent_path();
  if (exe_dir.filename() == "MacOS" && contents.filename() == "Contents" &&
      contents.parent_path().extension() == ".app") {
    layout.bundle = contents.parent_path();
    layout.container = layout.bundle.parent_path();
    layout.executable = layout.bundle;
    return layout;
  }
#endif
  layout.container = exe_dir;
#if defined(_WIN32)
  layout.executable = exe_dir / "skate3.exe";
#else
  layout.executable = exe_dir / "skate3";
#endif
  return layout;
}

std::string RandomHex() {
  std::random_device device;
  char text[17];
  std::snprintf(text, sizeof(text), "%08x%08x", unsigned(device()), unsigned(device()));
  return text;
}

#if defined(_WIN32)
std::wstring Widen(std::string_view value) {
  if (value.empty()) return {};
  const int length = MultiByteToWideChar(CP_UTF8, 0, value.data(), int(value.size()), nullptr, 0);
  std::wstring wide(size_t(length), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, value.data(), int(value.size()), wide.data(), length);
  return wide;
}

// GET over WinHTTP (redirects followed). Writes the body to `file` when given,
// otherwise to `body`.
bool HttpGet(const std::string& url, const wchar_t* extra_headers, std::string* body,
             const fs::path* file, std::atomic<uint64_t>* done, int timeout_ms,
             std::string& error) {
  std::wstring wide_url = Widen(url);
  URL_COMPONENTS parts{};
  parts.dwStructSize = sizeof(parts);
  parts.dwSchemeLength = DWORD(-1);
  parts.dwHostNameLength = DWORD(-1);
  parts.dwUrlPathLength = DWORD(-1);
  parts.dwExtraInfoLength = DWORD(-1);
  if (!WinHttpCrackUrl(wide_url.c_str(), 0, 0, &parts)) {
    error = "Invalid update address.";
    return false;
  }
  const std::wstring host(parts.lpszHostName, parts.dwHostNameLength);
  std::wstring path(parts.lpszUrlPath, parts.dwUrlPathLength);
  if (parts.lpszExtraInfo) path.append(parts.lpszExtraInfo, parts.dwExtraInfoLength);
  HINTERNET session = WinHttpOpen(Widen(kUserAgent).c_str(), WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                  WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
  if (!session) {
    error = "Unable to start the download.";
    return false;
  }
  WinHttpSetTimeouts(session, timeout_ms, timeout_ms, timeout_ms, timeout_ms * 3);
  HINTERNET connection = WinHttpConnect(session, host.c_str(), parts.nPort, 0);
  HINTERNET request =
      connection ? WinHttpOpenRequest(connection, L"GET", path.c_str(), nullptr,
                                      WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                      parts.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0)
                 : nullptr;
  bool ok = request &&
            WinHttpSendRequest(request, extra_headers ? extra_headers : WINHTTP_NO_ADDITIONAL_HEADERS,
                               extra_headers ? DWORD(-1L) : 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0) &&
            WinHttpReceiveResponse(request, nullptr);
  DWORD status = 0, status_size = sizeof(status);
  if (ok) {
    WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                        WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size, WINHTTP_NO_HEADER_INDEX);
    ok = status == 200;
    if (!ok) error = "The update server answered " + std::to_string(status) + ".";
  } else {
    error = "Could not reach the update server (error " + std::to_string(GetLastError()) + ").";
  }
  std::ofstream out;
  if (ok && file) {
    out.open(*file, std::ios::binary | std::ios::trunc);
    if (!out) {
      ok = false;
      error = "Unable to save the download.";
    }
  }
  std::vector<char> buffer(1 << 16);
  while (ok) {
    DWORD read = 0;
    if (!WinHttpReadData(request, buffer.data(), DWORD(buffer.size()), &read)) {
      ok = false;
      error = "The download was interrupted.";
      break;
    }
    if (read == 0) break;
    if (file) {
      out.write(buffer.data(), read);
      if (!out) {
        ok = false;
        error = "Unable to save the download.";
      }
    } else {
      body->append(buffer.data(), read);
      if (body->size() > 8u << 20) {
        ok = false;
        error = "Unexpected update response.";
      }
    }
    if (done) done->fetch_add(read, std::memory_order_relaxed);
  }
  if (request) WinHttpCloseHandle(request);
  if (connection) WinHttpCloseHandle(connection);
  WinHttpCloseHandle(session);
  return ok;
}
#else
bool Curl(const std::vector<std::string>& args, const fs::path& log, std::string& error) {
  return RunSetupProcess("/usr/bin/curl", args, log, error);
}
#endif

bool FetchLatestRelease(std::string& body, std::string& error) {
  const std::string url =
      "https://api.github.com/repos/" + REXCVAR_GET(update_repository) + "/releases/latest";
#if defined(_WIN32)
  return HttpGet(url,
                 L"Accept: application/vnd.github+json\r\nX-GitHub-Api-Version: 2022-11-28",
                 &body, nullptr, nullptr, 5000, error);
#else
  const fs::path temp = fs::temp_directory_path() / ("skate3-update-check-" + RandomHex());
  fs::create_directories(temp);
  const bool ok =
      Curl({"-fsSL", "--connect-timeout", "5", "--max-time", "15", "-A", kUserAgent, "-H",
            "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28", "-o",
            (temp / "release.json").string(), url},
           temp / "curl.log", error);
  if (ok) {
    std::ifstream in(temp / "release.json", std::ios::binary);
    body.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
  }
  std::error_code ec;
  fs::remove_all(temp, ec);
  return ok;
#endif
}

bool DownloadFile(const std::string& url, const fs::path& file, std::atomic<uint64_t>& done,
                  std::string& error) {
#if defined(_WIN32)
  return HttpGet(url, nullptr, nullptr, &file, &done, 20000, error);
#else
  // curl runs in a child process; report progress from the growing file.
  std::atomic<bool> running{true};
  std::thread progress([&] {
    while (running.load()) {
      std::error_code ec;
      const auto size = fs::file_size(file, ec);
      if (!ec) done.store(size, std::memory_order_relaxed);
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
  });
  const bool ok = Curl({"-fL", "--retry", "3", "--connect-timeout", "20", "-A", kUserAgent, "-sS",
                        "-o", file.string(), url},
                       file.parent_path() / "download.log", error);
  running = false;
  progress.join();
  std::error_code ec;
  const auto size = fs::file_size(file, ec);
  if (!ec) done.store(size, std::memory_order_relaxed);
  if (!ok) error = "The download did not finish. Check your connection and try again.";
  return ok;
#endif
}

std::string Sha256File(const fs::path& file) {
  std::ifstream in(file, std::ios::binary);
  if (!in) return {};
  sha256::SHA256 hasher;
  std::vector<char> buffer(1 << 20);
  while (in) {
    in.read(buffer.data(), std::streamsize(buffer.size()));
    if (in.gcount() > 0) hasher.add(buffer.data(), size_t(in.gcount()));
  }
  return update_version::Lower(hasher.getHash());
}

bool Extract(const fs::path& archive, const fs::path& destination, std::string& error) {
  fs::create_directories(destination);
  const fs::path log = archive.parent_path() / "extract.log";
#if defined(_WIN32)
  wchar_t system[MAX_PATH];
  const UINT length = GetSystemDirectoryW(system, MAX_PATH);
  const fs::path tar = fs::path(std::wstring(system, length)) / "tar.exe";
  if (!fs::is_regular_file(tar)) {
    error = "This version of Windows cannot unpack the update. Download it from the release page.";
    return false;
  }
  return RunSetupProcess(tar, {"-xf", rex::path_to_utf8(archive), "-C", rex::path_to_utf8(destination)},
                         log, error);
#elif defined(__APPLE__)
  // ditto keeps the app bundle's links, modes and signature intact.
  return RunSetupProcess("/usr/bin/ditto", {"-x", "-k", archive.string(), destination.string()}, log,
                         error);
#else
  if (fs::is_regular_file("/usr/bin/unzip"))
    return RunSetupProcess("/usr/bin/unzip", {"-q", archive.string(), "-d", destination.string()},
                           log, error);
  const fs::path resources = SetupResourceRoot() / "installer";
  return RunSetupProcess(resources / "python/bin/python3",
                         {"-B", "-E", "-s", (resources / "tools/extract_update_archive.py").string(),
                          archive.string(), destination.string()},
                         log, error);
#endif
}

// The package's single top-level folder, checked to hold this platform's program.
bool PackageRoot(const fs::path& extracted, fs::path& root, std::string& error) {
  std::vector<fs::path> entries;
  for (const auto& entry : fs::directory_iterator(extracted)) entries.push_back(entry.path());
  if (entries.size() != 1 || !fs::is_directory(entries[0])) {
    error = "The downloaded package has an unexpected layout.";
    return false;
  }
  root = entries[0];
#if defined(_WIN32)
  const bool ok = fs::is_regular_file(root / "skate3.exe") && fs::is_directory(root / "installer");
#elif defined(__APPLE__)
  bool ok = false;
  for (const auto& entry : fs::directory_iterator(root))
    ok |= entry.path().extension() == ".app" &&
          fs::is_regular_file(entry.path() / "Contents/MacOS/skate3");
#else
  const bool ok = fs::is_regular_file(root / "skate3") && fs::is_directory(root / "installer");
#endif
  if (!ok) error = "The downloaded package is not a Skate 3 Recompiled+ release for this platform.";
  return ok;
}

// Names in the program folder that belong to the player, never to a package.
bool Protected(const std::string& name) {
  static const char* kProtected[] = {"game",     "mods",  "install-state", "install-backups",
                                     "logs",     "saves", "profiles",      "cache",
                                     "0000000000000000", kStagingName};
  for (const char* p : kProtected)
    if (name == p) return true;
  return false;
}

struct Move {
  fs::path from, to;
};

void Rollback(const std::vector<Move>& moves) {
  for (auto it = moves.rbegin(); it != moves.rend(); ++it) {
    std::error_code ec;
    fs::rename(it->to, it->from, ec);
    if (ec) REXLOG_ERROR("update: could not restore {}: {}", rex::path_to_utf8(it->from), ec.message());
  }
}

// Moves the package's entries into place; the replaced ones go to `previous`.
bool Swap(const Layout& layout, const fs::path& package, const fs::path& previous,
          std::string& error) {
  std::vector<Move> moves;
  fs::create_directories(previous);
  try {
    int index = 0;
    // The listing first, the moves after: renaming entries out of a folder
    // while a directory iterator walks it is not defined to visit each one
    // exactly once (readdir); a repeated entry would fail its rename and roll
    // the whole update back.
    std::vector<fs::path> entries;
    for (const auto& entry : fs::directory_iterator(package)) entries.push_back(entry.path());
    for (const fs::path& entry_path : entries) {
      const std::string name = rex::path_to_utf8(entry_path.filename());
      if (Protected(name)) continue;
      fs::path destination = layout.container / entry_path.filename();
#if defined(__APPLE__)
      // Only the app bundle is the program: the package's README, setup notes
      // and provenance beside it must not land in the folder holding the app
      // (usually /Applications).
      if (entry_path.extension() != ".app") continue;
      destination = layout.bundle;
#endif
#if defined(_WIN32)
      // The Windows program folder holds the player's own settings.toml.
      if (name == "settings.toml" && fs::exists(destination)) continue;
#endif
      if (fs::exists(destination) || fs::is_symlink(destination)) {
        const fs::path old = previous / (std::to_string(index++) + "-" + name);
        fs::rename(destination, old);
        moves.push_back({destination, old});
      }
      fs::rename(entry_path, destination);
      moves.push_back({entry_path, destination});
    }
  } catch (const std::exception& e) {
    Rollback(moves);
    error = std::string("The update could not replace the program files (") + e.what() +
            "). Nothing was changed.";
    return false;
  }
  return true;
}

}  // namespace

std::string CurrentVersionDisplay() {
  Version current{};
  return update_version::ParseNumeric(SKATE3_VERSION_NUMERIC, current)
             ? update_version::Display(current)
             : std::string("v") + SKATE3_VERSION_STRING;
}

CheckResult CheckForUpdate(Release& release, std::string& detail) {
  Version current{};
  if (!update_version::ParseNumeric(SKATE3_VERSION_NUMERIC, current)) {
    detail = "unknown build version";
    return CheckResult::kUnavailable;
  }
  std::string body;
  if (!FetchLatestRelease(body, detail)) return CheckResult::kUnavailable;
  const auto json = nlohmann::json::parse(body, nullptr, false);
  if (json.is_discarded() || !json.is_object()) {
    detail = "unexpected release information";
    return CheckResult::kUnavailable;
  }
  const std::string tag = json.value("tag_name", std::string());
  Version latest{};
  if (json.value("draft", false) || json.value("prerelease", false) ||
      !update_version::ParseTag(tag, latest)) {
    detail = "no usable release tag";
    return CheckResult::kUnavailable;
  }
  if (!update_version::Newer(latest, current)) return CheckResult::kUpToDate;
  release.tag = tag;
  release.display = update_version::Display(latest);
  release.page_url = json.value("html_url", std::string());
  for (const auto& asset : json.value("assets", nlohmann::json::array())) {
    const std::string name = asset.value("name", std::string());
    if (!update_version::MatchesPlatform(name, kPlatform)) continue;
    release.asset_name = name;
    release.asset_url = asset.value("browser_download_url", std::string());
    release.size = asset.value("size", uint64_t(0));
    release.sha256 = update_version::Sha256FromDigest(asset.value("digest", std::string()));
    break;
  }
  REXLOG_INFO("update: {} is available (this build {}); package {}", release.display,
              CurrentVersionDisplay(), release.asset_name.empty() ? "none" : release.asset_name);
  return CheckResult::kAvailable;
}

bool CanInstallInPlace(std::string& reason) {
  const Layout layout = GetLayout();
  const fs::path probe = layout.container / (std::string(kStagingName) + "-probe-" + RandomHex());
  std::error_code ec;
  {
    std::ofstream out(probe, std::ios::binary);
    if (!out) {
      reason = "The game folder is read-only, so the update can't be installed automatically.";
      return false;
    }
  }
  fs::remove(probe, ec);
#if defined(__APPLE__)
  if (layout.bundle.empty() || rex::path_to_utf8(layout.bundle).find("/AppTranslocation/") !=
                                   std::string::npos) {
    reason = "Move Skate 3 Recompiled+ to your Applications folder to install updates automatically.";
    return false;
  }
#endif
  return true;
}

bool DownloadAndInstall(const Release& release, std::atomic<uint64_t>& done,
                        std::atomic<uint64_t>& total, std::atomic<int>& phase, std::string& error) {
  if (release.asset_url.empty()) {
    error = "This release has no package for your system yet.";
    return false;
  }
  if (release.sha256.empty()) {
    error = "This release can't be verified automatically.";
    return false;
  }
  const Layout layout = GetLayout();
  const fs::path staging = layout.container / kStagingName / ("u-" + RandomHex());
  std::error_code ec;
  try {
    fs::create_directories(staging);
    const fs::path archive = staging / "package.zip";
    phase = int(Phase::kDownloading);
    done = 0;
    total = release.size;
    REXLOG_INFO("update: downloading {} ({} bytes)", release.asset_name, release.size);
    if (!DownloadFile(release.asset_url, archive, done, error)) throw std::runtime_error(error);
    if (release.size && fs::file_size(archive) != release.size)
      throw std::runtime_error("The download is incomplete. Try again.");
    phase = int(Phase::kVerifying);
    if (Sha256File(archive) != release.sha256)
      throw std::runtime_error("The download didn't pass verification. Try again.");
    phase = int(Phase::kInstalling);
    if (!Extract(archive, staging / "extracted", error))
      throw std::runtime_error("The update couldn't be unpacked. " + error);
    fs::path package;
    if (!PackageRoot(staging / "extracted", package, error)) throw std::runtime_error(error);
    if (!Swap(layout, package, staging / "previous", error)) throw std::runtime_error(error);
    fs::remove(archive, ec);
    REXLOG_INFO("update: {} installed; restarting", release.display);
    return true;
  } catch (const std::exception& e) {
    error = e.what();
    REXLOG_ERROR("update: {}", error);
    fs::remove_all(staging, ec);
    return false;
  }
}

bool Relaunch(std::string& error) {
  const Layout layout = GetLayout();
#if defined(_WIN32)
  // Keep the arguments the game was started with.
  std::wstring command_line = GetCommandLineW();
  size_t end = 0;
  if (!command_line.empty() && command_line[0] == L'"') {
    end = command_line.find(L'"', 1);
    end = end == std::wstring::npos ? command_line.size() : end + 1;
  } else {
    end = command_line.find(L' ');
    if (end == std::wstring::npos) end = command_line.size();
  }
  std::wstring command = L"\"" + layout.executable.wstring() + L"\"" + command_line.substr(end) +
                         L" --update_wait_pid=" + std::to_wstring(GetCurrentProcessId());
  STARTUPINFOW startup{};
  startup.cb = sizeof(startup);
  PROCESS_INFORMATION process{};
  const auto cwd = fs::current_path();
  if (!CreateProcessW(layout.executable.c_str(), command.data(), nullptr, nullptr, FALSE, 0, nullptr,
                      cwd.c_str(), &startup, &process)) {
    error = "Please start the game again to finish the update.";
    return false;
  }
  CloseHandle(process.hThread);
  CloseHandle(process.hProcess);
  return true;
#else
  const std::string wait = "--update_wait_pid=" + std::to_string(getpid());
  std::vector<std::string> storage;
#if defined(__APPLE__)
  storage = {"/usr/bin/open", "-n", layout.executable.string(), "--args", wait};
#else
  storage = {layout.executable.string()};
  // Keep the arguments the game was started with.
  std::ifstream cmdline("/proc/self/cmdline", std::ios::binary);
  std::string arg;
  bool first = true;
  while (std::getline(cmdline, arg, '\0')) {
    if (!first && arg.rfind("--update_wait_pid", 0) != 0) storage.push_back(arg);
    first = false;
  }
  storage.push_back(wait);
#endif
  std::vector<char*> argv;
  for (auto& value : storage) argv.push_back(value.data());
  argv.push_back(nullptr);
  pid_t child = -1;
  if (posix_spawn(&child, storage[0].c_str(), nullptr, nullptr, argv.data(), environ) != 0) {
    error = "Please start the game again to finish the update.";
    return false;
  }
  return true;
#endif
}

void FinishPreviousUpdate() {
  const int pid = REXCVAR_GET(update_wait_pid);
  if (pid > 0) {
#if defined(_WIN32)
    if (HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, DWORD(pid))) {
      WaitForSingleObject(process, 15000);
      CloseHandle(process);
    }
#else
    for (int i = 0; i < 150 && kill(pid_t(pid), 0) == 0; ++i)
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
#endif
    REXLOG_INFO("update: now running {}", CurrentVersionDisplay());
  }
  // Replaced program files from an earlier update; retried next time if busy.
  const fs::path staging = GetLayout().container / kStagingName;
  std::error_code ec;
  if (fs::exists(staging, ec)) fs::remove_all(staging, ec);
}

void OpenInBrowser(const std::string& url) {
  if (url.rfind("https://", 0) != 0) return;
#if defined(_WIN32)
  ShellExecuteW(nullptr, L"open", Widen(url).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
#else
#if defined(__APPLE__)
  std::vector<std::string> storage = {"/usr/bin/open", url};
#else
  std::vector<std::string> storage = {"/usr/bin/xdg-open", url};
#endif
  std::vector<char*> argv;
  for (auto& value : storage) argv.push_back(value.data());
  argv.push_back(nullptr);
  pid_t child = -1;
  posix_spawn(&child, storage[0].c_str(), nullptr, nullptr, argv.data(), environ);
#endif
}

}  // namespace skate3::updater
