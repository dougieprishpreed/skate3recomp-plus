#if defined(_WIN32)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#endif

#include "skate3_freeze_capture.h"
#include "skate3_freeze_watchdog.h"
#include "skate3_thread_snapshot.h"
#include "skate3_world_loading.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <ctime>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <memory>
#include <string>
#include <vector>

#include <rex/cvar.h>
#include <rex/logging.h>

REXCVAR_DEFINE_BOOL(skate3_sim_freeze_capture, false, "Skate 3",
    "New San Van freeze diagnostics: capture thread stacks after ten seconds "
    "without a completed gameplay update, or on Delete. Writes small logs/nsv_freeze_* "
    "text files; leaves guest execution and spawning unchanged. Windows only.")
    .lifecycle(rex::cvar::Lifecycle::kHotReload);

namespace skate3::freeze_capture {
namespace {
int64_t NowUs() {
  return std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}
std::atomic<int64_t> last_completed_us{0};
std::atomic<uint64_t> entered{0}, completed{0};
std::atomic<uint32_t> gameplay_thread{0};
std::atomic<bool> manual_requested{false}, shutting_down{false};

#if defined(_WIN32)
struct Target {
  HANDLE handle = nullptr;
  DWORD id = 0;
  const uint8_t* teb = nullptr;
  std::string name;
  uint64_t previous_cpu = 0;
  ~Target() { if (handle) CloseHandle(handle); }
};
struct Module {
  uint64_t base = 0, size = 0;
  std::string name;
};
uint64_t ThreadCpu(HANDLE thread) {
  FILETIME creation{}, exit{}, kernel{}, user{};
  if (!GetThreadTimes(thread, &creation, &exit, &kernel, &user)) return 0;
  return (uint64_t(kernel.dwHighDateTime) << 32) + kernel.dwLowDateTime +
         (uint64_t(user.dwHighDateTime) << 32) + user.dwLowDateTime;
}
std::string ThreadName(HANDLE thread) {
  PWSTR wide = nullptr;
  std::string name = "unnamed";
  if (SUCCEEDED(GetThreadDescription(thread, &wide)) && wide) {
    char utf8[256]{};
    if (WideCharToMultiByte(CP_UTF8, 0, wide, -1, utf8, sizeof(utf8), nullptr, nullptr) && utf8[0])
      name = utf8;
    LocalFree(wide);
  }
  return name;
}

class Capture {
 public:
  Capture(unsigned number, const char* reason, int64_t now) {
    std::error_code error;
    std::filesystem::create_directories("logs", error);
    path_ = "logs/nsv_freeze_" + std::to_string(std::time(nullptr)) + "_" +
            std::to_string(number) + ".txt";
    file_.open(path_);
    if (!file_) {
      REXLOG_ERROR("sim-freeze: cannot create {}", path_);
      return;
    }
    file_ << "NSV_FREEZE_V1 reason=" << reason << " pid=" << GetCurrentProcessId()
          << " gameplay_tid=" << gameplay_thread.load() << '\n';
    HANDLE modules = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, GetCurrentProcessId());
    if (modules != INVALID_HANDLE_VALUE) {
      MODULEENTRY32W entry{};
      entry.dwSize = sizeof(entry);
      for (BOOL ok = Module32FirstW(modules, &entry); ok; ok = Module32NextW(modules, &entry)) {
        char name[1024]{};
        WideCharToMultiByte(CP_UTF8, 0, entry.szModule, -1, name, sizeof(name), nullptr, nullptr);
        modules_.push_back({reinterpret_cast<uint64_t>(entry.modBaseAddr), entry.modBaseSize, name});
        file_ << "module name=" << name << " base=0x" << std::hex
              << reinterpret_cast<uint64_t>(entry.modBaseAddr) << " size=0x"
              << entry.modBaseSize << std::dec << '\n';
      }
      CloseHandle(modules);
    }
    HANDLE threads = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (threads != INVALID_HANDLE_VALUE) {
      THREADENTRY32 entry{};
      entry.dwSize = sizeof(entry);
      for (BOOL ok = Thread32First(threads, &entry); ok; ok = Thread32Next(threads, &entry)) {
        if (entry.th32OwnerProcessID != GetCurrentProcessId() ||
            entry.th32ThreadID == GetCurrentThreadId()) continue;
        if (targets_.size() == 128) { file_ << "thread_limit=128\n"; break; }
        auto target = std::make_unique<Target>();
        target->id = entry.th32ThreadID;
        target->handle = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
            THREAD_QUERY_INFORMATION, FALSE, target->id);
        if (!target->handle) {
          file_ << "unavailable tid=" << target->id << " error=" << GetLastError() << '\n';
          continue;
        }
        target->teb = ThreadTeb(target->handle);
        target->name = ThreadName(target->handle);
        target->previous_cpu = ThreadCpu(target->handle);
        targets_.push_back(std::move(target));
      }
      CloseHandle(threads);
    }
    const auto game_tid = gameplay_thread.load();
    std::stable_sort(targets_.begin(), targets_.end(), [game_tid](const auto& a, const auto& b) {
      return a->id == game_tid && b->id != game_tid;
    });
    REXLOG_WARN("sim-freeze: capture begin reason={} gameplay_tid={} threads={} file={}",
                reason, game_tid, targets_.size(), path_);
    next_us_ = now;
  }

  // Called after every target has been resumed; formatting and all logging are
  // outside SnapshotThread. Only three passes per event, not a continuous profiler.
  bool Poll(int64_t now) {
    if (!file_) return true;
    if (now < next_us_) return false;
    ++sample_;
    file_ << "sample=" << sample_ << " steady_us=" << now << " entered=" << entered.load()
          << " completed=" << completed.load() << " completed_age_ms="
          << (now - last_completed_us.load()) / 1000 << '\n';
    for (auto& target : targets_) {
      ThreadStack stack;
      if (target->teb) SnapshotThread(target->handle, target->teb, &stack);
      const uint64_t cpu = ThreadCpu(target->handle);
      const uint64_t delta = cpu >= target->previous_cpu ? cpu - target->previous_cpu : 0;
      target->previous_cpu = cpu;
      file_ << "thread tid=" << target->id << " name=" << target->name << " cpu_delta_ms="
            << double(delta) / 10000 << " frames=" << stack.count << " suspend_before="
            << stack.suspend_before << " resume_result=" << stack.resume_result << '\n';
      if (stack.suspend_before != DWORD(-1) && stack.resume_result == DWORD(-1)) {
        REXLOG_ERROR("sim-freeze: failed to resume diagnostic target tid={}", target->id);
      }
      std::string game_frames;
      for (int i = 0; i < stack.count; ++i) {
        const uint64_t pc = stack.pcs[i];
        file_ << "  pc=0x" << std::hex << pc;
        for (const auto& module : modules_) {
          if (pc < module.base || pc - module.base >= module.size) continue;
          const auto rva = pc - module.base;
          file_ << " module=" << module.name << " rva=0x" << rva;
          if (module.name == "skate3.exe") {
            file_ << " symbol=0x" << 0x140000000ull + rva;
            if (target->id == gameplay_thread.load()) {
              char address[32];
              std::snprintf(address, sizeof(address), "%llX,", 0x140000000ull + rva);
              game_frames += address;
            }
          }
          break;
        }
        file_ << std::dec << '\n';
      }
      if (target->id == gameplay_thread.load()) {
        REXLOG_WARN("sim-freeze: gameplay stack sample={} tid={} cpu_delta_ms={:.3f} frames=[{}]",
                    sample_, target->id, double(delta) / 10000, game_frames);
      }
    }
    file_.flush();
    skate3::world_loading::LogFreezeSnapshot();
    next_us_ = now + 500'000;
    if (sample_ != 3) return false;
    REXLOG_WARN("sim-freeze: capture complete samples=3 file={}", path_);
    return true;
  }

 private:
  std::string path_;
  std::ofstream file_;
  std::vector<Module> modules_;
  std::vector<std::unique_ptr<Target>> targets_;
  int sample_ = 0;
  int64_t next_us_ = 0;
};
#endif
}  // namespace

bool Enabled() {
#if defined(_WIN32)
  return REXCVAR_GET(skate3_sim_freeze_capture) && !shutting_down.load();
#else
  return false;
#endif
}

GameplayUpdate::GameplayUpdate(bool update) {
  active_ = update && Enabled() && skate3::world_loading::IsSkate2Gameplay();
  if (!active_) return;
#if defined(_WIN32)
  gameplay_thread.store(GetCurrentThreadId());
#endif
  if (entered.fetch_add(1) == 0) {
    // Seed the timeout even if the very first observed call never returns.
    last_completed_us.store(NowUs());
    REXLOG_INFO("sim-freeze: watchdog armed (New San Van, timeout=10s; Delete=manual thread capture)");
  }
}
GameplayUpdate::~GameplayUpdate() {
  if (!active_) return;
  last_completed_us.store(NowUs(), std::memory_order_release);
  completed.fetch_add(1, std::memory_order_release);
}

void Request() {
  if (Enabled() && skate3::world_loading::IsSkate2Gameplay()) {
    manual_requested.store(true);
    REXLOG_INFO("sim-freeze: manual capture requested (Delete)");
  }
}
void Shutdown() { shutting_down.store(true); }

void Poll(bool native_recent, bool menu_paused) {
#if defined(_WIN32)
  struct State {
    Watchdog watchdog;
    std::unique_ptr<Capture> capture;
    unsigned captures = 0;
    int64_t next_progress_us = 0;
  };
  // The existing recorder is detached. Do not destroy its state during static
  // teardown; Shutdown stops capture work before the application closes.
  static State* state = nullptr;
  if (!state && !Enabled()) return;
  if (!state) state = new State;
  auto& watchdog = state->watchdog;
  auto& capture = state->capture;
  auto& captures = state->captures;
  if (shutting_down.load()) { capture.reset(); return; }
  const int64_t now = NowUs();
  const bool eligible = Enabled() && native_recent && !menu_paused &&
                        skate3::world_loading::IsSkate2Gameplay();
  if (Enabled() && skate3::world_loading::IsSkate2Gameplay() &&
      now >= state->next_progress_us) {
    state->next_progress_us = now + 10'000'000;
    REXLOG_INFO("sim-freeze: progress entered={} completed={} update_age_ms={} gameplay_tid={} menu_paused={} native_recent={}",
                entered.load(), completed.load(), (now - last_completed_us.load()) / 1000,
                gameplay_thread.load(), menu_paused, native_recent);
  }
  const bool automatic = watchdog.Poll(now, last_completed_us.load(std::memory_order_acquire),
                                       completed.load(std::memory_order_acquire), eligible);
  const bool manual = manual_requested.exchange(false) && Enabled();
  if (!capture && captures < 8 && (automatic || manual)) {
    capture = std::make_unique<Capture>(++captures, manual ? "Delete" : "gameplay-update-stalled", now);
  }
  if (capture && capture->Poll(now)) capture.reset();
#endif
}
}  // namespace skate3::freeze_capture
