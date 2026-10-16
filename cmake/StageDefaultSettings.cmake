# Seed a fresh build with the tested release settings; preserve local settings.
if(NOT EXISTS "${SKATE3_BUILD_ROOT}/settings.toml")
    if(SKATE3_HOST_PLATFORM STREQUAL "Linux" OR SKATE3_HOST_PLATFORM STREQUAL "Darwin")
        execute_process(
            COMMAND "${SKATE3_PYTHON}" "${CMAKE_CURRENT_LIST_DIR}/../tools/platform_settings.py"
                --platform "${SKATE3_HOST_PLATFORM}" --source "${SKATE3_DEFAULT_SETTINGS}"
                --output "${SKATE3_BUILD_ROOT}/settings.toml" --only-if-missing
            COMMAND_ERROR_IS_FATAL ANY)
    else()
        file(COPY_FILE "${SKATE3_DEFAULT_SETTINGS}" "${SKATE3_BUILD_ROOT}/settings.toml")
    endif()
endif()
