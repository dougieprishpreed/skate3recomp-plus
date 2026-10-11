# Welcome to New San Van.

Visit the city from the in-game locations menu of Skate 3; it's at the bottom of the list.

## Complete with:

- 25 spawn locations
- distant backdrop assets, skybox, clouds, water shader
- animated vegetation, fountains, water surfaces, flags
- emissive lights, neon signs, storefront glass, reflective skyscrapers
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
- smoothing of enhanced shadow gradients
- feet no longer detach during forward man
- tattoos, dirt on clothes, scrapes on knees are all visible now
- enabled traffic lights in Skate 3 and New San Van
- original static effect on teleporting to placed marker

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

Emulated renderer is broken, D3D12 is not working, linux and macOS builds not yet tested. Future builds may remove the option to enable emulated renderer or d3d12 except via settings.toml