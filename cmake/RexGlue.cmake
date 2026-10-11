set(REXSDK_VERSION "" CACHE STRING "Override rexglue SDK package version")

if(REXSDK_DIR AND EXISTS "${REXSDK_DIR}/CMakeLists.txt")
    # Verify the fork's recorded SDK edits before compiling.
    execute_process(
        COMMAND "${Python3_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/tools/apply_sdk_patches.py"
            --sdk "${REXSDK_DIR}" --check
        RESULT_VARIABLE _skate3_sdk_patch_result
        OUTPUT_VARIABLE _skate3_sdk_patch_output
        ERROR_VARIABLE _skate3_sdk_patch_error)
    if(NOT _skate3_sdk_patch_result EQUAL 0)
        message(FATAL_ERROR "${_skate3_sdk_patch_error}")
    endif()
    message(STATUS "${_skate3_sdk_patch_output}")
    add_subdirectory("${REXSDK_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/rexglue-sdk")
    message(STATUS "Using rexglue SDK source tree: ${REXSDK_DIR}")
else()
    if(REXSDK_VERSION)
        find_package(rexglue ${REXSDK_VERSION} EXACT QUIET CONFIG)
    else()
        find_package(rexglue 0.8.1.19 QUIET CONFIG)
    endif()

    if(NOT rexglue_FOUND)
        message(FATAL_ERROR
            "rexglue SDK not found. Set REXSDK_DIR to a rexglue-sdk source tree "
            "or install the SDK package and set CMAKE_PREFIX_PATH.")
    endif()

    message(STATUS "Found rexglue SDK ${REXGLUE_VERSION_STRING} at ${rexglue_DIR}")
endif()
