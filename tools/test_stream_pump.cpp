#include "../src/skate3_stream_pump.h"
#include <cassert>
#include <cstring>
#include <iostream>

struct State {
  bool valid = true, waiting = true;
  unsigned phase = 0;
  bool operator==(const State&) const = default;
};
int main() {
  using skate3::stream_pump::Run;
  State s;
  unsigned calls = 0;
  // Queued unload, completed unload callback, load, ready: no fake frame needed.
  auto r = Run([&] { return s; }, [&] {
    ++calls; if (++s.phase == 4) s.waiting = false;
  }, [] { return 0.0; });
  assert(calls == 4 && !s.waiting && !std::strcmp(r.stop, "ready"));
  // I/O, release fence, or an external consumer has not progressed: no spin.
  s = {}; calls = 0;
  r = Run([&] { return s; }, [&] { ++calls; }, [] { return 0.0; });
  assert(calls == 1 && s.waiting && !std::strcmp(r.stop, "no-progress"));
  // Unbounded stream of progress is still capped.
  calls = 0;
  r = Run([&] { return s; }, [&] { ++calls; ++s.phase; }, [] { return 0.0; });
  assert(calls == 4 && !std::strcmp(r.stop, "pass-budget"));
  // Slow passes stop at the soft budget without starting more work.
  calls = 0; double ms = 0;
  r = Run([&] { return s; }, [&] { ++calls; ++s.phase; ms += 1.1; }, [&] { return ms; });
  assert(calls == 2 && !std::strcmp(r.stop, "time-budget"));
  // Normal readiness and invalid snapshots never advance anything.
  calls = 0; s.waiting = false;
  Run([&] { return s; }, [&] { ++calls; }, [] { return 0.0; });
  assert(calls == 0);
  s = {}; s.valid = false;
  Run([&] { return s; }, [&] { ++calls; }, [] { return 0.0; });
  assert(calls == 0);
  s = {};
  r = Run([&] { return s; }, [&] { ++calls; s.valid = false; }, [] { return 0.0; });
  assert(calls == 1 && !std::strcmp(r.stop, "invalid-snapshot"));
  std::cout << "Streaming pump bounded-progress scenarios passed.\n";
}
