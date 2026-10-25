"""PlayerOneMap: the first device used becomes player 1, the only player with a profile.

Compiles a small program against the SDK's rex/input/input.h. Never launches the game.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DRIVER = r'''
#include <rex/input/input.h>
#include <cstdio>
using rex::input::PlayerOneMap;
static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::printf("FAIL line %d: %s\n", __LINE__, #c); ++failures; } } while (0)
int main() {
  {  // Before any input: every player is its own slot.
    PlayerOneMap m;
    for (uint32_t i = 0; i < 4; ++i) CHECK(m.Map(i) == i);
    CHECK(m.player_one_slot() == -1);
    CHECK(!m.ObserveButtons(2, 0));
    CHECK(m.player_one_slot() == -1);
  }
  {  // Slot 3 pressed first: it swaps with slot 1 for the session.
    PlayerOneMap m;
    CHECK(m.ObserveButtons(2, 0x0010));
    CHECK(m.player_one_slot() == 2);
    CHECK(m.Map(0) == 2);
    CHECK(m.Map(2) == 0);
    CHECK(m.Map(1) == 1);
    CHECK(m.Map(3) == 3);
    CHECK(m.Map(0xFF) == 0xFF);
    CHECK(!m.ObserveButtons(0, 0x1000));
    CHECK(m.Map(0) == 2);
  }
  {  // Slot 1 pressed first: nothing moves, as on a console.
    PlayerOneMap m;
    CHECK(m.ObserveButtons(0, 0x1000));
    CHECK(!m.ObserveButtons(1, 0x1000));
    for (uint32_t i = 0; i < 4; ++i) CHECK(m.Map(i) == i);
  }
  {  // Player 1's device gone for kReleaseMs: the next device used takes over.
    PlayerOneMap m;
    CHECK(m.ObserveButtons(1, 0x0010));
    CHECK(!m.ObservePlayerOneConnected(false, 1000));
    CHECK(!m.ObservePlayerOneConnected(false, 1000 + PlayerOneMap::kReleaseMs - 1));
    CHECK(m.Map(0) == 1);
    CHECK(m.ObservePlayerOneConnected(false, 1000 + PlayerOneMap::kReleaseMs));
    CHECK(m.player_one_slot() == -1);
    CHECK(m.Map(0) == 0);
    CHECK(m.ObserveButtons(3, 0x0010));
    CHECK(m.Map(0) == 3);
    CHECK(m.Map(3) == 0);
  }
  {  // A brief dropout keeps the choice.
    PlayerOneMap m;
    CHECK(m.ObserveButtons(1, 0x0010));
    CHECK(!m.ObservePlayerOneConnected(false, 5000));
    CHECK(!m.ObservePlayerOneConnected(true, 5500));
    CHECK(!m.ObservePlayerOneConnected(false, 9000));
    CHECK(!m.ObservePlayerOneConnected(false, 9000 + PlayerOneMap::kReleaseMs - 1));
    CHECK(m.Map(0) == 1);
  }
  std::printf(failures ? "FAILED %d\n" : "ok\n", failures);
  return failures ? 1 : 0;
}
'''


class PlayerOneMapTest(unittest.TestCase):
    def test_first_device_used_is_player_one(self):
        compiler = shutil.which('clang++') or shutil.which('clang++-20')
        if not compiler:
            raise RuntimeError('Install the build compiler before the input player map check')
        with tempfile.TemporaryDirectory(prefix="NSV input user's ") as tmp:
            source = Path(tmp) / 'player_one_map.cpp'
            source.write_text(DRIVER)
            binary = Path(tmp) / ('player_one_map.exe' if sys.platform == 'win32' else 'player_one_map')
            subprocess.run([compiler, '-std=c++23', '-I', str(ROOT / 'third_party/rexglue-sdk/include'),
                            str(source), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
