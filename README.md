# Welcome to New San Vanelona.

Visit Skate 2's iconic city from the in-game locations menu of Skate 3.

## Complete with:

- 25 spawn locations
- distant backdrop assets, skybox, clouds, water shader
- animated vegetation, fountains, water surfaces, flags
- emissive lights, neon signs
- storefront glass, reflective skyscrapers
- transparent objects with correct properties, like the sails in Waterfront
- jumbotron video and indoor lighting in S.V. Stadium
- ambient audio around city, audio emitters like waves and birds
- original cars with audio, traffic patterns
- original peds with speech
- all DLC maps
- night mode, Skate 3 skybox mode

**Vulkan with native renderer is the default. Only tested in Windows.**

Many native renderer issues have also been resolved. These fixes affect all Skate 3 maps as well:

- fixed Hall of Meat bones
- ported motion blur and vignette shaders
- hair now looks normal in shadows
- enhanced shadows no longer briefly drop out
- feet no longer detach during forward man
- tattoos, dirt on clothes, scrapes on knees are all visible now
- enabled traffic lights in Skate 3 and New San Van
- original static effect on returning to placed marker
- no visible seams on player when returning to placed marker

Open the imgui interface with **RB+Start** or **ESC** and look in the **World** menu for:

- New San Van night mode
- unlocked ragdoll
- disable fall damage
- disable air timer
- disable Hall of Meat timer
- walk out of bounds
- no black screen out of bounds
- disable screen shake on high speed

## Known limitations

Emulated renderer is broken in New San Van, linux and macOS builds not yet tested.

Skater portraits in menus are off by default. With them on
(`skate3_native_render_scene_menu_rtt_passes = true` in settings.toml), Edit
Skater can freeze on its loading screen and then close the game for a skater
whose portrait still has to be generated. The cause is a wider problem still to
be fixed: when emulated rendering gets heavy, the picture can stop updating for
several seconds while the game keeps running, which is also why the emulated
renderer runs so poorly.

## Credits

Built on [Skate 3 Recomp](https://github.com/mchughalex/skate3recomp) and its
[rexglue SDK](https://github.com/mchughalex/rexglue-skate3) by
[mchughalex](https://github.com/mchughalex).

Fixes from community pull requests to those projects:

- [drewsipher](https://github.com/drewsipher): Linux and Steam Deck fixes
  ([rexglue-skate3#22](https://github.com/mchughalex/rexglue-skate3/pull/22)):
  picture freezing while the game keeps running, controllers not detected, a
  boot stall on the first frame, the post-event photo screen stalling, music
  skipping with a large audio buffer, an idle audio thread using a full CPU
  core, crashes leaking memory into `/dev/shm`, the system RenderDoc library
  loading into every run; and the Steam launcher
  ([skate3recomp#191](https://github.com/mchughalex/skate3recomp/pull/191))
- [spekulazarus](https://github.com/spekulazarus): macOS/Linux black screen at
  boot or after a resize during loading
  ([rexglue-skate3#7](https://github.com/mchughalex/rexglue-skate3/pull/7))
- [mrmos](https://github.com/mrmos): no startup abort when the log folder is
  read-only ([rexglue-skate3#3](https://github.com/mchughalex/rexglue-skate3/pull/3))
- [schillers14-lab](https://github.com/schillers14-lab): no seams on the player
  when returning to a placed marker
  ([skate3recomp#173](https://github.com/mchughalex/skate3recomp/pull/173),
  [rexglue-skate3#9](https://github.com/mchughalex/rexglue-skate3/pull/9))
- [andrewnakas](https://github.com/andrewnakas): map-load crash on Linux
  ([skate3recomp#131](https://github.com/mchughalex/skate3recomp/pull/131)),
  untextured surfaces on custom maps
  ([skate3recomp#130](https://github.com/mchughalex/skate3recomp/pull/130))
- [zeezme](https://github.com/zeezme): shirt vanishing or lagging while its cloth
  sim runs ([skate3recomp#166](https://github.com/mchughalex/skate3recomp/pull/166))
- [elespink](https://github.com/elespink): free-fly camera on Linux and macOS
  ([skate3recomp#148](https://github.com/mchughalex/skate3recomp/pull/148))
