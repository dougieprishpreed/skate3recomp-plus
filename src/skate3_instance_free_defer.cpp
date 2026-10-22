// Cross-thread use-after-free on Sk8::WorldPresentation instance teardown.
//
// Port of the fix from mchughalex/skate3recomp PR #131 (andrewnakas), without
// that PR's Linux-only pool diagnostics (execinfo/prctl store watch).
//
// sub_82791CF8 (under Sk8::WorldPresentation::AddRenderInstance) allocates two
// u16 arrays at self+496/+500 and publishes INTERIOR pointers into them
// (&A[idx], &B[idx]) into other objects' +64/+68. A Job Manager work item
// captures one and keeps writing 4x u16 records through it. sub_82792040 frees
// both arrays on render_thread without clearing those holders, so a late job
// write can land on a block that is already back on the small-block pool's
// free list - on its link word, which is how 0xFFFFFFFF gets into the free list
// and later crashes the allocator (store through NULL in sub_8298CEF8 during a
// map load).
//
// Mitigation: the blocks that teardown frees are held for
// skate3_instance_free_defer_ms before the pool gets them back, so a late
// write lands on memory nobody has reused. A 512 KB cap bounds the hold, so a
// long job latency cannot starve the pool. The root fix (draining the
// outstanding jobs before teardown) needs a job-manager primitive that has not
// been identified.

#include "generated/skate3_init.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <deque>
#include <mutex>
#include <vector>

#include <rex/cvar.h>
#include <rex/logging.h>

REXCVAR_DEFINE_INT32(skate3_instance_free_defer_ms, 1000, "Skate 3",
                     "Hold the u16 arrays freed by the WorldPresentation instance "
                     "teardown for this many ms before returning them to the pool, so a "
                     "Job Manager worker still writing through an interior pointer into "
                     "them cannot corrupt the free list (0 = off; at most 512 KB held)")
    .range(0, 5000)
    .lifecycle(rex::cvar::Lifecycle::kRequiresRestart);

namespace {

// Pool layout (decoded in PR #131 from sub_82990768 / sub_82990A58):
//   pool  +224/+228  arena [lo, hi)
//   chunk +12        u16 block size; chunks are 32 KiB aligned, first block at +32
constexpr uint32_t kArenaLo = 224;
constexpr uint32_t kArenaHi = 228;
constexpr uint32_t kChunkSize = 32768;
constexpr uint32_t kChunkData = 32;
constexpr uint32_t kChunkBlockSize = 12;
constexpr uint64_t kDeferMaxBytes = 512 * 1024;

thread_local bool tl_in_instance_dtor = false;

struct DeferredBlock {
  uint32_t pool;
  uint32_t block;
  uint32_t size;
  uint64_t due_ms;
};
std::mutex g_mutex;
std::deque<DeferredBlock> g_deferred;
uint64_t g_deferred_bytes = 0;

uint64_t DeferMs() {
  static const uint64_t ms = uint64_t(std::max(0, REXCVAR_GET(skate3_instance_free_defer_ms)));
  return ms;
}

uint64_t NowMs() {
  return uint64_t(std::chrono::duration_cast<std::chrono::milliseconds>(
                      std::chrono::steady_clock::now().time_since_epoch())
                      .count());
}

// The guest's own chunk arithmetic, wraparound included.
uint32_t ChunkOf(uint32_t block) { return ((block + 32767u) & 0xFFFF8000u) - 32768u; }

// True when `block` is a block of this pool; also returns its size class.
bool BlockExtent(uint8_t* base, uint32_t pool, uint32_t block, uint32_t* size) {
  if (pool < 0x10000) return false;  // pool object not initialised yet
  const uint32_t lo = REX_LOAD_U32(pool + kArenaLo);
  const uint32_t hi = REX_LOAD_U32(pool + kArenaHi);
  if (lo < 0x10000 || hi <= lo || block < lo || block >= hi) return false;
  const uint32_t chunk = ChunkOf(block);
  if (block < chunk || block >= chunk + kChunkSize) return false;
  const uint32_t bsize = REX_LOAD_U16(chunk + kChunkBlockSize);
  if (bsize < 16 || bsize > 2048 || block < chunk + kChunkData ||
      (block - (chunk + kChunkData)) % bsize != 0 || block + bsize > chunk + kChunkSize) {
    return false;
  }
  *size = bsize;
  return true;
}

}  // namespace

// Pool free: r3 = pool, r4 = ptr; returns r3 = 1 when the pool owned the pointer.
extern "C" REX_FUNC(sub_82990A58) {
  uint32_t bsize = 0;
  if (!tl_in_instance_dtor || DeferMs() == 0 ||
      !BlockExtent(base, ctx.r3.u32, ctx.r4.u32, &bsize)) {
    __imp__sub_82990A58(ctx, base);
    return;
  }
  const uint64_t now = NowMs();
  std::vector<DeferredBlock> release;
  {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_deferred.push_back({ctx.r3.u32, ctx.r4.u32, bsize, now + DeferMs()});
    g_deferred_bytes += bsize;
    while (!g_deferred.empty() &&
           (g_deferred.front().due_ms <= now || g_deferred_bytes > kDeferMaxBytes)) {
      release.push_back(g_deferred.front());
      g_deferred_bytes -= g_deferred.front().size;
      g_deferred.pop_front();
    }
  }
  for (const DeferredBlock& d : release) {
    ctx.r3.u32 = d.pool;
    ctx.r4.u32 = d.block;
    __imp__sub_82990A58(ctx, base);
  }
  ctx.r3.u32 = 1;  // the caller's block is ours now
}

// Sk8::WorldPresentation instance teardown: marks the frees it performs.
extern "C" REX_FUNC(sub_82792040) {
  if (DeferMs() == 0) {
    __imp__sub_82792040(ctx, base);
    return;
  }
  static bool logged = false;
  if (!logged) {
    logged = true;
    REXLOG_INFO("world-presentation: instance teardown frees held {} ms (cap {} KB)", DeferMs(),
                kDeferMaxBytes / 1024);
  }
  const bool previous = tl_in_instance_dtor;
  tl_in_instance_dtor = true;
  __imp__sub_82792040(ctx, base);
  tl_in_instance_dtor = previous;
}
