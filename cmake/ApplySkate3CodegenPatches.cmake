if(NOT DEFINED SKATE3_SOURCE_DIR)
  message(FATAL_ERROR "SKATE3_SOURCE_DIR is required")
endif()

file(GLOB _skate3_recomp_files
  LIST_DIRECTORIES false
  "${SKATE3_SOURCE_DIR}/generated/skate3_recomp.*.cpp")
if(NOT _skate3_recomp_files)
  message(FATAL_ERROR "No generated Skate 3 recompilation files found")
endif()

function(_skate3_add_include _contents_var _include)
  set(_contents "${${_contents_var}}")
  if(NOT _contents MATCHES "#include \"${_include}\"")
    string(REPLACE
      "#include \"skate3_init.h\"\n"
      "#include \"skate3_init.h\"\n#include \"${_include}\"\n"
      _contents
      "${_contents}")
  endif()
  set(${_contents_var} "${_contents}" PARENT_SCOPE)
endfunction()

set(_frustum_patched FALSE)
foreach(_file IN LISTS _skate3_recomp_files)
  file(READ "${_file}" _contents)
  if(_contents MATCHES "Skate3UltrawideGameFrustumPatchScope")
    set(_frustum_patched TRUE)
    break()
  endif()
  string(FIND "${_contents}" "ctx.r6.u64 = REX_LOAD_U32(ctx.r4.u32 + 5260);" _frustum_anchor)
  if(_frustum_anchor EQUAL -1)
    continue()
  endif()

  string(SUBSTRING "${_contents}" ${_frustum_anchor} 12000 _frustum_window)
  string(REGEX MATCH
    "\t// bl 0x[0-9a-fA-F]+\n\tctx\\.lr = 0x[0-9A-F]+;\n\tsub_[0-9A-F]+\\(ctx, base\\);"
    _frustum_call
    "${_frustum_window}")
  if(_frustum_call STREQUAL "")
    message(FATAL_ERROR "Failed to apply Skate 3 generated frustum patch; call near frustum anchor not found in ${_file}")
  endif()

  string(REGEX REPLACE
    "(\tctx\\.lr = 0x[0-9A-F]+;\n)"
    "\\1\tSkate3UltrawideGameFrustumPatchScope skate3_ultrawide_game_frustum_patch_scope(\n\t\tctx, base, ctx.r4.u32);\n"
    _frustum_patch
    "${_frustum_call}")
  string(REPLACE "${_frustum_call}" "${_frustum_patch}" _contents "${_contents}")
  _skate3_add_include(_contents "skate3_ultrawide_guest.h")
  file(WRITE "${_file}" "${_contents}")
  set(_frustum_patched TRUE)
  message(STATUS "Applied Skate 3 generated frustum patch in ${_file}")
  break()
endforeach()
if(NOT _frustum_patched)
  message(FATAL_ERROR "Failed to apply Skate 3 generated frustum patch; frustum anchor not found")
endif()

set(_fov_patched FALSE)
foreach(_file IN LISTS _skate3_recomp_files)
  file(READ "${_file}" _contents)
  if(_contents MATCHES "Skate3MaybeOverrideProjectionFovRadians")
    set(_fov_patched TRUE)
    break()
  endif()
  if(NOT _contents MATCHES "ctx\\.f27\\.f64 = ctx\\.f1\\.f64;")
    continue()
  endif()
  if(NOT _contents MATCHES "ctx\\.f4\\.f64 = double\\(float\\(ctx\\.f1\\.f64 \\* ctx\\.f0\\.f64\\)\\);")
    continue()
  endif()

  set(_projection_fov_site "ctx.f27.f64 = ctx.f1.f64;")
  set(_projection_fov_patch
"ctx.f1.f64 = double(Skate3MaybeOverrideProjectionFovRadians(float(ctx.f1.f64)));
	ctx.f27.f64 = ctx.f1.f64;")
  string(REPLACE "${_projection_fov_site}" "${_projection_fov_patch}" _contents "${_contents}")
  _skate3_add_include(_contents "skate3_fov.h")
  file(WRITE "${_file}" "${_contents}")
  set(_fov_patched TRUE)
  message(STATUS "Applied Skate 3 generated projection FOV patch in ${_file}")
  break()
endforeach()
if(NOT _fov_patched)
  message(FATAL_ERROR "Failed to apply Skate 3 generated projection FOV patch; projection FOV anchor not found")
endif()

set(_demo_path_movie_patched FALSE)
foreach(_file IN LISTS _skate3_recomp_files)
  file(READ "${_file}" _contents)
  if(_contents MATCHES "ShouldForceIntroMovieComplete")
    set(_demo_path_movie_patched TRUE)
    break()
  endif()

  set(_demo_path_movie_site
"	// bl 0x825d60c8
	ctx.lr = 0x825E05A0;
	sub_825D60C8(ctx, base);")
  if(NOT _contents MATCHES "ctx\\.lr = 0x825E05A0;")
    continue()
  endif()
  if(NOT _contents MATCHES "DEFINE_REX_FUNC\\(sub_825E0510\\)")
    continue()
  endif()
  string(FIND "${_contents}" "${_demo_path_movie_site}" _demo_path_movie_anchor)
  if(_demo_path_movie_anchor EQUAL -1)
    continue()
  endif()

  set(_demo_path_movie_patch
"	if (skate3::demo_path::ShouldForceIntroMovieComplete()) {
		ctx.r3.u64 = 0;
	} else {
		// bl 0x825d60c8
		ctx.lr = 0x825E05A0;
		sub_825D60C8(ctx, base);
	}")
  string(REPLACE "${_demo_path_movie_site}" "${_demo_path_movie_patch}" _contents "${_contents}")
  _skate3_add_include(_contents "skate3_demo_path.h")
  file(WRITE "${_file}" "${_contents}")
  set(_demo_path_movie_patched TRUE)
  message(STATUS "Applied Skate 3 demo path intro movie patch in ${_file}")
  break()
endforeach()
if(NOT _demo_path_movie_patched)
  message(FATAL_ERROR "Failed to apply Skate 3 demo path intro movie patch; FEMoviePlayer::Update anchor not found")
endif()

# Time the original synchronous event callbacks without replacing dispatch,
# its subscriber iteration, filters, locks or guest return registers.
set(_event_callback_patched FALSE)
foreach(_file IN LISTS _skate3_recomp_files)
  file(READ "${_file}" _contents)
  if(_contents MATCHES "GuestEventCallbackScope")
    set(_event_callback_patched TRUE)
    break()
  endif()
  set(_event_callback_site
"	ctx.lr = 0x82B93A10;
	REX_CALL_INDIRECT_FUNC(ctx.ctr.u32);")
  string(FIND "${_contents}" "${_event_callback_site}" _event_callback_anchor)
  if(_event_callback_anchor EQUAL -1)
    continue()
  endif()
  set(_event_callback_patch
"	ctx.lr = 0x82B93A10;
	{
		skate3::perf_recorder::GuestEventCallbackScope skate3_event_callback_timing(
			ctx.ctr.u32, ctx.r4.u32);
		REX_CALL_INDIRECT_FUNC(ctx.ctr.u32);
	}")
  string(REPLACE "${_event_callback_site}" "${_event_callback_patch}" _contents "${_contents}")
  _skate3_add_include(_contents "skate3_perf_recorder.h")
  file(WRITE "${_file}" "${_contents}")
  set(_event_callback_patched TRUE)
  message(STATUS "Applied Skate 3 event callback timing patch in ${_file}")
  break()
endforeach()
if(NOT _event_callback_patched)
  message(FATAL_ERROR "Failed to apply event callback timing patch; dispatch anchor not found")
endif()

# sub_823D0F58 is the event system's generic (object, function) forwarder.
# Observe the target already loaded by the guest; keep its tail call intact.
set(_event_forward_patched FALSE)
foreach(_file IN LISTS _skate3_recomp_files)
  file(READ "${_file}" _contents)
  if(_contents MATCHES "NoteGuestEventCallbackTarget")
    set(_event_forward_patched TRUE)
    break()
  endif()
  set(_event_forward_site
"DEFINE_REX_FUNC(sub_823D0F58) {
	REX_FUNC_PROLOGUE();
	// lwz r10,0(r4)
	ctx.r10.u64 = REX_LOAD_U32(ctx.r4.u32 + 0);")
  string(FIND "${_contents}" "${_event_forward_site}" _event_forward_anchor)
  if(_event_forward_anchor EQUAL -1)
    continue()
  endif()
  set(_event_forward_patch
"${_event_forward_site}
	skate3::perf_recorder::NoteGuestEventCallbackTarget(ctx.r10.u32);")
  string(REPLACE "${_event_forward_site}" "${_event_forward_patch}" _contents "${_contents}")
  _skate3_add_include(_contents "skate3_perf_recorder.h")
  file(WRITE "${_file}" "${_contents}")
  set(_event_forward_patched TRUE)
  message(STATUS "Applied Skate 3 event forwarding target probe in ${_file}")
  break()
endforeach()
if(NOT _event_forward_patched)
  message(FATAL_ERROR "Failed to apply event forwarding probe; thunk anchor not found")
endif()

# Inclusive, read-only timings around the original resource work seen in
# the slow-event stacks. These scopes arm only for recorded NSV events.
foreach(_phase IN ITEMS "8279ECF8;kResourcePresentation" "82794090;kPrimitiveSetup" "828915F0;kGuidLookup"
                        "827A5D60;kInstanceContexts" "827A6658;kInstanceParameters"
                        "82796860;kInstanceRegistry" "828AAE90;kInstanceAllocation"
                        "827E5580;kContextMaterials" "82CA6150;kContextFactory"
                        "827E51E8;kContextBindings" "827E1608;kContextResources"
                        "827930C0;kPrimitiveClear" "82C251E0;kPrimitiveCollect"
                        "82793028;kPrimitiveInsert" "82793C70;kPrimitiveCreate"
                        "82C25360;kPrimitiveRelease" "82C94ED0;kPrimitiveRequest")
  list(GET _phase 0 _address)
  list(GET _phase 1 _kind)
  set(_event_phase_patched FALSE)
  foreach(_file IN LISTS _skate3_recomp_files)
    file(READ "${_file}" _contents)
    if(_contents MATCHES "skate3_event_work_${_address}")
      set(_event_phase_patched TRUE)
      break()
    endif()
    set(_event_phase_site "DEFINE_REX_FUNC(sub_${_address}) {
	REX_FUNC_PROLOGUE();")
    string(FIND "${_contents}" "${_event_phase_site}" _event_phase_anchor)
    if(_event_phase_anchor EQUAL -1)
      continue()
    endif()
    set(_event_phase_patch
"${_event_phase_site}
	skate3::perf_recorder::GuestEventWorkScope skate3_event_work_${_address}(
		skate3::perf_recorder::GuestEventWork::${_kind});")
    string(REPLACE "${_event_phase_site}" "${_event_phase_patch}" _contents "${_contents}")
    _skate3_add_include(_contents "skate3_perf_recorder.h")
    file(WRITE "${_file}" "${_contents}")
    set(_event_phase_patched TRUE)
    message(STATUS "Applied Skate 3 event work probe ${_address} in ${_file}")
    break()
  endforeach()
  if(NOT _event_phase_patched)
    message(FATAL_ERROR "Failed to apply event work probe ${_address}; function anchor not found")
  endif()
endforeach()

# Preserve the factory input effect key for the existing detailed timer.
# Apply to already-patched originals AND compiler variants; no extra clocks.
# The scope only records during original NSV activation with event detail on.
foreach(_file IN LISTS _skate3_recomp_files)
  file(READ "${_file}" _contents)
  if(_contents MATCHES "GuestEventWork::kContextFactory\\);")
    string(REPLACE "GuestEventWork::kContextFactory);"
                   "GuestEventWork::kContextFactory, ctx.r5.u32);"
                   _contents "${_contents}")
    file(WRITE "${_file}" "${_contents}")
    message(STATUS "Applied activation material factory identity in ${_file}")
  endif()
endforeach()

# Time the two original virtual dispatches inside primitive setup. Read only
# their already loaded function target; preserve each guest call and LR.
foreach(_return_address IN ITEMS "827940D0" "827941B4")
  set(_dispatch_patched FALSE)
  foreach(_file IN LISTS _skate3_recomp_files)
    file(READ "${_file}" _contents)
    if(_contents MATCHES "skate3_primitive_dispatch_${_return_address}")
      set(_dispatch_patched TRUE)
      break()
    endif()
    set(_site "\tctx.lr = 0x${_return_address};\n\tREX_CALL_INDIRECT_FUNC(ctx.ctr.u32);")
    string(FIND "${_contents}" "${_site}" _anchor)
    if(_anchor EQUAL -1)
      continue()
    endif()
    set(_patch "\tctx.lr = 0x${_return_address};\n\t{\n\t\tskate3::perf_recorder::GuestEventWorkScope skate3_primitive_dispatch_${_return_address}(\n\t\t\tskate3::perf_recorder::GuestEventWork::kPrimitiveDispatch, ctx.ctr.u32);\n\t\tREX_CALL_INDIRECT_FUNC(ctx.ctr.u32);\n\t}")
    string(REPLACE "${_site}" "${_patch}" _contents "${_contents}")
    _skate3_add_include(_contents "skate3_perf_recorder.h")
    file(WRITE "${_file}" "${_contents}")
    set(_dispatch_patched TRUE)
    break()
  endforeach()
  if(NOT _dispatch_patched)
    message(FATAL_ERROR "Primitive dispatch probe ${_return_address} anchor missing")
  endif()
endforeach()

# Index only the texture find within one original NSV activation callback.
# No event deferral, changed guest hash/iteration, or cross-frame pointers.
set(_index_batch_found FALSE)
set(_index_find_found FALSE)
foreach(_file IN LISTS _skate3_recomp_files)
  file(READ "${_file}" _contents)
  set(_changed FALSE)
  if(_contents MATCHES "DEFINE_REX_FUNC\\(sub_82727B70\\)" AND
     NOT _contents MATCHES "skate3_texture_lookup_batch")
    set(_site "DEFINE_REX_FUNC(sub_82727B70) {\n\tREX_FUNC_PROLOGUE();")
    string(FIND "${_contents}" "${_site}" _anchor)
    if(_anchor EQUAL -1)
      message(FATAL_ERROR "NSV activation index scope anchor changed")
    endif()
    string(REPLACE "${_site}" "${_site}\n\tskate3::world_loading::TextureLookupBatch skate3_texture_lookup_batch;" _contents "${_contents}")
    set(_changed TRUE)
  endif()
  if(_contents MATCHES "ctx\\.lr = 0x8280AC8C;" AND
     NOT _contents MATCHES "TryIndexedTextureLookup")
    set(_site "\tctx.lr = 0x8280AC8C;\n\tsub_828915F0(ctx, base);")
    string(FIND "${_contents}" "${_site}" _anchor)
    if(_anchor EQUAL -1)
      message(FATAL_ERROR "Texture binding index lookup anchor changed")
    endif()
    string(REPLACE "${_site}" "\tctx.lr = 0x8280AC8C;\n\tif (!skate3::world_loading::TryIndexedTextureLookup(ctx, base)) {\n\t\tsub_828915F0(ctx, base);\n\t}" _contents "${_contents}")
    set(_changed TRUE)
  endif()
  if(_changed)
    _skate3_add_include(_contents "skate3_texture_lookup.h")
    file(WRITE "${_file}" "${_contents}")
    message(STATUS "Applied scoped NSV texture lookup index in ${_file}")
  endif()
  if(_contents MATCHES "skate3_texture_lookup_batch")
    set(_index_batch_found TRUE)
  endif()
  if(_contents MATCHES "TryIndexedTextureLookup")
    set(_index_find_found TRUE)
  endif()
endforeach()
if(NOT _index_batch_found OR NOT _index_find_found)
  message(FATAL_ERROR "Scoped NSV texture lookup anchors missing")
endif()

# Run a native equivalent of only the descriptor SEARCH in 827E1608.
# The original stack/register prologue, creation, cache insertion and epilogue
# remain intact. The helper falls back without modifying the PPC context.
set(_context_search_found FALSE)
foreach(_file IN LISTS _skate3_recomp_files)
  file(READ "${_file}" _contents)
  if(NOT _contents MATCHES "DEFINE_REX_FUNC\\(sub_827E1608\\)")
    continue()
  endif()
  if(NOT _contents MATCHES "TryFastContextResourceSearch")
    set(_site
"\t// mr r29,r5
\tctx.r29.u64 = ctx.r5.u64;
\t// li r7,0
\tctx.r7.s64 = 0;
\t// cmplwi cr6,r11,0")
    string(FIND "${_contents}" "${_site}" _anchor)
    if(_anchor EQUAL -1)
      message(FATAL_ERROR "Context resource search prefix anchor changed")
    endif()
    string(LENGTH "${_site}" _site_length)
    math(EXPR _end "${_anchor} + ${_site_length}")
    string(SUBSTRING "${_contents}" ${_end} -1 _remainder)
    string(FIND "${_remainder}" "${_site}" _duplicate)
    if(NOT _duplicate EQUAL -1)
      message(FATAL_ERROR "Context resource search prefix anchor is ambiguous")
    endif()
    set(_patch
"\t// mr r29,r5
\tctx.r29.u64 = ctx.r5.u64;
\t// li r7,0
\tctx.r7.s64 = 0;
\tswitch (skate3::world_loading::TryFastContextResourceSearch(ctx, base)) {
\t\tcase skate3::world_loading::ContextResourceSearch::kReady: goto loc_827E1748;
\t\tcase skate3::world_loading::ContextResourceSearch::kCreate: goto loc_827E16AC;
\t\tcase skate3::world_loading::ContextResourceSearch::kFallback: break;
\t}
\t// cmplwi cr6,r11,0")
    string(REPLACE "${_site}" "${_patch}" _contents "${_contents}")
    _skate3_add_include(_contents "skate3_context_resource_search.h")
    file(WRITE "${_file}" "${_contents}")
    message(STATUS "Applied NSV native context descriptor search in ${_file}")
  endif()
  set(_context_search_found TRUE)
  break()
endforeach()
if(NOT _context_search_found)
  message(FATAL_ERROR "Context resource search function anchor missing")
endif()

# Localize the arithmetic-only hash after all other generated-code patches.
# Python is already required by the project and title-update code generation.
find_program(_nsv_hash_python NAMES python3 python REQUIRED)
execute_process(
  COMMAND "${_nsv_hash_python}" "${SKATE3_SOURCE_DIR}/cmake/LocalizeNsvActivationHash.py"
          "${SKATE3_SOURCE_DIR}/generated"
  RESULT_VARIABLE _nsv_hash_result)
if(NOT _nsv_hash_result EQUAL 0)
  message(FATAL_ERROR "NSV activation hash localization failed")
endif()

# NSV uses the active camera direction for world-space projected mesh areas.
# The original SIMD culler keeps its frustum, volumes, thresholds and calls.
execute_process(
  COMMAND "${_nsv_hash_python}" "${SKATE3_SOURCE_DIR}/cmake/PatchNsvContributionDirection.py"
          "${SKATE3_SOURCE_DIR}/generated"
  RESULT_VARIABLE _nsv_direction_result)
if(NOT _nsv_direction_result EQUAL 0)
  message(FATAL_ERROR "NSV contribution direction correction failed")
endif()

# Keep original NSV vectorized culling decisions with host-local registers.
execute_process(
  COMMAND "${_nsv_hash_python}" "${SKATE3_SOURCE_DIR}/cmake/LocalizeNsvCulling.py"
          "${SKATE3_SOURCE_DIR}/generated"
  RESULT_VARIABLE _nsv_cull_register_result)
if(NOT _nsv_cull_register_result EQUAL 0)
  message(FATAL_ERROR "NSV vectorized culling register localization failed")
endif()
