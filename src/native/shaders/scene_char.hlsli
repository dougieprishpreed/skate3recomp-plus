// Character families: the game's own lighting in LINEAR space (diffuse is
// gamma -> square it), then the exact tone chain from the disassembly and
// the postfx uber's 1.41 scene multiplier (which the empirical world
// shading already folds into its constants; without it characters sit
// ~30% darker than their surroundings, measured on an F11 A/B pair).
float4 ShadeCharacter(VSOut i, float4 albedo) {
  float fam = cam_pos.w;
  float3 dlin = albedo.rgb * albedo.rgb;
  float3 cn = dot(i.nrm, i.nrm) > 0.01
                  ? normalize(i.nrm)
                  : normalize(cross(ddx(i.rpos), ddy(i.rpos)));
  float ndl = saturate(dot(cn, ch_light.xyz));
  float3 vd = -normalize(i.rpos);
  float3 lin;
  float out_a = 1.0;
  if (fam > 7.5) {
    // Hall of Meat x-ray bones, exact from defaulthom_defaultPS (ucode +
    // constant table, out/research/log084/s3_disasm): unlit.
    //   tinted = max(D^2, colorParams.y) * boneClr
    //   rim    = (1 - sat(N.V))^m_params[1].w
    //   col    = tinted + D^2 (1 - rim) colorParams.x * (D^2 - tinted)
    // times m_params[0].y, fixed exposure 1. ch_tintA = boneClr,
    // ch_tintB.xyz = (colorParams.x, colorParams.y, m_params[0].y),
    // ch_light.w = the rim power.
    float3 tinted = max(dlin, ch_tintB.y) * ch_tintA.rgb;
    float rim = pow(max(1.0 - saturate(dot(vd, cn)), 1e-6), ch_light.w);
    float3 core = dlin * (1.0 - rim) * ch_tintB.x;
    lin = (tinted + core * (dlin - tinted)) * ch_tintB.z;
    return ToneOut(lin, 1.0, false);
  } else if (fam > 5.5) {
    // Traffic vehicles (vehicle.fx fam 6 body / vehicle_glass.fx fam 7
    // windows, disassembled from vehicle_defaultPS): paint recolor where
    // the diffuse green channel is below the mask threshold (red-channel
    // mask * colorize_red + blue-channel mask * colorize_blue, the taxi
    // yellow), key light + the livingworld flat ambient, phong specular
    // along the reflected sun, and an environment-cube reflection scaled
    // by fresnel and the gloss packed in the diffuse alpha. Glass keeps
    // only the reflection terms (its tint rows are zero) and blends at
    // the captured alpha. overlay.y > 0 = the material's cube resolved
    // at t6 (same convention as water).
    float3 sel;
    if (fam < 6.5) {
      sel = dlin.g > 0.001225
                ? dlin
                : ch_tintA.rgb * dlin.r + ch_tintB.rgb * dlin.b;
    } else {
      sel = ch_tintA.rgb;
    }
    // DXN panel normal map (the material's `normal` channel, riding the
    // macro slot; overlay.z > 0 = resolved). The vertex layout carries no
    // tangent frame, so build a screen-space cotangent frame from the
    // position/uv derivatives; it reproduces the authored panel shading
    // including mirrored UV islands. Skipping the map entirely shades the
    // hinged panels by their vertex normals, which face away from the sun
    // - the dark ambient-blue "misdrawn shadow" that stopped at the door
    // seam (verified against the ucode: flat map = the artifact, real
    // map = the emulated car).
    float3 vn = cn;
    if (overlay.z > 0.5) {
      // DXT5nm (overlay.w, from the resolved format in both games) uses
      // AG, the channels the game's vehicle PSes read; a DXN map keeps RG.
      float4 normal_sample = macro.Sample(smp, i.uv);
      float2 nm = (overlay.w > 0.5 ? normal_sample.ag : normal_sample.rg) * 2.0 - 1.0;
      // The calibrated screen frame every other family uses (negated U
      // axis, raw V axis: measured against the stored usage-6/7 frames of
      // world and character meshes; the game's vehicle VS builds its frame
      // from the same stored tangent/binormal). This branch used to build
      // its own UNNEGATED frame: the map's x tilt ran backwards, and the
      // panels' baked curvature lit as large dark blotches on hoods and
      // doors (user A/B vs the emulated renderer, 15 Nov).
      float3 tt, bb;
      ScreenTangentFrame(cn, i.rpos, i.uv, tt, bb);
      vn = normalize(nm.x * tt + nm.y * bb +
                     cn * sqrt(saturate(1.0 - dot(nm, nm))));
    }
    float vndl = saturate(dot(vn, ch_light.xyz));
    float3 rfl = ch_light.xyz - 2.0 * dot(vn, ch_light.xyz) * vn;
    // The sun spec is gated on N.L >= 0 (the ucode multiplies the spec
    // term by an sge result); the cube reflection is not.
    float spec = pow(saturate(dot(vd, -rfl)), max(ch_sh[0].w, 1.0)) *
                 (dot(vn, ch_light.xyz) >= 0.0 ? 1.0 : 0.0);
    float fres = pow(1.0 - saturate(dot(vn, vd)), max(ch_light.w, 1.0));
    float3 cube = float3(0.0, 0.0, 0.0);
    if (overlay.y > 0.5) {
      // vehicle_defaultPS looks the cube up at (R.x, R.y, -R.z), R =
      // reflect(-V, N): its reflected eye vector has xy negated (c252.y =
      // -1), the same lookup as the world and water families. The
      // unmirrored lookup reflected the wrong side of the surroundings.
      float3 rv = reflect(-vd, vn);
      cube = env_cube.Sample(smp, float3(rv.x, rv.y, -rv.z)).rgb;
      cube *= cube;  // the PS consumes the cube squared (linear space)
    }
    // Gloss = the diffuse alpha SQUARED: the ucode squares the whole
    // diffuse fetch (linear-space decode), alpha included; raw alpha
    // over-specs ~5x and mottles the body panels.
    float gloss = fam < 6.5 ? albedo.a * albedo.a : 1.0;
    // vehicle_defaultPS / vehicle_glass_defaultPS (ucode, both games' cars)
    // shadow the direct diffuse and the key specular only, ambient and the
    // environment reflection stay unshadowed: shadow = min(CSM x (N.L >= 0),
    // max(world shadow, g_CSMSelfBias.w)). Before, only New San Van's
    // imported cars took it, so Skate 3's cars stayed fully sunlit in shade
    // (the "bright cars"). The CSM tap keeps the character receiver bias.
    float vehicle_shadow = min(
        SampleCsmShadowSoft(i.rpos + cam_pos.xyz, 0.012, cn, i.pos.xy) *
            (dot(vn, ch_light.xyz) >= 0.0 ? 1.0 : 0.0),
        max(SampleStaticSun(i.rpos + cam_pos.xyz, cn, i.pos.xy), ch_misc.z));
    lin = sel * (ch_key.rgb * vndl * vehicle_shadow + ch_amb.rgb) +
          (spec * ch_sh[0].rgb * vehicle_shadow + cube) * fres * gloss;
    out_a = ch_misc.x;
  } else if (fam > 3.5 && fam < 4.5) {
    // CAC player hair, exact from cac_hair_defaultPS (ucode + constant
    // table, out/research/log084/s3_disasm; same body as Skate 2's
    // cac_hair.fx source):
    //   D^2 * [ c16 * (sat(0.75 N.L + 0.25) * shadow + c14.w/4)
    //         + c17 * sinTH^c11.w * sat(1.75 N.L + 0.25) * shadow ]
    // N = the DXN normal map on the strand frame, T.H = the Kajiya-Kay
    // strand term along the tangent (H = normalize(V + L)), shadow =
    // max(world shadow, c3.w). The world shadow is the baked building/tree
    // shade (our native static sun map); hair takes no CSM. The previous
    // port used defaulthair's fresnel rim here and no shadow at all, so
    // hair stayed fully lit in shade and read far lighter than its color.
    float3 htt, hbb;
    ScreenTangentFrame(cn, i.rpos, i.uv, htt, hbb);
    float3 hn = cn;
    if (overlay.z > 0.5) {
      float2 nm = macro.SampleBias(smp, i.uv, misc.y).ag * 2.0 - 1.0;
      hn = normalize(nm.x * htt + nm.y * hbb +
                     cn * sqrt(saturate(1.0 - dot(nm, nm))));
    }
    float hndl = dot(hn, ch_light.xyz);
    float3 hh = normalize(vd + ch_light.xyz);
    float th = dot(normalize(htt), hh);
    float strand = pow(max(sqrt(saturate(1.0 - th * th)), 1e-6), ch_light.w);
    float hs = max(SampleStaticSun(i.rpos + cam_pos.xyz, hn, i.pos.xy), ch_misc.z);
    lin = dlin * (ch_key.rgb * (saturate(hndl * 0.75 + 0.25) * hs + ch_amb.w) +
                  ch_tintB.rgb * strand * saturate(hndl * 1.75 + 0.25) * hs);
    out_a = saturate(decal_art.Sample(smp, i.uv2).r * ch_tintB.w);
  } else if (fam > 3.5) {
    // Pro / pedestrian hair (defaulthair), exact from defaulthair_defaultPS
    // (ucode + constant table, out/research/log084/s3_disasm; the same body
    // as Skate 2's character_functions.fxh CalculateCharacterLighting):
    //   lit  = c10 * sat(N.L) * shadow + c14.w/4 + c15 * sat(sat(N.RL) * rF)
    //   spec = sat(D.a^2 * (phong(L)^c11.w * shadow * c11 * kF
    //                     + phong(RL)^c12.w * c12 * rF))
    //   col  = (D^2 * lit + spec) * m_params[0].y
    // RL = normalize(L * (-1, 0.2, -1) - V) (the game's fixed rim light),
    // kF / rF = (1 - sat(N.V))^c10.w / ^c9.z, phong(X) = sat(V.-reflect(X)),
    // the key spec with no N.L gate (unlike the body PSes). shadow =
    // max(world shadow, c3.w) as in cac_hair: the baked building/tree shade
    // (our static sun map); hair takes no CSM. N = the DXN normal map on
    // the strand frame (cac_hair's channel pairing). The previous port had
    // no shadow term and swapped the key and rim colors, so the key light
    // rode an unshadowed rim term and light (blonde) hair stayed bright in
    // shade. Strand coverage from the mesh's "alpha" channel at the raw
    // second texcoord (bound at t4), alpha-blended in the sorted sub-pass.
    float3 htt, hbb;
    ScreenTangentFrame(cn, i.rpos, i.uv, htt, hbb);
    float3 hn = cn;
    if (overlay.z > 0.5) {
      float2 nm = macro.SampleBias(smp, i.uv, misc.y).ag * 2.0 - 1.0;
      hn = normalize(nm.x * htt + nm.y * hbb +
                     cn * sqrt(saturate(1.0 - dot(nm, nm))));
    }
    float hndl_u = dot(hn, ch_light.xyz);
    float hs = max(SampleStaticSun(i.rpos + cam_pos.xyz, hn, i.pos.xy), ch_misc.z);
    float fb = 1.0 - saturate(dot(hn, vd));
    float kfres = pow(max(fb, 1e-6), ch_rim.w);
    float rfres = pow(max(fb, 1e-6), ch_misc.w);
    float3 rd = normalize(ch_light.xyz * float3(-1.0, 0.2, -1.0) - vd);
    float3 lit = ch_key.rgb * saturate(hndl_u) * hs + ch_amb.w +
                 ch_rim.rgb * saturate(saturate(dot(hn, rd)) * rfres);
    float3 spec = float3(0.0, 0.0, 0.0);
    if (ch_ks.w > 0.0) {
      float3 kr = ch_light.xyz - 2.0 * hndl_u * hn;
      float ks = pow(saturate(dot(vd, -kr)), ch_ks.w) * hs;
      float3 rr = rd - 2.0 * dot(hn, rd) * hn;
      float rs = pow(saturate(dot(vd, -rr)), ch_rs.w);
      spec = saturate(albedo.a * albedo.a *
                      (ks * ch_ks.rgb * kfres + rs * ch_rs.rgb * rfres));
    }
    lin = (dlin * lit + spec) * (ch_light.w > 0.25 ? ch_light.w : 1.0);
    out_a = saturate(decal_art.Sample(smp, i.uv2).r * ch_tintB.w);
  } else if (fam > 2.5) {
    // livingworld pedestrians, exact from livingworld_stamp_defaultPS
    // (ucode + constant table, out/research/log084/s3_disasm; Skate 2's
    // livingworld_stamp.fx source, the near and the low-detail body
    // materials both run it): the diffuse is a stamp-mask atlas; red
    // regions recolor with tintA (c22), blue with tintB (c23) (judged in
    // linear space; real-color regions have green above the threshold).
    //   lit  = c15 sat(N.L) shadow + c19.w (0.1, 0.175, 0.3)
    //        + c20 sat(sat(N.RL) rF)
    //   spec = sat(nm.g (phong(L)^c16.w shadow c16 kF + phong(RL)^c17.w c17 rF))
    //   col  = (sel lit + spec) m_params[0].y
    // N = the RGB tangent-space normal map (x on the tangent, y on the
    // binormal, z on the vertex normal, normalized after the frame; NOT
    // reconstructed), whose GREEN channel is also the spec mask
    // (fSpecValue = vNormal.g in the source). RL = normalize(L (-1, 0.2, -1)
    // - V), kF / rF = (1 - sat(N.V))^c15.w / ^c14.z, phong(X) =
    // sat(V.-reflect(X)). shadow = min(CSM x (N.L >= 0), max(world shadow,
    // c8.w)). Characters are casters themselves, so the CSM tap keeps the
    // extra receiver bias that suppresses self-shadow acne. The previous
    // port had only the key and the flat ambient: no normal map, rim light
    // or specular.
    float3 sel = dlin.g > 0.001225
                     ? dlin
                     : ch_tintA.rgb * dlin.r + ch_tintB.rgb * dlin.b;
    float3 pn = cn;
    float smask = 0.0;
    if (overlay.z > 0.5) {
      float3 nm = macro.SampleBias(smp, i.uv, misc.y).rgb * 2.0 - 1.0;
      float3 ptt, pbb;
      ScreenTangentFrame(cn, i.rpos, i.uv, ptt, pbb);
      float3 pm = nm.x * ptt + nm.y * pbb + nm.z * cn;
      pn = dot(pm, pm) > 1e-8 ? normalize(pm) : cn;
      smask = nm.y;
    }
    float pndl_u = dot(pn, ch_light.xyz);
    float pcsm = SampleCsmShadowSoft(i.rpos + cam_pos.xyz, 0.012, cn, i.pos.xy) *
                 (pndl_u >= 0.0 ? 1.0 : 0.0);
    float psh = min(pcsm, max(SampleStaticSun(i.rpos + cam_pos.xyz, cn, i.pos.xy),
                              ch_misc.z));
    float3 plit = ch_key.rgb * saturate(pndl_u) * psh + ch_amb.rgb;
    float3 pspec = float3(0.0, 0.0, 0.0);
    if (ch_ks.w > 0.0) {
      float fb = 1.0 - saturate(dot(pn, vd));
      float kfres = pow(max(fb, 1e-6), ch_rim.w);
      float rfres = pow(max(fb, 1e-6), ch_misc.w);
      float3 rd = normalize(ch_light.xyz * float3(-1.0, 0.2, -1.0) - vd);
      plit += ch_rim.rgb * saturate(saturate(dot(pn, rd)) * rfres);
      float3 kr = ch_light.xyz - 2.0 * pndl_u * pn;
      float ks = pow(saturate(dot(vd, -kr)), ch_ks.w) * psh;
      float3 rr = rd - 2.0 * dot(pn, rd) * pn;
      float rs = pow(saturate(dot(vd, -rr)), ch_rs.w);
      pspec = saturate((ks * ch_ks.rgb * kfres + rs * ch_rs.rgb * rfres) * smask);
    }
    lin = (sel * plit + pspec) * (ch_light.w > 0.25 ? ch_light.w : 1.0);
    // livingworld_stamp_defaultPS ends `max oC0.w, c21.x`: the entity's
    // spawn/distance fade. Only visible when the item is routed to the
    // blended sub-pass (alpha < 1); the opaque pass ignores it.
    out_a = ch_misc.x;
  } else {
    // defaultcharacter / CAC pieces: key light + SH irradiance ambient,
    // key gated by the CSM shadow (see the livingworld comment above).
    // The material's DXT5nm normal map (x in ALPHA, y in GREEN; the PSes
    // read tf4.wy) rides the macro slot; overlay.z > 0 = resolved. The
    // skinned vertex layout has no free bytes for the authored tangent
    // frame (blend weights/indices own them), so use the screen-space
    // cotangent frame. ScreenTangentFrame's env calibration (negated U
    // axis, raw V axis) holds for characters too: projecting the skinned
    // meshes through the frame's real view_proj and taking the D3D y-down
    // screen-basis derivatives on the CAMERA-FACING triangles dots the
    // frame against the skinned usage-6/7 tangent frame at -0.95 (tt.T) /
    // +0.95 (bb.B); an edge-basis check without the projection lands on
    // the back-face orientation and reads inverted. Map x rides T, map y
    // rides B (the VS skins the usage-6 tangent into the interpolator the
    // PS pairs with tf4.w, the usage-7 binormal into the tf4.y one).
    float3 vn = cn;
    if (overlay.z > 0.5) {
      // misc.y = LOD bias to the console's 640p-gradient mip (same
      // rationale as the reflective families' cube bias): mip 0 at 4K
      // keeps fine wrinkle noise the console filters away, which reads
      // as weaker authored folds than the emulated reference.
      float2 nm = macro.SampleBias(smp, i.uv, misc.y).ag * 2.0 - 1.0;
      float3 tt, bb;
      ScreenTangentFrame(cn, i.rpos, i.uv, tt, bb);
      vn = normalize(nm.x * tt + nm.y * bb +
                     cn * sqrt(saturate(1.0 - dot(nm, nm))));
    }
    float vndl_u = dot(vn, ch_light.xyz);
    float vndl = saturate(vndl_u);
    // ch_amb.b > 0.5 (fam 1 only) = defaultcharacter_light: no shadow receive
    // and no N.L gate on the key spec (DrawItem::char_noshadow).
    bool noshadow = fam < 1.5 && ch_amb.b > 0.5;
    float csm = noshadow
                    ? 1.0
                    : min(SampleCsmShadowSoft(i.rpos + cam_pos.xyz, 0.012, vn, i.pos.xy),
                          SampleStaticSun(i.rpos + cam_pos.xyz, vn, i.pos.xy));
    if (ch_tintA.w > 0.0) {
      dlin *= ch_tintA.rgb;
    }
    // CAC skin stamps (cacstamp_skin_defaultPS; misc.z flags staged only on
    // that layout): the composed tattoo texture at the second texcoord
    // multiplies by lerp(1, rgb, a); the second overlay (first texcoord)
    // blends over by its alpha x i_params.z (misc.w). Stamp colors are used
    // as fetched (the ucode does not square them).
    if (misc.z > 0.5) {
      uint sf = (uint)(misc.z + 0.5);
      if ((sf & 1u) != 0u) {
        // Tattoo uv = the VS rows (c190/c191) applied to (uv1, 1): row 0 in
        // mat_tint.xyz, row 1 in (overlay.xy, mat_tint.w).
        float3 u1 = float3(i.uv2, 1.0);
        float2 tuv = float2(dot(u1, mat_tint.xyz),
                            dot(u1, float3(overlay.xy, mat_tint.w)));
        // Address mode (flags 4 edge / 16 border, else wrap) from the
        // draw's fetch constant; the atlas covers only part of the uv range
        // (wrap tiled stray tattoos across the body). Border = transparent
        // outside [0,1], the multiply's neutral.
        float4 tat = (sf & 20u) != 0u ? detail_map.Sample(smp_clamp, tuv)
                                      : detail_map.Sample(smp, tuv);
        if ((sf & 16u) != 0u && any(saturate(tuv) != tuv)) {
          tat.a = 0.0;
        }
        dlin *= lerp(float3(1.0, 1.0, 1.0), tat.rgb, tat.a);
      }
      if ((sf & 2u) != 0u) {
        float4 ov2 = (sf & 40u) != 0u ? spec2_map.Sample(smp_clamp, i.uv)
                                      : spec2_map.Sample(smp, i.uv);
        if ((sf & 32u) != 0u && any(saturate(i.uv) != i.uv)) {
          ov2.a = 0.0;
        }
        dlin = lerp(dlin, ov2.rgb, ov2.a * misc.w);
      }
    }
    float3 irr = saturate(
        ch_sh[0].rgb + vn.x * ch_sh[1].rgb + vn.y * ch_sh[2].rgb +
        vn.z * ch_sh[3].rgb + (vn.x * vn.z) * ch_sh[4].rgb +
        (vn.z * vn.y) * ch_sh[5].rgb + (vn.y * vn.x) * ch_sh[6].rgb +
        (vn.z * vn.z) * ch_sh[7].rgb +
        (vn.x * vn.x - vn.y * vn.y) * ch_sh[8].rgb);
    float3 lit = ch_key.rgb * vndl * csm + irr * ch_amb.w;
    float3 spec = float3(0.0, 0.0, 0.0);
    // Rim light + key/rim phong specular, exact from the character PSes
    // (ch_ks.w == 0 = rows not captured -> the terms vanish). The key spec
    // reflects the sun about the mapped normal, gated on N.L >= 0 and the
    // shadow; the rim terms share the game's fixed rim direction built
    // from the sun and view. Spec mask = the diffuse alpha SQUARED
    // (linear-space decode, like the vehicle gloss); skin/face carry a
    // dedicated mask map in the free decal slot instead (overlay.w = 3;
    // 2 = map present but not yet decoded -> mask 0, because the DXT1
    // skin diffuse's opaque alpha would read as a full-white mask).
    if (ch_ks.w > 0.0) {
      float fb = 1.0 - saturate(dot(vn, vd));
      float kfres = pow(max(fb, 1e-6), ch_rim.w);
      float rfres = pow(max(fb, 1e-6), ch_misc.w);
      float3 rd = normalize(ch_light.xyz * float3(-1.0, 0.2, -1.0) - vd);
      lit += saturate(rfres * saturate(dot(vn, rd))) * ch_rim.rgb;
      float3 kr = ch_light.xyz - 2.0 * vndl_u * vn;
      float ks = pow(saturate(dot(vd, -kr)), ch_ks.w) * csm *
                 (vndl_u >= 0.0 || noshadow ? 1.0 : 0.0);
      float3 rr = rd - 2.0 * dot(vn, rd) * vn;
      float rs = pow(saturate(dot(vd, -rr)), ch_rs.w) * rfres;
      float smask = overlay.w > 2.5 ? decal_art.SampleBias(smp, i.uv, misc.y).r
                                    : (overlay.w > 1.5 ? 0.0 : albedo.a);
      smask *= smask;
      spec = saturate((ks * ch_ks.rgb * kfres + rs * ch_rs.rgb) * smask);
    }
    lin = dlin * lit + spec;
    // The PS multiplies the lit color by the material multiplier
    // m_params[0].y before the tone chain (1.2 on the gameplay banks;
    // without it the jeans sit ~14% darker than the emulated reference).
    // Captured into the otherwise-unused tintB.w on fams 1/2; 0 = an
    // older capture without it.
    if (ch_tintB.w > 0.25) {
      lin *= ch_tintB.w;
    }
    // defaultcharacter/cacstamp PSes end `max oC0.w, c13.x / c22.x`:
    // the entity's spawn fade (see the livingworld comment above).
    out_a = ch_misc.x;
    // character.alpha accessory (sunglass lens): coverage from the mesh's
    // "alpha" channel at the raw second texcoord, like hair (cac_alphaPS:
    // oC0.w = tf5(uv2).r * c22.x), routed to the blended sub-pass.
    if (ch_misc.z > 0.5) {
      out_a *= saturate(decal_art.Sample(smp, i.uv2).r);
    }
  }
  // Exact tone chain: sqrt(0.5 * (max(x*E/4 + 0.75, 1) - sat(1 - x*E)^2)).
  float E = max(ch_key.w, 0.01);
  return ToneOut(lin * E, out_a, false);

}
