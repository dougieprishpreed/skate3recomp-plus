#pragma once
#include <filesystem>
namespace skate3::pedestrian_speech {
// Called after the actor dependency inventory has been verified.
void Install(const std::filesystem::path& folder);
}
