// Standalone offline safety scenarios; does not load or launch the game.
#include "../src/skate3_release_budget.h"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <iostream>

using namespace skate3::release_budget;
struct Queue {
  Progress p{true, true, 3045, 1375, 1375, 3024};
  bool eligible = true;
  double ms = 0, batch_ms = 0.05;
  uint32_t calls = 0;
  void Batch() {
    ++calls;
    ms += batch_ms;
    if (eligible) p.processed += std::min(kBatchSize, p.submitted - p.processed);
  }
  Result Tick(bool extra = true) {
    const auto initial = p.processed;
    calls = 0;
    ms = 0;
    Batch(); // Always execute the normal call first.
    if (!extra) return {};
    return DrainExtra(initial, [&] { return p; }, [&] { Batch(); }, [&] { return ms; });
  }
};

int main() {
  // Reproduce the first log035 fence, with retirement happening next frame.
  const auto frames_to_fence = [](bool extra) {
    Queue q;
    unsigned frames = 0;
    while (q.p.retired < q.p.target) {
      const auto before = q.p.processed;
      q.Tick(extra);
      assert(q.p.processed - before <= kMaxEntries);
      assert(q.p.processed - q.p.retired <= kMaxEntries);
      q.p.retired = q.p.processed; // Model the original retire phase.
      assert(++frames < 100);
    }
    return frames;
  };
  const auto vanilla = frames_to_fence(false), bounded = frames_to_fence(true);
  assert(vanilla == 83 && bounded == 7);

  // Repeated calls cannot make an unsafe release epoch eligible.
  Queue epochs;
  epochs.eligible = false;
  const auto blocked = epochs.Tick();
  assert(epochs.p.processed == 1375 && epochs.p.retired == 1375);
  assert(blocked.extra_calls == 1 && !std::strcmp(blocked.stop, "no-progress"));

  // No acceleration without a pending collision load or an unmet fence.
  Queue idle;
  idle.p.sim_pending = false;
  assert(idle.Tick().extra_calls == 0 && idle.calls == 1);
  idle.p.sim_pending = true;
  idle.p.target = idle.p.retired;
  assert(idle.Tick().extra_calls == 0);

  // A slow batch cannot trigger unbounded additional work. Budget is soft:
  // one original batch cannot be interrupted, and its duration is included.
  Queue slow;
  slow.batch_ms = 0.8;
  assert(slow.Tick().extra_calls == 2 && slow.calls == 3);
  assert(slow.ms >= kBudgetMs && slow.ms < kBudgetMs + slow.batch_ms);
  Queue very_slow;
  very_slow.batch_ms = 3;
  assert(very_slow.Tick().extra_calls == 0);

  // Avoid shifting the entire queue to the retire thread at once.
  Queue backlog;
  backlog.p.processed += 200;
  assert(!std::strcmp(backlog.Tick().stop, "retire-backlog"));
  assert(backlog.p.processed - backlog.p.retired == kMaxEntries);

  // Stop processing when the target is covered, even if more releases arrive.
  Queue covered;
  covered.p.target = covered.p.processed + 30;
  const auto reached = covered.Tick();
  assert(reached.extra_calls == 1 && !std::strcmp(reached.stop, "await-retire"));
  assert(covered.p.processed < covered.p.submitted);
  assert(covered.p.retired == 1375); // Never claim completion ourselves.

  // Counter reset/race or an unreadable snapshot must not drain more work.
  Queue reset;
  auto invalid = reset.p;
  invalid.processed = 0;
  unsigned unexpected_calls = 0;
  const auto rejected = DrainExtra(1375, [&] { return invalid; },
      [&] { ++unexpected_calls; }, [] { return 0.0; });
  assert(unexpected_calls == 0 && !std::strcmp(rejected.stop, "invalid-snapshot"));

  std::cout << "Release budget safety scenarios passed; simulated fence frames "
            << vanilla << " -> " << bounded << ".\n";
}
