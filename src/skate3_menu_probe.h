#pragma once

#include <cstdint>

// Locations-menu probe (see skate3_menu_probe.cpp): hooks owned by other
// files forward their events here.
namespace skate3::menu_probe {

void OnGameplayEvent(uint8_t* base, uint32_t lr, uint32_t sp, uint32_t object, uint32_t payload,
                     uint32_t type);
void OnLocationMenuAction(uint8_t* base, uint32_t lr, uint32_t sp, uint32_t object);

}  // namespace skate3::menu_probe
