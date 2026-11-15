"""Run the production camera smoother (SmoothCamera) on synthetic camera sampler streams.

log233: the 1 kHz sampler can read a camera state the game never renders (metres off,
position only, for a few ms while a sim update is late). The 50 ms boxcar turned it into a
0.4-0.8 m camera bump, seen as the world or the close-up skater glitching. Here such stray
samples must leave the smoothed camera where it is without them; a stray beyond the 5 m cut
limit must not restart the ring; a real cut must still restart it; fast legitimate motion
and camera shake must keep every sample. Never launches the game.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <mutex>
#include <string>
#include <vector>
#include "native/skate3_native_actor_history.h"
#define REXCVAR_GET(n) (n)
#define REXLOG_INFO(...) ((void)0)
double skate3_native_render_scene_smooth_camera_filter_ms = 50.0;
std::atomic<int> g_synpan_active{0};
struct RawCamSample { double t; float view[16]; float proj[16]; };
std::mutex g_cam_samples_mutex;
std::vector<RawCamSample> g_cam_samples;
double g_smooth_play = 0.0;
bool g_smooth_active = false;
float g_smooth_err_ms = 0.0f, g_smooth_period_ms = 0.0f, g_smooth_age_ms = -1.0f;
uint32_t g_smooth_events = 0;
'''

DRIVER = r'''
struct Truth { float c[3]; float yaw; };
struct Sample { double t; Truth s; bool stray; };
const float kProj[16] = {1.2f, 0, 0, 0, 0, 2.1f, 0, 0, 0, 0, 1.0001f, 1, 0, 0, -0.1f, 0};

// Row-vector view: rows 0..2 = R, row 3 = -c*R (the layout ComposeViewProj builds).
void MakeView(const Truth& s, float view[16]) {
  const float cs = std::cos(s.yaw), sn = std::sin(s.yaw);
  const float R[3][3] = {{cs, 0, -sn}, {0, 1, 0}, {sn, 0, cs}};
  std::memset(view, 0, 64);
  for (int r = 0; r < 3; ++r) for (int k = 0; k < 3; ++k) view[r * 4 + k] = R[r][k];
  for (int k = 0; k < 3; ++k) view[12 + k] = -(s.c[0] * R[0][k] + s.c[1] * R[1][k] + s.c[2] * R[2][k]);
  view[15] = 1;
}

// Sim camera updates at an irregular ~200 Hz; the sampler sees each 0.7 ms later.
// With late_for > 0 the sim skips its updates inside (late_at, late_at + late_for).
std::vector<Sample> Ticks(const std::function<Truth(double)>& path, double t0, double t1,
                          double late_at = 0, double late_for = 0) {
  static const double cadence[4] = {0.004, 0.005, 0.006, 0.005};
  std::vector<Sample> out;
  int i = 0;
  for (double t = t0; t < t1; t += cadence[i++ % 4]) {
    if (late_for > 0 && t > late_at && t < late_at + late_for) continue;
    out.push_back({t + 0.0007, path(t), false});
  }
  return out;
}

// 60 Hz sim camera that falls behind at each `late` time for 33 ms and then
// catches up: the missed updates arrive 1 ms apart just before the next one,
// each at its own place on the route (the Dam drop-in under load).
std::vector<Sample> Bursts(const std::function<Truth(double)>& path, double t0, double t1,
                           const std::vector<double>& late) {
  static const double cadence[4] = {0.0157, 0.0167, 0.0177, 0.0167};
  std::vector<Sample> out;
  std::vector<double> deferred;
  int i = 0;
  for (double t = t0; t < t1; t += cadence[i++ % 4]) {
    bool held = false;
    for (double at : late) held |= t > at && t < at + 0.033;
    if (held) { deferred.push_back(t); continue; }
    for (size_t k = 0; k < deferred.size(); ++k)
      out.push_back({t + 0.0007 - 0.001 * double(deferred.size() - k), path(deferred[k]), false});
    deferred.clear();
    out.push_back({t + 0.0007, path(t), false});
  }
  return out;
}

void AddStray(std::vector<Sample>& samples, const std::function<Truth(double)>& path, double t, float dx, float dy, float dz) {
  Truth s = path(t);
  s.c[0] += dx; s.c[1] += dy; s.c[2] += dz;
  samples.push_back({t, s, true});
  std::sort(samples.begin(), samples.end(), [](const Sample& a, const Sample& b) { return a.t < b.t; });
}

// Adaptive smoothing strength per frame (time since t0), when a scenario sets one.
std::function<double(double)> g_scale_at;

// 60 Hz frames: drain the samples due, read the latest COMPLETED camera as the frame
// camera (never a stray), smooth, print t cam smoothed events raw.
void Run(const std::vector<Sample>& samples, double t0, double t1) {
  size_t next = 0;
  Truth frame = samples.front().s;
  for (double now = t0; now < t1; now += 1.0 / 60.0) {
    {
      std::lock_guard<std::mutex> lock(g_cam_samples_mutex);
      for (; next < samples.size() && samples[next].t <= now; ++next) {
        RawCamSample raw;
        raw.t = samples[next].t;
        MakeView(samples[next].s, raw.view);
        std::memcpy(raw.proj, kProj, sizeof(kProj));
        g_cam_samples.push_back(raw);
        if (!samples[next].stray) frame = samples[next].s;
      }
    }
    float view[16];
    MakeView(frame, view);
    CamPose pose;
    pose.t = now;
    QuatFromView(view, pose.q);
    std::memcpy(pose.c, frame.c, sizeof(pose.c));
    float raw_vp[16], raw_cam[3], vp[16], cam[3];
    ComposeViewProj(pose, kProj, raw_vp, raw_cam);
    // Unsmoothed frames keep the game's camera, as BuildFrameScene does.
    std::memcpy(vp, raw_vp, sizeof(vp));
    std::memcpy(cam, frame.c, sizeof(cam));
    g_smooth_events = 0;
    if (g_scale_at) g_smooth_scale = g_scale_at(now - t0);
    const bool smoothed = SmoothCamera(view, kProj, raw_vp, frame.c, now, now, vp, cam);
    std::printf("%.6f %.6f %.6f %.6f %d %u %.6f %.6f %.6f\n", now - t0, cam[0], cam[1], cam[2],
                smoothed ? 1 : 0, g_smooth_events, frame.c[0], frame.c[1], frame.c[2]);
  }
}

int main(int argc, char** argv) {
  const std::string name = argc > 1 ? argv[1] : "";
  const double t0 = 1000.0, t1 = 1003.0, at = 1001.5;
  const auto straight = [=](double t) {
    const float u = float(t - t0);
    return Truth{{1000 + 9 * u, 15 - 0.5f * u, 300 + 2 * u}, 0.3f + 0.4f * u};
  };
  const auto cut = [=](double t) {
    Truth s = straight(t);
    if (t >= at) s.c[0] += 10;
    return s;
  };
  const auto orbit = [=](double t) {
    const float u = float(t - t0), w = 6.2831853f;  // 360 deg/s around a skater 3 m away
    return Truth{{900 + 3 * std::cos(w * u), 20 + 0.4f * std::sin(3 * w * u) + 0.05f * std::sin(94.2f * u),
                  -400 + 3 * std::sin(w * u)}, -w * u + 0.8f * std::sin(5 * u)};
  };
  const auto dam = [=](double t) {  // 35 m/s down a curving slope
    const float u = float(t - t0);
    return Truth{{500 + 35 * u, 200 - 12 * u, -300 + 2.0f * u * u}, 0.1f * u + 0.05f * std::sin(3 * u)};
  };
  const std::vector<double> dam_late = {at - 0.4, at, at + 0.5};
  const auto glitch = [=](double t) {  // a speed glitch: 300 m/s, curving (40 m/s^2 sideways)
    const float u = float(t - t0);
    return Truth{{0 + 300 * u, 50 - 2 * u, 100 + 20 * u * u}, 0.02f * u};
  };
  const auto glitch_cut = [=](double t) {  // ...teleported 40 m sideways at 1.5 s
    Truth s = glitch(t);
    if (t >= at) s.c[2] += 40;
    return s;
  };
  const auto snap = [=](double t) {  // the camera snaps 34 degrees at 1.5 s (a respawn)
    Truth s = straight(t);
    if (t >= at) s.yaw += 0.6f;
    return s;
  };
  std::vector<Sample> samples;
  if (name == "snap") samples = Ticks(snap, t0, t1);
  else if (name == "glitch") samples = Bursts(glitch, t0, t1, dam_late);
  else if (name == "glitch_cut") samples = Bursts(glitch_cut, t0, t1, {});
  else if (name == "dam") samples = Bursts(dam, t0, t1, dam_late);
  else if (name == "dam_stray") { samples = Bursts(dam, t0, t1, dam_late); AddStray(samples, dam, at + 0.0012, 0.0f, 0.0f, 0.8f); }
  else if (name == "late") samples = Ticks(straight, t0, t1, at, 0.010);
  else if (name == "late_stray") { samples = Ticks(straight, t0, t1, at, 0.010); AddStray(samples, straight, at + 0.0012, 0.0f, 3.5f, 0.0f); }
  else if (name == "steady") samples = Ticks(straight, t0, t1);
  else if (name == "big_stray") { samples = Ticks(straight, t0, t1); AddStray(samples, straight, at + 0.0012, 7.0f, 0.0f, 0.0f); }
  else if (name == "cut") samples = Ticks(cut, t0, t1);
  else if (name == "orbit") samples = Ticks(orbit, t0, t1);
  else if (name == "k0") {  // adaptive smoothing at rest: the game's own camera
    samples = Bursts(straight, t0, t1, {});
    g_scale_at = [](double) { return 0.0; };
  } else if (name == "ramp") {  // ...building up to half the full smoothing at 1.0 s
    samples = Bursts(straight, t0, t1, {});
    g_scale_at = [](double u) { return u < 1.0 ? 0.0 : std::min(0.5, (u - 1.0) * 0.024 * 60.0); };
  }
  else return 2;
  Run(samples, t0, t1);
  return 0;
}
'''


def section(text, start, end):
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


def fixture(scene_text):
    """The production camera helpers and SmoothCamera between the test's stubs and driver."""
    helpers = section(scene_text, '// Standard 3x3 <-> quaternion pair', '// Shared playback clock')
    compose = section(scene_text, '// Rebuild a row-vector view matrix from pose', '// Drone / free-fly camera')
    smoother = section(scene_text, 'double g_latest_cam_tick = 0.0;',
                       '// Minimum time spacing (seconds) between dynamic-pose ring ingests.')
    return PRELUDE + helpers + 'CamPose g_smooth_pose;\n' + compose + smoother + DRIVER


def run_scenarios(scene_text, work):
    """{scenario: [(t, (x, y, z), smoothed, events)]} for the given scene source."""
    compiler = os.environ.get('CXX') or shutil.which('clang++-20') or shutil.which('clang++') or shutil.which('c++')
    if not compiler:
        raise RuntimeError('Install the build compiler before the camera smoothing check')
    source = work / 'camera_smoothing.cpp'
    source.write_text(fixture(scene_text), encoding='utf8')
    binary = work / ('camera_smoothing.exe' if os.name == 'nt' else 'camera_smoothing')
    subprocess.run([compiler, '-std=c++20', '-O2', '-I', str(ROOT / 'src'), str(source), '-o', str(binary)], check=True)
    out = {}
    for name in ('steady', 'late', 'late_stray', 'big_stray', 'cut', 'orbit', 'dam', 'dam_stray',
                 'glitch', 'glitch_cut', 'snap', 'k0', 'ramp'):
        text = subprocess.run([str(binary), name], capture_output=True, text=True, check=True).stdout
        rows = []
        for line in text.splitlines():
            t, x, y, z, smoothed, events, rx, ry, rz = line.split()
            rows.append((float(t), (float(x), float(y), float(z)), smoothed == '1', int(events),
                         (float(rx), float(ry), float(rz))))
        out[name] = rows
    return out


def largest_difference(a, b):
    return max(sum((p - q) ** 2 for p, q in zip(ra[1], rb[1])) ** .5 for ra, rb in zip(a, b))


OUTLIER, RING_JUMP, RING_TURN = 512, 16, 32


class CameraSmoothingTest(unittest.TestCase):
    def test_stray_camera_samples(self):
        with tempfile.TemporaryDirectory(prefix="camera smoothing user's ") as temp:
            runs = run_scenarios((ROOT / 'src/skate3_native_scene.cpp').read_text(encoding='utf8'), Path(temp))
        settled = lambda rows: [r for r in rows if r[0] > 0.3]
        # A stray sample 3.5 m high while the sim is 10 ms late: no camera bump.
        bump = largest_difference(runs['late'], runs['late_stray'])
        self.assertLess(bump, 0.02, bump)
        self.assertTrue(any(r[3] & OUTLIER for r in runs['late_stray']))
        # A stray 7 m off (past the 5 m cut limit): no ring restart, no raw frames, no bump.
        self.assertTrue(all(r[2] for r in settled(runs['big_stray'])))
        self.assertFalse(any(r[3] & RING_JUMP for r in runs['big_stray']))
        bump = largest_difference(runs['steady'], runs['big_stray'])
        self.assertLess(bump, 0.02, bump)
        # A real 10 m cut at 1.5 s still restarts the ring within the 20 ms hold.
        jumps = [r[0] for r in runs['cut'] if r[3] & RING_JUMP]
        self.assertEqual(len(jumps), 1)
        self.assertTrue(1.5 <= jumps[0] <= 1.5 + 0.06 + 1 / 60, jumps)
        self.assertFalse(any(r[3] & OUTLIER for r in runs['cut']))
        after = [r for r in runs['cut'] if r[0] > 1.65]
        self.assertTrue(all(r[2] for r in after))
        # Fast legitimate motion (360 deg/s orbit, bobbing, 15 Hz shake) keeps every sample.
        self.assertFalse(any(r[3] & OUTLIER for r in runs['orbit']))
        self.assertTrue(all(r[2] for r in settled(runs['orbit'])))
        # 35 m/s with catch-up bursts (real samples 1 ms apart, a tick further along) keeps
        # every sample; a stray 0.8 m sideways at that speed still moves nothing.
        self.assertFalse(any(r[3] & OUTLIER for r in runs['dam']))
        self.assertTrue(all(r[2] for r in settled(runs['dam'])))
        self.assertTrue(any(r[3] & OUTLIER for r in runs['dam_stray']))
        bump = largest_difference(runs['dam'], runs['dam_stray'])
        self.assertLess(bump, 0.02, bump)
        # A speed glitch: 300 m/s (5 m per update, 10 m in a catch-up burst) never
        # restarts the ring, drops a sample or falls back to raw frames ...
        for name in ('glitch',):
            self.assertFalse(any(r[3] & (OUTLIER | RING_JUMP) for r in runs[name]), name)
            self.assertTrue(all(r[2] for r in settled(runs[name])), name)
        # ... and a 40 m sideways teleport at that speed is still a cut.
        jumps = [r[0] for r in runs['glitch_cut'] if r[3] & RING_JUMP]
        self.assertEqual(len(jumps), 1, jumps)
        self.assertTrue(1.5 <= jumps[0] <= 1.5 + 0.06 + 1 / 60, jumps)
        self.assertFalse(any(r[3] & OUTLIER for r in runs['glitch_cut']))
        # A 34 degree snap in one update is a cut; the 360 deg/s orbit never is.
        turns = [r[0] for r in runs['snap'] if r[3] & RING_TURN]
        self.assertEqual(len(turns), 1, turns)
        self.assertTrue(1.5 <= turns[0] <= 1.5 + 0.06 + 1 / 60, turns)
        self.assertFalse(any(r[3] & (RING_TURN | RING_JUMP) for r in settled(runs['orbit'])))
        # Adaptive smoothing at strength 0 is the game's own camera; building up to half
        # strength never jumps (no frame moves more than the game's own frames do) and
        # settles about half the full delay (~21 ms at 9.2 m/s = 0.19 m) behind.
        dist = lambda a, b: sum((p - q) ** 2 for p, q in zip(a, b)) ** .5
        k0 = [dist(r[1], r[4]) for r in settled(runs['k0'])]
        self.assertLess(max(k0), 0.001, max(k0))
        ramp = runs['ramp']
        moves = [dist(b[1], a[1]) for a, b in zip(ramp, ramp[1:]) if b[0] > 0.3]
        raw_moves = [dist(b[4], a[4]) for a, b in zip(ramp, ramp[1:]) if b[0] > 0.3]
        self.assertLess(max(moves), 1.2 * max(raw_moves), (max(moves), max(raw_moves)))
        behind = [dist(r[1], r[4]) for r in ramp if r[0] > 1.8]
        self.assertTrue(0.12 < min(behind) and max(behind) < 0.27, (min(behind), max(behind)))
        print('PASS camera smoothing: stray samples (3.5 m late-sim, 7 m past the cut limit, 0.8 m at 35 m/s) '
              'move nothing; real cuts (10 m ahead, 40 m sideways at 300 m/s, a 34 deg snap) restart within the hold; '
              '360 deg/s orbit, shake, 35 m/s and 300 m/s catch-up bursts keep every sample; adaptive smoothing '
              'at 0 is the game camera and builds to half strength without a jump')


if __name__ == '__main__':
    unittest.main()
