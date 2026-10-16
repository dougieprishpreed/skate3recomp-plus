#include "generated/skate3_init.h"
#include "skate3_context_resource_search.h"
#include "skate3_world_loading.h"
#include "skate3_perf_recorder.h"

#include <rex/cvar.h>

REXCVAR_DEFINE_BOOL(skate3_skate2_context_resource_search, true, "Skate 3",
                    "Use native descriptor search in New San Van material context creation");

namespace skate3::world_loading {

ContextResourceSearch TryFastContextResourceSearch(PPCContext& ctx, uint8_t* base) {
  if (!IsSkate2Gameplay() || !REXCVAR_GET(skate3_skate2_context_resource_search)) {
    return ContextResourceSearch::kFallback;
  }
  // Work in locals, avoiding the aliasing between PPCContext stores and guest
  // loads in the translated inner loop. Read the SAME live entries in the SAME
  // order, including the count re-read at the end of every rejected entry.
  // Commit only a completed bounded search: fallback leaves ALL ctx state intact.
  auto cr0 = ctx.cr0;
  auto cr6 = ctx.cr6;
  auto xer = ctx.xer;
  uint64_t r3 = ctx.r3.u64, r5 = ctx.r5.u64, r6 = ctx.r6.u64;
  uint64_t r7 = ctx.r7.u64, r8 = ctx.r8.u64, r9 = ctx.r9.u64;
  uint64_t r10 = ctx.r10.u64, r11 = ctx.r11.u64;
  const uint64_t table = ctx.r31.u64, query = ctx.r30.u64;
  const uint32_t tag = ctx.r29.u32;
  constexpr uint32_t kMaxEntries = 4096, kMaxDescriptorRows = 64;
  if (uint32_t(r11) > kMaxEntries) return ContextResourceSearch::kFallback;
  uint32_t scanned = 0;
  const auto finish = [&](ContextResourceSearch result) {
    ctx.r3.u64 = r3; ctx.r5.u64 = r5; ctx.r6.u64 = r6;
    ctx.r7.u64 = r7; ctx.r8.u64 = r8; ctx.r9.u64 = r9;
    ctx.r10.u64 = r10; ctx.r11.u64 = r11;
    ctx.cr0 = cr0; ctx.cr6 = cr6; ctx.xer = xer;
    skate3::perf_recorder::NoteFastContextResourceSearch(scanned);
    return result;
  };
  cr6.compare<uint32_t>(uint32_t(r11), 0, xer);
  if (!cr6.gt) return finish(ContextResourceSearch::kCreate);
  r8 = r3 + 108;
  for (;;) {
    if (++scanned > kMaxEntries) return ContextResourceSearch::kFallback;
    r11 = REX_LOAD_U32(uint32_t(r8) + 4);
    cr6.compare<uint32_t>(uint32_t(r11), tag, xer);
    if (cr6.eq) {
      r11 = REX_LOAD_U32(uint32_t(r8));
      r10 = REX_LOAD_U32(uint32_t(query) + 4);
      r9 = REX_LOAD_U32(uint32_t(r11) + 4);
      cr6.compare<uint32_t>(uint32_t(r9), uint32_t(r10), xer);
      if (cr6.eq) {
        r10 = REX_LOAD_U16(uint32_t(r11) + 8);
        r9 = REX_LOAD_U16(uint32_t(query) + 8);
        cr6.compare<uint32_t>(uint32_t(r10), uint32_t(r9), xer);
        if (cr6.eq) {
          // The original compares 4 words PER row, then reads the resource
          // for the FIRST equal descriptor/tag. Zero rows take its original
          // unusual do/while path; don't reinterpret it as an empty match.
          if (r10 == 0 || r10 > kMaxDescriptorRows) {
            return ContextResourceSearch::kFallback;
          }
          r11 += 16;
          r10 = (uint32_t(r10) << 2) & 0xFFFFFFFCu;
          r9 = query - r11 + 16;
          bool equal = true;
          do {
            r6 = REX_LOAD_U32(uint32_t(r11));
            r5 = REX_LOAD_U32(uint32_t(r9 + r11));
            cr6.compare<uint32_t>(uint32_t(r6), uint32_t(r5), xer);
            if (!cr6.eq) { equal = false; break; }
            xer.ca = uint32_t(r10) > 0;
            --r10;
            cr0.compare<int32_t>(int32_t(r10), 0, xer);
            r11 += 4;
          } while (!cr0.eq);
          if (equal) {
            r11 = (uint32_t(r7) << 1) & 0xFFFFFFFEu;
            r11 += r7;
            r11 = (uint32_t(r11) << 2) & 0xFFFFFFFCu;
            r10 = r11 + table;
            r3 = REX_LOAD_U32(uint32_t(r10) + 116);
            cr6.compare<uint32_t>(uint32_t(r3), 0, xer);
            return finish(cr6.eq ? ContextResourceSearch::kCreate
                                 : ContextResourceSearch::kReady);
          }
        }
      }
    }
    r11 = REX_LOAD_U32(uint32_t(table) + 104);
    if (r11 > kMaxEntries) return ContextResourceSearch::kFallback;
    ++r7;
    r8 += 12;
    cr6.compare<uint32_t>(uint32_t(r7), uint32_t(r11), xer);
    if (!cr6.lt) return finish(ContextResourceSearch::kCreate);
  }
}

}  // namespace skate3::world_loading
