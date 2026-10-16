#pragma once
#include <cstdint>

namespace skate3::native_cull_probe {
// Manual NSV diagnostic. Arms one complete guest frame, no ongoing history.
void Request(uint32_t marker);
void OnGuestFrame(uint64_t frame);
}
