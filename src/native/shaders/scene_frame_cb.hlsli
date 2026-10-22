// Per-frame shadow / lighting / material constant slice (b1). Shared by
// the scene pass (scene.hlsl) and the HDR post chain (hdr.hlsl) - the
// volumetric march samples sun visibility through the same receiver
// rows. The CPU staging in skate3_native_scene_gpu.cpp mirrors this
// layout; row offsets there and here must agree.
cbuffer S : register(b1) {
  float4 sh_x;      // light-space X row (xyz) + translation (w)   [PS c0]
  float4 sh_y;      // light-space Y row                           [PS c3]
  float4 sh_z;      // depth row (height ramp)                     [PS c4]
  float4 sh_c1;     // cascade 1 scale.xy + offset.zw              [PS c1]
  float4 sh_c2;     // cascade 2 scale.xy + offset.zw              [PS c2]
  float4 sh_color;  // shadow color rgb [PS c8] + its luma in w
  float4 sh_misc;   // x = depth bias [PS c5.x], y = enable,
                    // zw = atlas dimensions (3*tile, tile)
  // Exact world-shading frame rows (consumed by the env-family branch):
  float4 sh_sun;    // xyz = sun direction [PS c6], w = scene exposure [c10.x]
  float4 sh_env;    // x = material multiplier [PS c11.y], yz = tree lightmap
                    // scale/floor [tree PS c0.xy], w = tree tint mult [c4.y]
  float4 sh_fogp;   // xyz = global fog ramp scale/bias/exp [VS c5],
                    // w = proxyworld scale [proxy PS c3.y]
  float4 sh_fogc;   // fog color rgb + transmittance scale in w [VS c6]
  // dynamicobject.fx frame-global lighting rows (see FrameScene::dynobj_rows).
  float4 dyn_sun;   // xyz = sun direction (PS c9), w = scene exposure (c13.x)
  float4 dyn_amb;   // xyz = flat ambient (c15.rgb), w = bounce scale (c15.w)
  float4 dyn_misc;  // x = material multiplier (c14.y),
                    // y = static world-shadow floor (c8.w)
  // Character CSM receive biases (game char PS bank, finest cascade first)
  // + enable in w: the exact 9-tap character shadow sampling
  // (cacstamp_skin_nisPS port; see SampleCsmShadow). sh_misc.zw carry the
  // atlas dimensions for the point-snapped taps.
  float4 sh_char;
  // World-shading v2: x = stored-tangent polarity (the sign relating
  // cross(binormal, normal) x handedness to the game's tangent; +-1,
  // cvar-tunable against the emulated reference), yzw spare.
  float4 sh_v2;
  // dynamicobject static world-shadow transform (the game's PS c5/c6/c7):
  // u = dot(wp4, dyn_wsx) * 0.5 + 0.5, v = dot(wp4, dyn_wsy) * -0.5 + 0.5,
  // ray depth = dot(wp4, dyn_wsz). Compared against the natively
  // re-rendered 512x512 baked-shade map at t4 (misc.z flag 8; see the
  // dynobj branch).
  float4 dyn_wsx;
  float4 dyn_wsy;
  float4 dyn_wsz;
  // flowingwateralpha m_params rows (captured per frame from the water PS
  // bank; consumed by the exact water branch, cam_pos.w = -30):
  float4 wat_p0;  // m_params[0]: tangent-normal scale xzw, material mult y
  float4 wat_p1;  // m_params[1]: animated-UV scroll speeds (tap1 xy, tap2 zw)
  float4 wat_p2;  // m_params[2]: tap UV scales (tap1 xy, tap2 zw)
  float4 wat_p3;  // x = g_fAnimationTime, y = mask threshold,
                  // z = spec power, w = alpha floor
  // ocean_defaultPS rows (exact ocean branch, cam_pos.w = -31): the PCA
  // mean (pre-swizzled to R,G,B channel order) + the six weight rows
  // (component-0/1 pairs for R, G, B) + m_params[0..2].
  float4 oc_mean;
  float4 oc_w0;   // R, component 0
  float4 oc_w1;   // R, component 1
  float4 oc_w2;   // G, component 0
  float4 oc_w3;   // G, component 1
  float4 oc_w4;   // B, component 0
  float4 oc_w5;   // B, component 1
  float4 oc_p0;   // m_params[0]: ward spec color
  float4 oc_p1;   // m_params[1]: y = multiplier, z = tonedown scale,
                  // w = PCA-tap uv scale
  float4 oc_p2;   // m_params[2]: ward spread u/v (xy), roughness (z)
  // oceanreflection_defaultPS row (fam 32): x = multiplier, y/z = the
  // world-height alpha fade bounds.
  float4 orf;
  // Contact-hardening soft shadows (PCSS over the same cascade atlas; see
  // SampleCsmShadowSoft): x = enable, y = tan of the sun's angular radius
  // (penumbra width per meter of caster distance), z = max penumbra
  // half-width in meters, w = static-caster receive bias in depth units
  // (slope-scaled in the sampler; zero when static geometry is not in the
  // atlas).
  float4 sh_pcss;
  // x = blocker-search radius in meters, y = meters per depth-map unit
  // (1 / |sh_z.xyz|), z = light-space units per meter (|sh_x.xyz|, base
  // cascade), w = minimum filter radius in physical atlas texels.
  float4 sh_pcss2;
  // Native static sun-shadow map transform (see SampleStaticSun): the
  // world -> FAR-tile rows. uc = dot(nsm_x.xyz, wp) + nsm_x.w in [-1,1]
  // (same for y), depth = dot(nsm_z.xyz, wp) + nsm_z.w in [0,1]. The near
  // tile shares the origin, so its coordinate is exactly uc * nsm_p2.z.
  float4 nsm_x;
  float4 nsm_y;
  float4 nsm_z;
  // x = static shadow strength (0 = feature off), y = far-tile uc units
  // per meter (1 / far radius), z = meters per depth unit, w = minimum
  // filter radius in tile uc units.
  float4 nsm_p;
  // x = base receiver bias (depth units), y = slope-scaled bias term
  // (depth units), z = reserved (cascade ratios are the shader-side
  // kRatioInner/kRatioMid constants), w = tile size in pixels (tap
  // clamping).
  float4 nsm_p2;
  // Skate 2 PCA water animation. Separate from the captured S3 ocean rows.
  float4 s2_pca[7]; // mean, R0, R1, G0, G1, B0, B1 (cb[164..191])
  // Skate 2 vertex animation (vertexanimate.fx flags / treeanimate.fx
  // trees), read by vs_main: x = g_fAnimationTime (seconds), y = enable
  // (New San Van only) (cb[192..195]); then m_params[0..2] of
  // animated.flag (rows 0-2) and animated.tree (rows 3-5) (cb[196..219]).
  float4 s2_anim_t;
  float4 s2_anim[6];
  // Scene options (cb[220..223]): x = smooth lightmap reconstruction
  // (cubic B-spline over the baked lighting atlas, see SampleWorldLightmap),
  // y = cube LOD bias log2(render height / 640) for Skate2SpecTerm, z = the
  // static sun strength WITHOUT the venue factor (Stadium crowd seating),
  // w = the Skate 2 ocean's low-angle reflection rise kept
  // (skate3_skate2_ocean_glare; 1 = Skate 2's original Fresnel).
  float4 scene_opts;
  // New San Vanelona at night (cb[224..227]): xyz = linear factor on the
  // world's baked light, matched to the Sanitarium's night lightmaps (see
  // kNightGrade); w > 0.5 enables. Zero outside night mode and New San Van.
  float4 night_grade;
  // Designated interior zones (cb[228..235]): minXZ, inverse cell size, width;
  // height, header address, vertical feather reciprocal (negative = covered
  // open places off), lattice texels per metre.
  // Width0 disables it; all Skate3/other-map frames keep these rows zero.
  float4 indoor_grid;
  float4 indoor_info;
  // cb[236..239]: z = live shadows of things above a zone's roof stop at it
  // (skate3_skate2_indoor_ceiling_shadows); x, y, w unused. Zero outside
  // New San Van.
  float4 indoor_day;
};
// The night factor for baked and pre-lit world light (1 when off). Applied
// after the shadow clamp, so live shadows keep their daylight contrast.
float3 NightGrade() {
  return night_grade.w > 0.5 ? night_grade.xyz : float3(1.0, 1.0, 1.0);
}
