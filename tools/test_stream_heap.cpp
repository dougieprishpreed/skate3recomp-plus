#include "../src/skate3_stream_heap.h"
#include <cassert>
#include <iostream>

template <typename Rule>
void CheckRule(Rule rule, const std::array<uint32_t, 5>& original, uint32_t expanded) {
  auto row = original;
  assert(!rule(false, row) && row == original);
  assert(rule(true, row) && row[4] == expanded);
  for (unsigned i = 0; i < 4; ++i) assert(row[i] == original[i]);
  assert(!rule(true, row)); // No repeated growth.
  for (unsigned i = 0; i < 5; ++i) {
    row = original;
    row[i] ^= 1; // Unknown build, heap, allocator type, parent or size.
    const auto changed = row;
    assert(!rule(true, row) && row == changed);
  }
}

int main() {
  using namespace skate3::world_loading;
  const std::array<uint32_t, 5> streamer{0x82175E68, 16, 0, 1, 39 * 1024 * 1024};
  const std::array<uint32_t, 5> audio{0x82175EBC, 0x17, 0, 1, 30 * 1024 * 1024};
  CheckRule(ExpandStreamerHeapConfig, streamer, 64 * 1024 * 1024);
  CheckRule(ExpandAudioHeapConfig, audio, 34 * 1024 * 1024);
  // Each rule accepts only its own row.
  auto row = audio;
  assert(!ExpandStreamerHeapConfig(true, row) && row == audio);
  row = streamer;
  assert(!ExpandAudioHeapConfig(true, row) && row == streamer);
  std::cout << "Streaming and audio heap config scope and repeat guards passed.\n";
}
