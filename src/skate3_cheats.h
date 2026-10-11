#pragma once

namespace skate3::cheats {

// The black-screen check's residency query (sub_82478D00 returning to
// 0x827FF73C, hooked in skate3_world_loading.cpp) reports its final result
// here; No Black Screen undoes the check's black screen when it was false.
void OnBlackScreenResidency(bool resident);

}  // namespace skate3::cheats
