#include "skate3_setup_process.h"
#include <cerrno>
#include <cstring>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#else
#include <fcntl.h>
#include <spawn.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#endif

namespace skate3 {
void EnsureExecutable(const std::filesystem::path& file) {
#if !defined(_WIN32)
  struct stat st {};
  if (::stat(file.c_str(), &st) != 0 || !S_ISREG(st.st_mode) || ::access(file.c_str(), X_OK) == 0)
    return;
  // Execute wherever read is allowed, like the packaged 0755 programs. A failure
  // leaves the spawn to report the real error.
  const auto mode = static_cast<mode_t>(st.st_mode & 07777);
  static_cast<void>(::chmod(file.c_str(), static_cast<mode_t>(mode | ((mode & 0444) >> 2))));
#else
  static_cast<void>(file);
#endif
}

bool RunSetupProcess(const std::filesystem::path& executable,
                     const std::vector<std::string>& arguments,
                     const std::filesystem::path& log_path, std::string& error) {
#if defined(_WIN32)
  auto quote = [](const std::wstring& value) {
    std::wstring out = L"\"";
    size_t slashes = 0;
    for (wchar_t c : value) {
      if (c == L'\\') { ++slashes; continue; }
      out.append(c == L'"' ? slashes * 2 + 1 : slashes, L'\\');
      slashes = 0; out += c;
    }
    out.append(slashes * 2, L'\\'); out += L'"'; return out;
  };
  std::wstring command = quote(executable.wstring());
  for (const auto& arg : arguments)
    command += L" " + quote(std::filesystem::path(std::u8string(arg.begin(), arg.end())).wstring());
  SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
  HANDLE log = CreateFileW(log_path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, &security,
                           CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (log == INVALID_HANDLE_VALUE) { error = "Unable to create setup log."; return false; }
  STARTUPINFOW startup{}; startup.cb = sizeof(startup);
  startup.dwFlags = STARTF_USESTDHANDLES; startup.hStdOutput = log; startup.hStdError = log;
  PROCESS_INFORMATION process{};
  const auto cwd = log_path.parent_path().parent_path();
  bool ok = CreateProcessW(executable.c_str(), command.data(), nullptr, nullptr, TRUE,
                           CREATE_NO_WINDOW, nullptr, cwd.c_str(), &startup, &process) != 0;
  if (ok) {
    ok = WaitForSingleObject(process.hProcess, INFINITE) == WAIT_OBJECT_0;
    DWORD status = 1;
    ok = GetExitCodeProcess(process.hProcess, &status) && ok && status == 0;
    CloseHandle(process.hThread); CloseHandle(process.hProcess);
  }
  CloseHandle(log);
#else
  // posix_spawn is safe when other application threads exist. All paths are absolute;
  // preparation does not depend on the shell or the launcher's working directory.
  std::vector<std::string> storage{executable.string()};
  storage.insert(storage.end(), arguments.begin(), arguments.end());
  std::vector<char*> argv;
  for (auto& arg : storage) argv.push_back(arg.data());
  argv.push_back(nullptr);
  posix_spawn_file_actions_t actions;
  int status = posix_spawn_file_actions_init(&actions);
  if (status) { error = std::strerror(status); return false; }
  status = posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0);
  if (!status) status = posix_spawn_file_actions_addopen(
      &actions, STDOUT_FILENO, log_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
  if (!status) status = posix_spawn_file_actions_adddup2(&actions, STDOUT_FILENO, STDERR_FILENO);
  pid_t child = -1;
  EnsureExecutable(executable);
  if (!status) status = posix_spawn(&child, executable.c_str(), &actions, nullptr, argv.data(), environ);
  posix_spawn_file_actions_destroy(&actions);
  if (status) { error = std::string("Unable to start setup: ") + std::strerror(status); return false; }
  pid_t waited;
  do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
  const bool ok = waited == child && WIFEXITED(status) && WEXITSTATUS(status) == 0;
#endif
  if (!ok) error = "Setup did not complete. Details: " + log_path.string();
  return ok;
}
}  // namespace skate3
