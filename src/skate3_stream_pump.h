#pragma once

namespace skate3::stream_pump {
inline constexpr unsigned kMaxPasses = 4;
inline constexpr double kBudgetMs = 2.0;
struct Result { unsigned passes = 0; const char* stop = "ready"; };

// Snapshot supplies valid, waiting and equality. Advance runs existing guest
// state machines, not simulation frames or release epochs. Never busy-wait
// for I/O or another thread: stop as soon as observable progress stops.
template <typename Read, typename Advance, typename Elapsed>
Result Run(Read read, Advance advance, Elapsed elapsed) {
  Result result;
  auto before = read();
  while (before.valid && before.waiting) {
    if (result.passes == kMaxPasses) { result.stop = "pass-budget"; return result; }
    if (elapsed() >= kBudgetMs) { result.stop = "time-budget"; return result; }
    advance();
    ++result.passes;
    auto after = read();
    if (!after.valid) { result.stop = "invalid-snapshot"; return result; }
    if (after == before) { result.stop = "no-progress"; return result; }
    before = after;
  }
  result.stop = before.valid ? "ready" : "invalid-snapshot";
  return result;
}
}  // namespace skate3::stream_pump
