#pragma once

#include <cstdint>

namespace skate3::release_budget {
// The guest consumes up to 20 entries per call. Keep both gameplay work and
// the resulting retire-thread backlog bounded, even if retirement runs slowly.
inline constexpr uint32_t kBatchSize = 20;
inline constexpr uint32_t kMaxEntries = 240;
inline constexpr double kBudgetMs = 2.0;

struct Progress {
  bool valid = false;
  bool sim_pending = false;
  uint32_t submitted = 0, processed = 0, retired = 0, target = 0;
};
struct Result {
  uint32_t extra_calls = 0;
  const char* stop = "inactive";
};

// Called AFTER the mandatory vanilla batch. Elapsed includes that batch.
// run_batch must invoke the original guest processor, including its locks and
// epoch eligibility checks. This policy never advances an epoch or a fence.
template <typename ReadProgress, typename RunBatch, typename ElapsedMs>
Result DrainExtra(uint32_t initial_processed, ReadProgress read,
                  RunBatch run_batch, ElapsedMs elapsed_ms) {
  Result result;
  for (;;) {
    const auto p = read();
    if (!p.valid || p.retired > p.processed || p.processed > p.submitted ||
        p.target > p.submitted || p.processed < initial_processed) {
      result.stop = "invalid-snapshot";
      break;
    }
    if (!p.sim_pending || p.target <= p.retired) {
      result.stop = "no-sim-fence";
      break;
    }
    if (p.processed >= p.target) {
      result.stop = "await-retire";
      break;
    }
    if (p.processed - initial_processed > kMaxEntries - kBatchSize ||
        result.extra_calls >= kMaxEntries / kBatchSize - 1) {
      result.stop = "entry-budget";
      break;
    }
    if (p.processed - p.retired > kMaxEntries - kBatchSize) {
      result.stop = "retire-backlog";
      break;
    }
    if (elapsed_ms() >= kBudgetMs) {
      result.stop = "time-budget";
      break;
    }
    run_batch();
    ++result.extra_calls;
    const auto after = read();
    if (!after.valid || after.processed <= p.processed) {
      // In particular, an ineligible epoch must not cause a busy loop.
      result.stop = "no-progress";
      break;
    }
  }
  return result;
}
}  // namespace skate3::release_budget
