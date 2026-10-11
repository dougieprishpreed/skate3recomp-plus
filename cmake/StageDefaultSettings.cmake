# Seed a fresh build with the tested release settings; preserve local settings.
if(NOT EXISTS "${SKATE3_BUILD_ROOT}/settings.toml")
    file(COPY_FILE "${SKATE3_DEFAULT_SETTINGS}" "${SKATE3_BUILD_ROOT}/settings.toml")
endif()
