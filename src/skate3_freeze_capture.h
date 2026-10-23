#pragma once

namespace skate3::freeze_capture {
bool Enabled();

// Scope only the real, existing New San Van gameplay update. No guest calls or
// guest state changes. It counts returns even when the player is standing still.
class GameplayUpdate {
 public:
  explicit GameplayUpdate(bool update);
  ~GameplayUpdate();
  GameplayUpdate(const GameplayUpdate&) = delete;
  GameplayUpdate& operator=(const GameplayUpdate&) = delete;
 private:
  bool active_ = false;
};

// Recorder thread (~10 Hz); no new high-frequency profiler needed. A stall or
// Delete press takes three bounded snapshots, 500 ms apart, without waiting here.
void Poll(bool native_recent, bool menu_paused);
void Request();
void Shutdown();
// Window paint stalls on any screen (skate3_paint_stall_capture): its own
// 10 Hz watcher thread, idle while the switch is off. Started from OnPostSetup.
void StartPaintStallWatch();
}  // namespace skate3::freeze_capture
