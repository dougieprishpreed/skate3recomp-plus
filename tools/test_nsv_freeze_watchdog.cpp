// Offline detector + real Windows suspension/unwind checks. Never runs Skate 3.
#if defined(_WIN32)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif
#include "../src/skate3_freeze_watchdog.h"
#include "../src/skate3_thread_snapshot.h"

#include <cassert>
#include <iostream>

#if defined(_WIN32)
HANDLE ready_event, finish_event;
__declspec(noinline) DWORD WINAPI WaitingWorker(void*) {
  SetEvent(ready_event);
  WaitForSingleObject(finish_event, INFINITE);
  return 0;
}
void TestThreadSnapshots() {
  ready_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  finish_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  HANDLE worker = CreateThread(nullptr, 0, WaitingWorker, nullptr, 0, nullptr);
  assert(worker && ready_event && finish_event);
  assert(WaitForSingleObject(ready_event, 5000) == WAIT_OBJECT_0);
  const auto* teb = skate3::freeze_capture::ThreadTeb(worker);
  assert(teb);
  for (int i = 0; i < 100; ++i) {
    skate3::freeze_capture::ThreadStack stack;
    skate3::freeze_capture::SnapshotThread(worker, teb, &stack);
    assert(stack.count >= 2 && stack.count <= skate3::freeze_capture::kStackFrames);
    assert(stack.suspend_before == 0 && stack.resume_result == 1);
  }
  // Preserve an existing intentional suspension; the probe owns only one count.
  assert(SuspendThread(worker) == 0);
  skate3::freeze_capture::ThreadStack suspended;
  skate3::freeze_capture::SnapshotThread(worker, teb, &suspended);
  assert(suspended.suspend_before == 1 && suspended.resume_result == 2);
  assert(ResumeThread(worker) == 1);

  // A corrupt or disappearing TEB fails cleanly and still resumes the target.
  skate3::freeze_capture::ThreadStack invalid;
  skate3::freeze_capture::SnapshotThread(worker, reinterpret_cast<const uint8_t*>(0x10000), &invalid);
  assert(invalid.count == 0 && invalid.suspend_before == 0 && invalid.resume_result == 1);
  skate3::freeze_capture::ThreadStack missing;
  skate3::freeze_capture::SnapshotThread(INVALID_HANDLE_VALUE, teb, &missing);
  assert(missing.count == 0 && missing.suspend_before == DWORD(-1));
  SetEvent(finish_event);
  assert(WaitForSingleObject(worker, 5000) == WAIT_OBJECT_0);
  CloseHandle(worker);
  CloseHandle(ready_event);
  CloseHandle(finish_event);
  std::cout << "Windows thread checks: 100 valid unwinds, prior suspension preserved, "
               "bad TEB/handle handled, worker resumed and exited.\n";
}
#endif

int main() {
  using skate3::freeze_capture::Watchdog;
  Watchdog detector;
  assert(!detector.Poll(20'000'000, 0, 0, true)); // no game update observed
  assert(!detector.Poll(10'999'999, 1'000'000, 1, true));
  assert(detector.Poll(11'000'000, 1'000'000, 1, true));
  assert(!detector.Poll(30'000'000, 1'000'000, 1, true)); // one capture per stall
  assert(!detector.Poll(30'000'001, 30'000'000, 2, true)); // recovery rearms
  assert(detector.Poll(40'000'000, 30'000'000, 2, true));
  // Menus, native maps, shutdown and disabled capture are all ineligible.
  for (int i = 0; i < 4; ++i) {
    Watchdog gated;
    assert(!gated.Poll(60'000'000, 1'000'000, 1, false));
    assert(!gated.Poll(60'000'001, 60'000'000, 2, true));
  }
  // Three hours of stationary play and regularly completed updates cannot trip.
  Watchdog idle;
  for (uint64_t frame = 1; frame <= 60 * 60 * 3 * 60; ++frame) {
    const int64_t now = 1'000'000 + frame * 16'667;
    assert(!idle.Poll(now + 10'000, now, frame, true));
  }
  assert(!idle.Poll(1, 10, 1, true)); // clock snapshot ahead of observer
  std::cout << "Watchdog checks: threshold, latching, recovery, gates, clock skew and "
               "648000 stationary frames passed.\n";
#if defined(_WIN32)
  TestThreadSnapshots();
#endif
}
