#pragma once

#include "skate3_native_scene.h"

#include <optional>
#include <utility>
#include <vector>

namespace skate3::native_scene {

// Host capacity only. Never reuse draw values, guest pointers or validation
// results. Each batch travels with an exclusively owned recycled scene vector.
class WorldDrawBuffers {
 public:
  static constexpr size_t kMaxBuffers = 8192;
  static constexpr size_t kMaxBytes = 8u << 20;
  WorldDrawBuffers() = default;
  WorldDrawBuffers(WorldDrawBuffers&& other) noexcept
      : bytes_(std::exchange(other.bytes_, 0)), buffers_(std::move(other.buffers_)) {}
  WorldDrawBuffers& operator=(WorldDrawBuffers&& other) noexcept {
    bytes_ = std::exchange(other.bytes_, 0);
    buffers_ = std::move(other.buffers_);
    return *this;
  }

  void Take(DrawItem& item) {
    if (buffers_.empty()) return;
    item.draws = std::move(buffers_.back());
    buffers_.pop_back();
    bytes_ -= item.draws.capacity() * sizeof(DrawEntry);
  }

  void Recycle(std::vector<DrawItem>& items) {
    for (auto& item : items) {
      if (item.dyn_entity || item.skinned || item.ropa || item.cloth_quads) continue;
      const size_t bytes = item.draws.capacity() * sizeof(DrawEntry);
      if (!bytes || buffers_.size() >= kMaxBuffers || bytes > kMaxBytes - bytes_) continue;
      item.draws.clear();
      buffers_.push_back(std::move(item.draws));
      bytes_ += bytes;
    }
  }

  size_t size() const { return buffers_.size(); }
  size_t bytes() const { return bytes_; }

 private:
  size_t bytes_ = 0;
  std::vector<std::vector<DrawEntry>> buffers_;
};

// NSV builds world items directly in their final scene slot. Failed, skipped
// and separately owned sort-list items roll back that slot on every exit path.
// The legacy branch preserves Skate 3's local-build/move sequence.
class SceneWorldItemSlot {
 public:
  SceneWorldItemSlot(std::vector<DrawItem>& items, bool direct,
                     WorldDrawBuffers& buffers)
      : items_(items), direct_(direct) {
    if (direct_) {
      items_.emplace_back();
      buffers.Take(items_.back());
    } else {
      local_.emplace();
    }
  }
  ~SceneWorldItemSlot() {
    if (direct_ && !kept_) items_.pop_back();
  }
  SceneWorldItemSlot(const SceneWorldItemSlot&) = delete;
  SceneWorldItemSlot& operator=(const SceneWorldItemSlot&) = delete;
  DrawItem& item() { return direct_ ? items_.back() : *local_; }
  void Keep() {
    if (!direct_) items_.push_back(std::move(*local_));
    kept_ = true;
  }

 private:
  std::vector<DrawItem>& items_;
  bool direct_;
  bool kept_ = false;
  std::optional<DrawItem> local_;
};

}  // namespace skate3::native_scene
