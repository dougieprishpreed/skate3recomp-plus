// Original baked light inside the designated interior zones. Only the
// enhanced static receive term changes: roofs remain casters and HDR shaft
// occlusion still sees them. The baked lattice keeps the lookup to a few loads.
float4 IndoorLoad(uint address) {
  return indoor_regions.Load(int3(address & 1023u, address >> 10u, 0));
}

// How far wp lies inside a designated interior zone: 0 outdoors, 1 fully
// inside, feathered at the edges. x = by day (and for the static sun
// receive); at night y = enclosed interiors, z = covered open places (the
// Downtown arcades, Oliver Elementary, The Parkade; left out while
// skate3_skate2_indoor_open_areas is off, indoor_info.z < 0). headroom =
// how far the zone's roof is above wp (0 outdoors): live shadows of things
// above that roof never reach wp (CsmCeilingLimit).
// The zones are baked (log229 bake_zones.py) to a lattice per grid cell: the
// cell header (x = block address, y = storeys, z = per-storey flags, bit s =
// a covered open place) and, per storey, a corner-inclusive (n+1)^2 lattice
// of float4 texels (x = roof top, y = bottom, z = signed distance in metres
// to the day set's outline, w = to the night set's; -100 = not in that
// set), n = cell size x indoor_info.w texels per metre. One header load and
// four bilinear loads per storey; the amount = min(vertical feather,
// saturate(distance / feather)), feather 1 m by day, 4 m at night for a
// covered open place (its night fade), 0.25 m for an enclosed interior.
float3 LocalIndoorAmounts(float3 wp, out float headroom) {
  headroom = 0.0;
  if (indoor_grid.w < 1.0) return float3(0.0, 0.0, 0.0);
  int2 cell = int2(floor((wp.xz - indoor_grid.xy) * indoor_grid.z));
  if (any(cell < 0) || cell.x >= int(indoor_grid.w) || cell.y >= int(indoor_info.x)) return float3(0.0, 0.0, 0.0);
  float4 head = IndoorLoad(uint(indoor_info.y) + uint(cell.y) * uint(indoor_grid.w) + uint(cell.x));
  uint storeys = uint(head.y);
  if (storeys == 0u) return float3(0.0, 0.0, 0.0);
  float size = 1.0 / indoor_grid.z;
  float tpm = indoor_info.w;
  uint n = uint(round(size * tpm));
  float2 f = clamp((wp.xz - (indoor_grid.xy + float2(cell) * size)) * tpm, 0.0, float(n) - 0.001);
  uint2 i = uint2(floor(f));
  float2 t = f - float2(i);
  uint block = uint(head.x);
  uint flags = uint(head.z);
  uint per = (n + 1u) * (n + 1u);
  bool open_areas = indoor_info.z > 0.0;
  float vertical = abs(indoor_info.z);
  float3 inside = float3(0.0, 0.0, 0.0);
  [loop] for (uint s = 0u; s < storeys; ++s) {
    bool semi = ((flags >> s) & 1u) != 0u;
    if (semi && !open_areas) continue;
    uint k00 = block + s * per + i.y * (n + 1u) + i.x;
    float4 v = lerp(lerp(IndoorLoad(k00), IndoorLoad(k00 + 1u), t.x),
                    lerp(IndoorLoad(k00 + n + 1u), IndoorLoad(k00 + n + 2u), t.x), t.y);
    float vert = min(v.x - wp.y, wp.y - v.y) * vertical;
    if (vert <= 0.0) continue;
    float day = min(vert, saturate(v.z / (semi ? 1.0 : 0.25)));
    float night = min(vert, saturate(v.w / (semi ? 4.0 : 0.25)));
    if (day > inside.x) { inside.x = day; headroom = v.x - wp.y; }
    if (semi) inside.z = max(inside.z, night);
    else inside.y = max(inside.y, night);
  }
  return saturate(inside);
}
float3 LocalIndoorAmounts(float3 wp) {
  float headroom;
  return LocalIndoorAmounts(wp, headroom);
}
float LocalIndoorAmount(float3 wp) {
  return LocalIndoorAmounts(wp).x;
}

// New San Vanelona at night: the interiors keep their own (day-baked) light,
// lit by their lamps; the night grade dims the outdoor world. Same feathered
// coverage as the shadow exception. The environment families first lower the
// day's baked sunlight to the shade level (SampleStaticSunStrengthNight):
// everywhere outdoors, and in enclosed interiors where the sun reaches; a
// semi-open area keeps its baked light capped at its lamp level instead.
float3 WorldNightGrade(float3 wp) {
  float3 grade = NightGrade();
  if (night_grade.w <= 0.5) return grade;
  float3 a = LocalIndoorAmounts(wp);
  return lerp(grade, float3(1.0, 1.0, 1.0), max(a.y, a.z));
}
