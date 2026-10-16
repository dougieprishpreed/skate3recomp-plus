#include "generated/skate3_init.h"
#include "skate3_texture_lookup.h"
#include "skate3_world_loading.h"
#include "skate3_perf_recorder.h"

#include <atomic>
#include <new>
#include <mutex>
#include <unordered_map>
#include <vector>

#include <rex/cvar.h>
#include <rex/logging.h>

REXCVAR_DEFINE_BOOL(skate3_skate2_texture_lookup_index, true, "Skate 3",
                    "Index texture binding lookups during New San Van scenery activation");

REXCVAR_DEFINE_BOOL(skate3_skate2_native_activation_registers, true, "Skate 3",
                    "Optimize host register aliasing during New San Van material activation");

namespace skate3::world_loading {
thread_local bool g_native_scenery_registers = false;
namespace {
thread_local uint32_t native_depth = 0;
constexpr uint32_t kMaxNodes = 65536;
struct GuidHash {
  size_t operator()(uint64_t v) const noexcept {
    // Mix BOTH halves: many Skate 2 IDs have the same low 32 bits.
    v ^= v >> 30; v *= 0xBF58476D1CE4E5B9ull;
    v ^= v >> 27; v *= 0x94D049BB133111EBull;
    return size_t(v ^ (v >> 31));
  }
};
struct Index {
  uint32_t depth = 0, table = 0, heads = 0, buckets = 0, count = 0;
  uint64_t revision = 0;
  bool disabled = false;
  std::unordered_map<uint64_t, uint32_t, GuidHash> nodes;
  std::vector<uint8_t> built;
  void Clear() {
    table = heads = buckets = count = 0;
    nodes.clear(); built.clear();
  }
};
thread_local Index index;
std::atomic<uint32_t> active_batches{0}, mutations{0};
std::atomic<uint32_t> active_table{0};
std::atomic<uint64_t> revision{0};
// Only registration of a batch/table is locked. Never hold this mutex while
// reading a guest table or calling guest code. Simultaneous batches using
// another table fall back; unrelated map mutations do not rebuild this index.
std::mutex batch_registry;

// All modifications to the texture binding chain use the insert/remove,
// rehash, initialization and clear/destruction paths hooked below. A global
// revision also invalidates an index when another guest thread mutates a map.
// No extra guest lock, table rehash, resource retention or persistent pointer.
class Mutation {
 public:
  Mutation(uint8_t* base, uint32_t table, uint64_t key = 0, bool insert = false)
      : base_(base), table_(table), key_(key), insert_(insert) {
    if (active_table.load(std::memory_order_acquire) != table_) return;
    active_ = true;
    mutations.fetch_add(1, std::memory_order_acq_rel);
    before_ = revision.fetch_add(1, std::memory_order_acq_rel);
    preserve_ = insert_ && index.depth && index.table == table_ &&
                index.revision == before_;
    if (!preserve_) index.Clear();
  }
  void Inserted(uint32_t node) { node_ = node; }
  ~Mutation() {
    if (!active_) return;
    const auto current = revision.fetch_add(1, std::memory_order_acq_rel);
    const auto remaining = mutations.fetch_sub(1, std::memory_order_acq_rel) - 1;
    uint8_t* base = base_;
    // Keep a built bucket up to date after the ORIGINAL insertion. Nested
    // rehash/other mutations clear it; an unexpected header/count falls back.
    if (preserve_ && current == before_ + 1 && remaining == 0 &&
        index.table == table_ && REX_LOAD_U32(table_ + 4) == index.heads &&
        REX_LOAD_U32(table_ + 8) == index.buckets && node_ != 0 &&
        REX_LOAD_U64(node_) == key_) {
      const uint32_t count = REX_LOAD_U32(table_ + 12);
      if (count == index.count || (index.count < kMaxNodes && count == index.count + 1)) {
        try {
          if (index.built[uint32_t(key_) % index.buckets]) index.nodes[key_] = node_;
          index.count = count; index.revision = current + 1;
          return;
        } catch (const std::bad_alloc&) { index.disabled = true; }
      }
    }
    index.Clear();
  }
 private:
  uint8_t* base_;
  uint32_t table_, node_ = 0;
  uint64_t key_, before_ = 0;
  bool insert_, active_ = false, preserve_ = false;
};
}  // namespace

TextureLookupBatch::TextureLookupBatch() {
  if (!IsSkate2Gameplay()) return;
  native_entered_ = true;
  if (native_depth++ == 0) {
    g_native_scenery_registers = REXCVAR_GET(skate3_skate2_native_activation_registers);
    static std::atomic<bool> logged{false};
    if (g_native_scenery_registers && !logged.load(std::memory_order_relaxed) &&
        !logged.exchange(true, std::memory_order_relaxed))
      REXLOG_INFO("native-scene: NSV material activation register optimization enabled");
  }
  if (!REXCVAR_GET(skate3_skate2_texture_lookup_index)) return;
  entered_ = true;
  if (index.depth++ == 0) {
    index.Clear(); index.disabled = false;
    std::lock_guard lock(batch_registry);
    active_batches.fetch_add(1, std::memory_order_acq_rel);
  }
}
TextureLookupBatch::~TextureLookupBatch() {
  if (native_entered_ && --native_depth == 0) g_native_scenery_registers = false;
  if (!entered_ || --index.depth != 0) return;
  index.Clear(); // Nothing survives the callback, a load, or a map transition.
  std::lock_guard lock(batch_registry);
  if (active_batches.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    active_table.store(0, std::memory_order_release);
  }
}

TextureLookupResetScope::TextureLookupResetScope(uint32_t table) {
  if (active_table.load(std::memory_order_acquire) != table) return;
  entered_ = true;
  mutations.fetch_add(1, std::memory_order_acq_rel);
  revision.fetch_add(1, std::memory_order_acq_rel);
  index.Clear();
}
TextureLookupResetScope::~TextureLookupResetScope() {
  if (!entered_) return;
  revision.fetch_add(1, std::memory_order_acq_rel);
  mutations.fetch_sub(1, std::memory_order_acq_rel);
}

bool TryIndexedTextureLookup(PPCContext& ctx, uint8_t* base) {
  if (!index.depth || index.disabled || uint32_t(ctx.lr) != 0x8280AC8C ||
      !IsSkate2Gameplay() || !REXCVAR_GET(skate3_skate2_texture_lookup_index)) return false;
  // This one caller passes separate stack-local key/iterator storage. Do
  // not accelerate another caller with potentially aliased table storage.
  const uint32_t sp = ctx.r1.u32;
  if (ctx.r5.u32 != sp + 80 || ctx.r3.u32 != sp + 88) return false;
  const uint32_t table = ctx.r4.u32;
  if (active_table.load(std::memory_order_acquire) != table) {
    std::lock_guard lock(batch_registry);
    if (active_table.load(std::memory_order_acquire) == 0) {
      active_table.store(table, std::memory_order_release);
    } else if (active_table.load(std::memory_order_acquire) != table) {
      return false;
    }
  }
  if (mutations.load(std::memory_order_acquire)) return false;
  const uint64_t version = revision.load(std::memory_order_acquire);
  const uint64_t key = REX_LOAD_U64(ctx.r5.u32);
  const uint32_t buckets = REX_LOAD_U32(table + 8), heads = REX_LOAD_U32(table + 4);
  const uint32_t count = REX_LOAD_U32(table + 12);
  if (!buckets || buckets > kMaxNodes || count > kMaxNodes ||
      mutations.load(std::memory_order_acquire)) return false;
  // Count/head checks are an extra guard, NOT a substitute for invalidating
  // unlink/free/reuse, which can otherwise leave the same count and head.
  if (index.table != table || index.heads != heads || index.buckets != buckets ||
      index.count != count || index.revision != version) {
    index.Clear(); index.table = table; index.heads = heads;
    index.buckets = buckets; index.count = count; index.revision = version;
  }
  skate3::perf_recorder::GuestEventWorkScope timing(
      skate3::perf_recorder::GuestEventWork::kGuidLookup);
  uint32_t scanned = 0;
  const uint32_t bucket_id = uint32_t(key) % buckets;
  try {
    if (index.built.empty()) index.built.resize(buckets);
    if (!index.built[bucket_id]) {
      uint32_t node = REX_LOAD_U32(heads + bucket_id * 4);
      while (node != 0) {
        if (++scanned > kMaxNodes || index.nodes.size() >= kMaxNodes) {
          index.Clear(); index.disabled = true; return false;
        }
        // First matching node wins, just as in the live guest chain.
        const uint64_t node_key = REX_LOAD_U64(node);
        if (uint32_t(node_key) % buckets == bucket_id) index.nodes.try_emplace(node_key, node);
        node = REX_LOAD_U32(node + 24);
      }
      index.built[bucket_id] = 1;
    }
  } catch (const std::bad_alloc&) {
    index.Clear(); index.disabled = true; return false;
  }
  if (revision.load(std::memory_order_acquire) != version ||
      mutations.load(std::memory_order_acquire)) { index.Clear(); return false; }
  const auto it = index.nodes.find(key);
  const bool found = it != index.nodes.end();
  uint32_t node = found ? it->second : 0;
  // Preserve the translated routine's final scratch writes and volatile
  // registers, including the signed GENERATED mullw and miss sentinel order.
  const uint32_t quotient = uint32_t(key) / buckets;
  const uint64_t product = uint64_t(int64_t(int32_t(quotient)) * int64_t(int32_t(buckets)));
  const uint64_t remainder = key - product;
  const uint64_t bucket = uint64_t((uint32_t(remainder) << 2) & 0xFFFFFFFCu) + heads;
  const uint32_t final_bucket = found ? uint32_t(bucket)
      : uint32_t(uint64_t((buckets << 2) & 0xFFFFFFFCu) + heads);
  REX_STORE_U32(sp - 12, final_bucket);
  if (!found) node = REX_LOAD_U32(final_bucket);
  REX_STORE_U32(sp - 16, node);
  const uint32_t final_node = REX_LOAD_U32(sp - 16);
  const uint32_t final_slot = REX_LOAD_U32(sp - 12);
  REX_STORE_U32(ctx.r3.u32, final_node); REX_STORE_U32(ctx.r3.u32 + 4, final_slot);
  ctx.r5.u64 = remainder; ctx.r6.u64 = bucket; ctx.r7.u64 = heads; ctx.r8.u64 = key;
  ctx.r9.u64 = buckets; ctx.r10.u64 = final_slot; ctx.r11.u64 = final_node;
  ctx.cr6.compare<uint32_t>(found ? 1u : 0u, 0, ctx.xer);
  skate3::perf_recorder::NoteIndexedTextureLookup(scanned);
  return true;
}
}  // namespace skate3::world_loading

// Preserve the original mutation functions/ABI. These hooks only maintain
// host index validity while an NSV activation batch is alive.
extern "C" REX_FUNC(sub_82812FA0) {
  if (skate3::world_loading::active_table.load(std::memory_order_acquire) != ctx.r4.u32) {
    __imp__sub_82812FA0(ctx, base); return;
  }
  skate3::world_loading::Mutation mutation(base, ctx.r4.u32, REX_LOAD_U64(ctx.r5.u32), true);
  __imp__sub_82812FA0(ctx, base);
  mutation.Inserted(REX_LOAD_U32(ctx.r3.u32));
}
extern "C" REX_FUNC(sub_82813118) {
  skate3::world_loading::Mutation mutation(base, ctx.r3.u32);
  __imp__sub_82813118(ctx, base);
}
extern "C" REX_FUNC(sub_82811860) {
  skate3::world_loading::Mutation mutation(base, ctx.r3.u32);
  __imp__sub_82811860(ctx, base);
}
extern "C" REX_FUNC(sub_82891688) {
  skate3::world_loading::Mutation mutation(base, ctx.r3.u32);
  __imp__sub_82891688(ctx, base);
}
extern "C" REX_FUNC(sub_82893BB8) {
  skate3::world_loading::Mutation mutation(base, ctx.r3.u32);
  __imp__sub_82893BB8(ctx, base);
}
