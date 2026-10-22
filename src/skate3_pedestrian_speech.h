#pragma once
#include <filesystem>
#include <cstdint>
namespace skate3::pedestrian_speech {
// Called after the actor dependency inventory has been verified.
void Install(const std::filesystem::path& folder);
// Called at the native voice-update gain calculation, inside its existing
// audio lock. Reads the current channel bank; no speaker writes/cached handles.
float ScaleVoiceGain(uint8_t* base, uint32_t voice_instance, uint32_t channel, float gain);
}
