"""Exercise renderer comparison policy and sun capture guards without the game."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include "native/skate3_native_performance.h"
#include "native/skate3_native_sun_guard.h"
#include "native/skate3_native_shadow_lifetime.h"
bool skate3_native_performance_skate3 = true;
bool skate3_native_performance_nsv = true;
int main(int argc, char** argv) {
  assert(argc == 3);
  const bool stock = std::atoi(argv[1]) != 0, nsv = std::atoi(argv[2]) != 0;
  skate3_native_performance_skate3 = stock;
  skate3_native_performance_nsv = nsv;
  using namespace skate3::native_scene;
  assert(PerformanceOptimizationsEnabled(false) == stock);
  assert(PerformanceOptimizationsEnabled(true) == nsv);
  // UI preferences may change during play. Queue policy stays fixed until restart.
  skate3_native_performance_skate3 = !stock;
  skate3_native_performance_nsv = !nsv;
  assert(PerformanceOptimizationsEnabled(false) == stock);
  assert(PerformanceOptimizationsEnabled(true) == nsv);
  const float a[3] = {0, 1, 0}, b[3] = {1, 0, 0};
  SunAxisCandidate guard;
  assert(!guard.Confirm(a, 10, true));
  for (int i = 0; i < 1000; ++i) assert(!guard.Confirm(a, 10, true));
  assert(guard.Confirm(a, 11, true));
  assert(guard.Confirm(a, 11, true));  // accepted sample may keep serving
  assert(!guard.Confirm(b, 12, true));
  assert(!guard.Confirm(a, 13, true));  // alternating foreign axes cannot confirm
  assert(guard.Confirm(a, 14, true));
  assert(!guard.Confirm(b, 15, true));
  assert(!guard.Confirm(b, 16, false));  // paused / stale capture discards candidate
  assert(!guard.Confirm(b, 17, true));
  assert(guard.Confirm(b, 18, true));
  assert(!guard.Confirm(b, 100, true));  // no confirmation across long capture gap
  assert(guard.Confirm(b, 101, true));
  assert(!guard.Confirm(b, 99, true));  // rollback/load cannot confirm
  guard = {};
  assert(!guard.Confirm(b, 101, true));  // map-load reset
  assert(guard.Confirm(b, 102, true));
  assert(SunAxisDot(a, a) >= 0.999995f);
  assert(SunAxisDot(a, b) < 0.999995f);  // active atlas must restart on changed sun
  ShadowWorldLifetime lifetime;
  // Downtown -> University -> NSV, with no stale-publish/presence yield.
  assert(lifetime.Update(2, true));
  assert(!lifetime.Update(2, true));
  assert(lifetime.Update(4, true));
  assert(!lifetime.Update(4, true));
  // Loading frames may still submit; NSV proxy admission cannot depend on
  // the gameplay flag (run230 admitted 140 proxies before it was enabled).
  assert(lifetime.Update(7, false));
  for (int i = 0; i < 100; ++i) {
    assert(!lifetime.Update(7, false));
    assert(!PersistentStaticProxyAllowed(7, 11));
    assert(PersistentStaticProxyAllowed(7, 1));
  }
  assert(lifetime.Update(7, true));  // discard loading-period cache at takeover
  assert(!lifetime.Update(7, true));
  assert(!lifetime.Update(7, false));  // ordinary pause keeps completed atlas
  assert(!lifetime.Update(7, true));
  assert(lifetime.Update(9, true));  // same NSV map reload is a new lifetime
  assert(lifetime.Update(10, false));  // back to a retail map
  assert(PersistentStaticProxyAllowed(10, 11));  // retail proxy policy preserved
  assert(lifetime.Update(10, true));
  assert(!lifetime.Update(10, true));
  std::puts("PASS explicit world-load/reset, loading takeover, NSV proxy admission and pause lifetime");
  std::puts("PASS map isolation/restart policy, unique fresh captures, stale/menu reset, axis and load guards");
}
'''


class NativeRendererPolicies(unittest.TestCase):
    def test_production_policy_headers(self):
        compiler = (os.environ.get('CXX') or shutil.which('clang++-20')
                    or shutil.which('clang++') or shutil.which('c++'))
        self.assertIsNotNone(compiler, 'A C++ compiler is required for the native policy fixture')
        with tempfile.TemporaryDirectory(prefix="Skate3 policy user's ") as tmp:
            work = Path(tmp)
            stub = work / 'rex/cvar.h'
            stub.parent.mkdir()
            stub.write_text('#pragma once\n#define REXCVAR_DECLARE(type, name) extern type name\n'
                            '#define REXCVAR_GET(name) (name)\n')
            source = work / 'policy.cpp'
            source.write_text(FIXTURE)
            binary = work / ('policy.exe' if os.name == 'nt' else 'policy')
            subprocess.run([compiler, '-std=c++20', '-O2', '-I', str(work), '-I', str(ROOT / 'src'),
                            str(source), '-o', str(binary)], check=True)
            for stock in (0, 1):
                for nsv in (0, 1):
                    with self.subTest(stock=stock, nsv=nsv):
                        subprocess.run([str(binary), str(stock), str(nsv)], check=True)


if __name__ == '__main__':
    unittest.main()
