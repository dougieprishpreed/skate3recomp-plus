// Original Skate 2 water.default / water.flowing (families 34 / 33).
// Source: render.big water/flowingwater_defaultPS, skater.bin m_params,
// and the original executable's PCA animation table. See the research report.
float3 Skate2PcaNormal(float2 uv, float tonedown) {
  float4 c0 = macro.Sample(smp, uv) * 2.0 - 1.0;
  float4 c1 = detail_map.Sample(smp, uv) * 2.0 - 1.0;
  float3 n = s2_pca[0].xyz + float3(
      dot(c0, s2_pca[1]) + dot(c1, s2_pca[2]),
      dot(c0, s2_pca[3]) + dot(c1, s2_pca[4]),
      dot(c0, s2_pca[5]) + dot(c1, s2_pca[6]));
  return normalize(lerp(float3(0, 1, 0), n * 2.0 - 1.0, tonedown));
}

float4 ShadeSkate2Water(VSOut i, bool flowing, float fog_a,
                       float3 fog_rgb, float expo) {
  // Authored collection rows, not asset-specific colour corrections.
  float4 p0 = flowing ? float4(1, .2, 1, 1) : float4(0, 1, .31, .31);
  float4 p1 = flowing ? float4(0, -.2, 0, -.1) : float4(.01, .01, 0, 0);
  float4 p2 = flowing ? float4(.2, .7, .3, .8) : float4(.02, .02, 1, 1);
  float4 p3 = flowing ? float4(0, .1, 27, 0) : float4(1, .11, 500, 0);
  float4 uv = i.uv.xyxy * (flowing ? 1.0 : p3.x) * p2 + p1 * overlay.z;
  float3 wn, ripple;
  float2 diffuse_uv = i.uv;
  if (flowing) {
    ripple = normalize(p0.xzw * (macro.Sample(smp, uv.xy).rgb * 2.0 - 1.0 +
                                macro.Sample(smp, uv.zw).rgb * 2.0 - 1.0));
    float3 n = dot(i.nrm, i.nrm) > .01 ? normalize(i.nrm) :
        normalize(cross(ddx(i.rpos), ddy(i.rpos)));
    float3 t, b;
    if (i.tanb.w > .1) {
      t = normalize(i.tanb.xyz);
      b = cross(n, t) * (i.tanb.w > .6 ? 1.0 : -1.0);
    } else {
      ScreenTangentFrame(n, i.rpos, i.uv, t, b);
    }
    wn = normalize(ripple.x * t + ripple.y * b + ripple.z * n);
  } else {
    ripple = Skate2PcaNormal(uv.zw, p0.z);
    wn = ripple;
    diffuse_uv += .02 * Skate2PcaNormal(uv.xy, p0.w).xz;
  }
  float3 d = diffuse.Sample(smp, diffuse_uv).rgb;
  float3 masks = overlay.x > 0.0 ?
      saturate(decal_art.Sample(smp, diffuse_uv).rgb - p3.y) : float3(0, 0, 0);
  float3 eye = -normalize(i.rpos);
  float visibility = 1.0;
  if (dot(wn, sh_sun.xyz) > 0.0) {
    visibility = min(SampleCsmShadowSoft(i.rpos + cam_pos.xyz, 0.0, wn, i.pos.xy),
                     SampleStaticSun(i.rpos + cam_pos.xyz, wn, i.pos.xy));
  }
  // S2 GetShadowedLightMap: preserve mono luminance separately from chroma.
  // Its alpha (used by specular/reflection) is NOT the RGB lightmap's green.
  float2 lm_uv = i.uv2 + .01 * ripple.xz;
  float4 packed = lightmap.SampleLevel(smp_clamp, lm_uv, 0.0);
  uint channel = (uint)(tint.r - 2.0 + .5);
  float mono = channel == 0 ? packed.r : channel == 1 ? packed.g :
               channel == 2 ? packed.b : dot(packed.rgb, float3(.33, .34, .33));
  float lm = mono * mono;
  float3 chroma = chromaticity_map.SampleLevel(smp_clamp, lm_uv, 0.0).rgb;
  float blend = saturate((lm - .04) * 50.0) * (1.0 - visibility);
  float3 light = lerp(lm * chroma * 4.5, float3(.07, .09, .1), blend);
  float luminance = lerp(min(lm, .04), lm, visibility);
  float3 axis = float3(.64, .64, .420465);
  float kd = dot(wn, float3(axis.xy * sign(sh_sun.xy), axis.z)) / normalize(axis).z;
  float3 lin = d * d * light * kd;
  float3 reflected = reflect(eye, wn);
  float3 cube = overlay.y > 0.0 ?
      env_cube.SampleBias(smp, float3(-reflected.xy, reflected.z), misc.y).rgb :
      float3(0, 0, 0);
  // The authored saturate(4*(mask.b-1.5)) term is zero for this [0,1] mask.
  lin += cube * luminance * masks.b * 1.5;
  float ks = pow(max(saturate(dot(eye, reflect(-sh_sun.xyz, wn))), 1e-6), p3.z);
  lin += masks.r * ks * float3(18, 15, 10.5) * saturate(luminance - .1);
  lin *= NightGrade();
  return ToneOut((lin * p0.y * fog_a + fog_rgb) * expo, 1.0, false);
}

// S2 ocean.default: original ocean_defaultPS plus material_ocean.default
// from skater.bin (array 0x97330). Includes Lake Sherwin and other surfaces
// using this material/packed-lighting layout, with no location overrides.
float4 ShadeSkate2Ocean(VSOut i, float fog_a, float3 fog_rgb, float expo) {
  const float4 p0 = float4(.25, .19475, .0875000134, 1);
  const float4 p1 = float4(.4, 1.2, .22, 44);
  const float4 p2 = float4(14, 9, 1.5, 0);
  float ov = overlay.z > 0.0 ?
      saturate(2.0 * spec2_map.Sample(smp, i.uv * overlay.w).x - .5) : 1.0;
  // Unlike S3's shader, the S2 PCA table and normal are already Y-up.
  float3 n = Skate2PcaNormal(i.uv * p1.w, p1.z * 2.0 * ov);
  n = normalize(max(abs(n), .001));
  float3 eye = -normalize(i.rpos);
  float2 lm_uv = i.uv2 + .01 * n.xz;
  float4 packed = lightmap.SampleLevel(smp_clamp, lm_uv, 0.0);
  uint channel = (uint)(tint.r - 2.0 + .5);
  float mono = channel == 0 ? packed.r : channel == 1 ? packed.g :
               channel == 2 ? packed.b : dot(packed.rgb, float3(.33, .34, .33));
  float luminance = mono * mono;
  float3 chroma = chromaticity_map.SampleLevel(smp_clamp, lm_uv, 0.0).rgb;
  float3 r = reflect(eye, n);
  float3 cube = overlay.y > 0.0 ?
      env_cube.SampleBias(smp, float3(-r.xy, r.z), misc.y).rgb : float3(0, 0, 0);
  // Fresnel's low-angle rise x scene_opts.w (skate3_skate2_ocean_glare). At
  // a low angle the reflection is the cube's pale horizon band, which at the
  // held zone-maximum exposure tone-maps to white across the distant sea
  // (the console's auto-exposure would dim that view); looking down (0.25)
  // is unchanged.
  float fresnel = .25 + .75 * scene_opts.w * pow(1.0 - saturate(abs(dot(n, eye))), 5.0);
  float3 u = normalize(float3(-n.z, 0, n.x));
  float3 v = cross(n, u);
  float3 h = normalize(sh_sun.xyz + eye);
  float ax = p2.z / p2.x, ay = p2.z / p2.y;
  float eu = dot(h, u) / ax, ev = dot(h, v) / ay;
  float ward = 0.0;
  float ne_nl = dot(n, eye) * dot(n, sh_sun.xyz);
  if (ne_nl > 0.0) {
    ward = exp(-2.0 * (eu * eu + ev * ev) / max(1.0 + dot(h, n), 1e-6)) /
        max(sqrt(ne_nl) * 12.5663706144 * ax * ay, 1e-6);
  }
  // GetLightMap_SingleTap returns RGB=mono^2*chroma*4.5 and A=mono^2.
  // Reading packed.rgb as coloured light instead was turning the lake green.
  float3 lin = cube * (luminance * chroma * 4.5) * fresnel +
               ward * luminance * p0.rgb;
  lin *= NightGrade();
  return ToneOut((lin * p1.y * fog_a + fog_rgb) * expo, 1.0, false);
}

// Water families, hand-ported from the game's own pixel shaders and
// verified per-pixel against the ucode: flowingwateralpha (fam 30),
// the ocean surface (fam 31) and the baked ocean horizon sheet
// (fam 32). Callers pass the shared world-family locals (squared
// diffuse, fog terms, scene exposure).
// flowingwateralpha (fam 30): the canal/waterfall water shader,
// hand-ported from flowingwateralpha_defaultPS and verified per-pixel
// against the ucode (dual time-scrolled ripple-normal taps -> TBN ->
// lightmap-lit body + phong sun spec + cube reflection, then the
// standard fog/exposure/tonemap chain; alpha = the spec term's green
// channel, floored; reflection strength drives opacity over the bed).
float4 ShadeFlowingWater(VSOut i, float3 dlin, float fog_a,
                         float3 fog_rgb, float expo) {
  float t = wat_p3.x;
  float3 n1 = macro.Sample(smp, i.uv * wat_p2.xy + wat_p1.xy * t).rgb;
  float3 n2 = macro.Sample(smp, i.uv * wat_p2.zw + wat_p1.zw * t).rgb;
  float3 nsum = 2.0 * n1 + 2.0 * n2 - 2.0;
  // vNormal = normalize(m_params[0].xzw * (n1 + n2)) in tangent space.
  float3 vn = normalize(float3(nsum.x * wat_p0.x, nsum.y * wat_p0.z,
                               nsum.z * wat_p0.w));
  float3 wn0 = dot(i.nrm, i.nrm) > 0.01
                   ? normalize(i.nrm)
                   : normalize(cross(ddx(i.rpos), ddy(i.rpos)));
  // Stored frame: tanb = the mesh's usage-6 TANGENT here (see the water
  // pack in DecodeMesh), sentinel sign = the unwrap sign.x;
  // B = sign.x * cross(N, T), exactly the game's VS.
  float3 wt, wb;
  if (i.tanb.w > 0.1) {
    wt = normalize(i.tanb.xyz);
    wb = cross(wn0, wt) * (i.tanb.w > 0.6 ? 1.0 : -1.0);
  } else {
    ScreenTangentFrame(wn0, i.rpos, i.uv, wt, wb);
  }
  float3 wn = normalize(vn.x * wt + vn.y * wb + vn.z * wn0);
  float3 vd = -normalize(i.rpos);
  float s = min(SampleCsmShadowSoft(i.rpos + cam_pos.xyz, 0.0, wn, i.pos.xy),
                SampleStaticSun(i.rpos + cam_pos.xyz, wn, i.pos.xy));
  // Lightmap at the unwrap + 0.01 * vNormal.xz (the ripple refraction
  // wobble), console fetch semantics, min-clamped like every world
  // family. White-fallback pages skip the clamp (see the env fetch).
  float2 wlm_uv = i.uv2 + 0.01 * vn.xz;
  float3 wlm = lightmap.SampleLevel(smp_clamp, wlm_uv, 0.0).rgb;
  float3 lml = tint.r > 0.0 ? min(wlm * wlm, s + sh_color.rgb)
                            : wlm * wlm;
  // GetTangentLight on the WORLD normal: fixed axis (0.58, 0.62, 0.39)
  // with xy signs following the sun direction, x 1/normalize(axis).z.
  float kd = (wn.x * 0.58 * sign(sh_sun.x) +
              wn.y * 0.62 * sign(sh_sun.y) + wn.z * 0.39) * 2.39562;
  float3 lin = lml * kd * dlin;
  // Spec/reflection masks (t4): x = phong mask, z = reflection mask,
  // thresholded by m_params[3].y. Zero until resolved: matte water.
  float2 wmask = float2(0.0, 0.0);
  if (overlay.x > 0.0) {
    float4 wm4 = decal_art.Sample(smp, i.uv);
    wmask = saturate(float2(wm4.x, wm4.z) - wat_p3.y);
  }
  // Phong vs the real sun, power m_params[3].z, tint (2.1, 1.8, 1.5),
  // scaled by sat(clamped lightmap green - 0.1).
  float3 wrl = 2.0 * wn * dot(wn, sh_sun.xyz) - sh_sun.xyz;
  float ks = pow(max(saturate(dot(vd, wrl)), 1e-6), wat_p3.z);
  float3 wspec =
      ks * wmask.x * float3(2.1, 1.8, 1.5) * saturate(lml.g - 0.1);
  // Cube reflection, same shape as fams 5/6: reflect(E, wN) with xy
  // negated, luminosity lerped toward 1 by 0.3 * sat(4*mask - 2.6),
  // x mask x 1.5. misc.y = the cube LOD bias (640p-gradient parity).
  float3 wrv = vd - 2.0 * wn * dot(vd, wn);
  float3 wcube =
      overlay.y > 0.0
          ? env_cube.SampleBias(smp, float3(-wrv.x, -wrv.y, wrv.z),
                                misc.y).rgb
          : float3(0.0, 0.0, 0.0);
  float wrlum = 0.3 * saturate(4.0 * wmask.y - 2.6);
  float3 wrefl = wcube * (lml.g + wrlum * (1.0 - lml.g)) * wmask.y * 1.5;
  float3 wspec_term = wspec + wrefl;
  lin += wspec_term;
  lin *= NightGrade();
  float3 wxe = (lin * (fog_a * wat_p0.y) + fog_rgb) * expo;
  return ToneOut(wxe, max(wspec_term.g, wat_p3.w), false);

}

// Exact ocean surface (fam 31, ocean_defaultPS; verified per-pixel
// against the ucode): PCA-compressed dual-component normal map, lerped
// toward flat by the overlay-scaled tonedown, cube reflection x squared
// lightmap x fresnel, ward anisotropic sun spec. OPAQUE (the game's PS
// outputs alpha 0 and draws without blending).
float4 ShadeOcean(VSOut i, float fog_a, float3 fog_rgb, float expo) {
  float2 suv = i.uv * oc_p1.w;
  float4 c0v = macro.Sample(smp, suv) * 2.0 - 1.0;       // t3: comp 0
  float4 c1v = detail_map.Sample(smp, suv) * 2.0 - 1.0;  // t8: comp 1
  // PCA normal in the shader's Z-UP basis; mean/weights are stored in
  // 0..1 texture space and folded 2x-1 after the sum.
  float3 pca = (float3(dot(c0v, oc_w0) + dot(c1v, oc_w1),
                       dot(c0v, oc_w2) + dot(c1v, oc_w3),
                       dot(c0v, oc_w4) + dot(c1v, oc_w5)) +
                oc_mean.xyz) * 2.0 - 1.0;
  // tonedown = 2 * m_params[1].z * sat(dot(c1v, wB1) + overlay.x);
  // the ucode folds the second PCA blue dot into the overlay term.
  float ovx = overlay.z > 0.0
                  ? spec2_map.Sample(smp, i.uv * overlay.w).x
                  : 1.0;
  float otd = (2.0 * oc_p1.z) * saturate(dot(c1v, oc_w5) + ovx);
  float3 nt = normalize(lerp(float3(0.0, 0.0, 1.0), pca, otd));
  nt = normalize(max(abs(nt), 0.001));
  float3 N = float3(nt.x, nt.z, nt.y);  // shader Z-up -> world Y-up
  float3 E = -normalize(i.rpos);
  // Cube reflection: reflect(E, N) with xy negated, x the SQUARED
  // single-tap lightmap (no chromaticity, no CSM clamp in this PS).
  float3 orv = E - 2.0 * N * dot(E, N);
  float3 ocube =
      overlay.y > 0.0
          ? env_cube.SampleBias(smp, float3(-orv.x, -orv.y, orv.z),
                                misc.y).rgb
          : float3(0.0, 0.0, 0.0);
  float2 olm_uv = i.uv2 + 0.01 * nt.xy;
  float3 olm = lightmap.SampleLevel(smp_clamp, olm_uv, 0.0).rgb;
  float3 orefl = ocube * (olm * olm);
  float NV = abs(dot(N, E));
  float F = 0.25 + 0.75 * pow(1.0 - NV, 5.0);
  // Ward anisotropic sun spec (spreads oc_p2.xy, roughness oc_p2.z).
  float3 oud = normalize(float3(-N.z, 0.0, N.x));
  float3 ovd = cross(N, oud);
  float dNE = dot(N, E);
  float dNL = dot(N, sh_sun.xyz);
  float oax = oc_p2.z / oc_p2.x;
  float oay = oc_p2.z / oc_p2.y;
  float3 H = normalize(sh_sun.xyz + E);
  float eu = dot(H, oud) / oax;
  float ev = dot(H, ovd) / oay;
  float oden = sqrt(abs(dNL * dNE)) * 12.566371 * oax * oay;
  float ward = exp(-2.0 * (eu * eu + ev * ev) / (1.0 + dot(H, N))) /
               max(oden, 1e-6);
  float3 ocol = (orefl * F + ward * oc_p0.rgb) * NightGrade();
  float3 oxe = (ocol * (fog_a * oc_p1.y) + fog_rgb) * expo;
  return ToneOut(oxe, 1.0, false);

}

// Baked ocean horizon sheet (fam 32, oceanreflection_defaultPS;
// verified per-pixel): squared sea texture x multiplier, fogged and
// tonemapped; alpha ramps with world height between the orf.z/orf.y
// bounds (the bright haze band fading in toward the horizon).
float4 ShadeOceanReflection(VSOut i, float3 dlin, float fog_a,
                            float3 fog_rgb, float expo) {
  float3 rcol = dlin * orf.x * NightGrade();
  float ra = saturate((i.rpos.y + cam_pos.y - orf.z) /
                      max(orf.y - orf.z, 1e-4));
  float3 rxe = (rcol * fog_a + fog_rgb) * expo;
  return ToneOut(rxe, ra, false);

}
