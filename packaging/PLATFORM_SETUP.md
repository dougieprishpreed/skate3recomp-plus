# Experimental v0.2 desktop packages

Linux x64 targets Ubuntu 24.04 or newer (glibc 2.39), with a working Vulkan driver.
Install the desktop runtime dependencies before starting:

```sh
sudo apt install libgtk-3-0t64 libx11-xcb1 libvulkan1 libasound2t64 libpulse0 libpipewire-0.3-0 libudev1 libusb-1.0-0
```

Extract the ZIP with a tool that preserves Unix permissions and symbolic links
(for example, `unzip`), then run `./skate3`.
The default writable data location is `$XDG_DATA_HOME/skate3` or
`~/.local/share/skate3`. Explicit user data overrides are respected.

Steam and Steam Deck: run `./skate3` once in Desktop Mode and complete setup.
Then choose Add a Non-Steam Game in Steam and add `steam-skate3.sh` from the same
folder, and launch the game from Steam in either mode. The launcher uses the
system libraries instead of Steam's runtime, blocks idle sleep while the game
runs, and closes a copy of the game that was left running before it starts.

macOS targets Apple Silicon. Extract the ZIP with Archive Utility and open
`Skate 3 Recompiled+.app`.
Vulkan uses the bundled MoltenVK runtime. The experimental app is ad-hoc signed,
not Developer ID signed or notarized. macOS may require the user to approve opening
it using Privacy & Security. Keep installed game data outside the app bundle.
The current default is `~/.local/share/skate3`; explicit user data overrides apply.

Both platforms use the same setup screen as Windows: select your Skate 3 and
Skate 2 Xbox 360 ISOs or extracted disc folders, verified TU3, optional Skate 3
DLC and the required Skate 2 DLC. Choose a sky, then install. Python, NumPy,
FFmpeg, ffprobe and the navigation baker are included. Setup logs are in
`install-state/new-san-van.log` under the writable data folder. Original inputs
are read-only; installed content is prepared with the existing rollback flow.

Replacing program files in a complete v0.1b Windows installation does not require
rerunning setup or rebaking NSV. Fresh Linux/macOS installations use setup.
Do not copy Windows settings over platform defaults. Preserve game, mods, content,
profiles and saves when updating. The included README remains the author's text;
these experimental platform instructions supplement it.

Compilation and package checks do not establish gameplay correctness. Platform
testers must check setup, rendering, audio, controllers, map changes and saves.
