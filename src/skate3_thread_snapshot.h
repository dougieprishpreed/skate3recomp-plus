#pragma once

// Windows-only, bounded stack collection for a rare diagnostic capture. No
// logging, allocation, module lookup or application locks while suspended.
#if defined(_WIN32)
#include <windows.h>
#include <cstdint>

namespace skate3::freeze_capture {
constexpr int kStackFrames = 64;
struct ThreadStack {
  uint64_t pcs[kStackFrames]{};
  int count = 0;
  DWORD suspend_before = DWORD(-1);
  DWORD resume_result = DWORD(-1);
};

inline const uint8_t* ThreadTeb(HANDLE thread) {
  struct BasicInformation {
    LONG exit_status;
    PVOID teb;
    HANDLE pid, tid;
    ULONG_PTR affinity;
    LONG priority, base_priority;
  };
  using QueryFn = LONG(NTAPI*)(HANDLE, int, PVOID, ULONG, PULONG);
  static const auto query = reinterpret_cast<QueryFn>(
      GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQueryInformationThread"));
  BasicInformation info{};
  return query && query(thread, 0, &info, sizeof(info), nullptr) == 0
             ? static_cast<const uint8_t*>(info.teb) : nullptr;
}

using LookupFn = PRUNTIME_FUNCTION(NTAPI*)(DWORD64, PDWORD64, PUNWIND_HISTORY_TABLE);
using UnwindFn = PEXCEPTION_ROUTINE(NTAPI*)(DWORD, DWORD64, DWORD64, PRUNTIME_FUNCTION,
                                         PCONTEXT, PVOID*, PDWORD64,
                                         PKNONVOLATILE_CONTEXT_POINTERS);
inline volatile LookupFn lookup_fn = RtlLookupFunctionEntry;
inline volatile UnwindFn unwind_fn = RtlVirtualUnwind;

// Opaque calls plus noinline retain the SEH scope under optimized Clang builds.
// Check the target's current TEB bounds: the game can switch to a fiber stack.
__declspec(noinline) inline void WalkThread(HANDLE thread, const uint8_t* teb,
                                          ThreadStack* out) {
  CONTEXT context{};
  context.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
  uint64_t bounds[2]{};
  SIZE_T copied = 0;
  if (!GetThreadContext(thread, &context) || !teb ||
      !ReadProcessMemory(GetCurrentProcess(), teb + 8, bounds, sizeof(bounds), &copied) ||
      copied != sizeof(bounds)) return;
  __try {
    while (out->count < kStackFrames && context.Rip != 0 &&
           context.Rsp >= bounds[1] && context.Rsp < bounds[0]) {
      out->pcs[out->count++] = context.Rip;
      const DWORD64 previous_rsp = context.Rsp;
      DWORD64 image_base = 0;
      PRUNTIME_FUNCTION function = lookup_fn(context.Rip, &image_base, nullptr);
      if (!function) {
        uint64_t address = 0;
        if (bounds[0] - context.Rsp < sizeof(address) ||
            !ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<void*>(context.Rsp),
                               &address, sizeof(address), &copied) || copied != sizeof(address)) break;
        context.Rip = address;
        context.Rsp += sizeof(address);
      } else {
        PVOID handler = nullptr;
        DWORD64 establisher = 0;
        unwind_fn(UNW_FLAG_NHANDLER, image_base, context.Rip, function, &context,
                  &handler, &establisher, nullptr);
      }
      if (context.Rsp <= previous_rsp) break;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

__declspec(noinline) inline void SnapshotThread(HANDLE thread, const uint8_t* teb,
                                              ThreadStack* out) {
  out->suspend_before = SuspendThread(thread);
  if (out->suspend_before == DWORD(-1)) return;
  __try {
    WalkThread(thread, teb, out);
  } __finally {
    // Exactly one resume restores even a pre-existing guest suspension.
    out->resume_result = ResumeThread(thread);
  }
}
}  // namespace skate3::freeze_capture
#endif
