# Windows build instructions

These instructions build Skate 3 Recompiled+ v0.1a from source on Windows.
Open an **x64 Native Tools Command Prompt for Visual Studio 2022**, then run
`powershell` to use the commands below in that environment. Install Visual Studio's
Desktop development with C++ workload and Windows SDK, LLVM/Clang 18 or newer,
CMake 3.25 or newer, Ninja, 64-bit Python 3.11 or newer with NumPy, and a Windows
FFmpeg distribution containing both `ffmpeg.exe` and `ffprobe.exe`. Put Git,
Clang, CMake, Ninja and Python on PATH. Building the committed shaders does not
require DXC. Use a short, writable working directory for the clone, such as
`C:\src`, to avoid Windows build path limits.

Provide your own extracted Xbox 360 Skate 3 disc, including `default.xex` and
`data\webkit\EAWebkit.xex`, and the Skate 3 Title Update 3 package
`TU_12K2276_000000C000000.00000000000O3`. Game data and generated recompilation
sources are excluded from the repository. Skate 2 and its DLC are selected
later in the setup screen; they are not required to compile the executable.

```powershell
git clone https://github.com/dougieprishpreed/skate3recomp-plus.git
cd skate3recomp-plus
python -m pip install numpy
git submodule update --init third_party/rexglue-sdk
python tools/apply_sdk_patches.py
git -C third_party/rexglue-sdk submodule update --init --recursive

# Replace these three paths with your local inputs.
$gameDump = 'D:/Games/Skate3-disc'
$titleUpdate = 'D:/Games/TU_12K2276_000000C000000.00000000000O3'
$ffmpeg = 'D:/Tools/ffmpeg/bin/ffmpeg.exe'

cmake --preset relwithdebinfo -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ "-DSKATE3_GAME_DATA_ROOT=$gameDump" "-DSKATE3_TITLE_UPDATE_PACKAGE=$titleUpdate" "-DSKATE3_INSTALLER_FFMPEG=$ffmpeg"
cmake --build --preset relwithdebinfo --target generate-all -- -j2
# Reconfigure after code generation so CMake sees the generated source lists.
cmake --preset relwithdebinfo
cmake --build --preset relwithdebinfo -- -j2

# Create a clean portable release with the checked-in release defaults.
python tools/package_release.py --build out/build/relwithdebinfo --output out/release/Skate3Recomp-Plus-Windows
```

Stop if any command fails. The first build compiles the SDK and generates the
recompiled game code, so it takes substantially longer than later builds.
RelWithDebInfo is the optimized configuration used for this Windows release;
the portable package omits debug symbols and build intermediates.

Keep the entire `Skate3Recomp-Plus-Windows` folder together. Run `skate3.exe`
there and use setup to select your Skate 3 and Skate 2 discs, TU3, and Skate 2
DLC. The private Python/NumPy runtime, FFmpeg, Recast navigation baker and all
preparation tools are staged automatically during the build. Setup generates
New San Van and its DLC navigation from your supplied game files. The release
starts with Vulkan/native rendering, Skate 2 sky, World gameplay options off
and ragdoll strength 1.00; setup offers the sky choice.

Follow the submodule steps in the order shown: the upstream SDK references an
unavailable ImGui commit, and the patch helper pins the working revision before
recursive initialization. After a later recursive submodule update, run the
patch script again. See [patches/README.md](patches/README.md) for the pinned SDK
and ImGui revisions.

## Build with an A.I. agent

Copy this prompt into your coding agent. It will need access to your Windows
computer and the paths to your extracted Skate 3 disc, TU3 package and FFmpeg.

```text
Build and package Skate 3 Recompiled+ v0.1a on this Windows computer from
https://github.com/dougieprishpreed/skate3recomp-plus.

Read BUILDING.md and follow its Windows instructions. Check that Git, the
Visual Studio C++ build tools and Windows SDK, Clang 18+, CMake 3.25+, Ninja,
and 64-bit Python 3.11+ with NumPy are available. Ask me for missing tools or
input paths: my extracted Skate 3 disc containing default.xex and
data/webkit/EAWebkit.xex, the TU3 package, and ffmpeg.exe with ffprobe.exe
beside it. Treat my disc and DLC source files as read-only.

Clone without --recursive. Initialize only third_party/rexglue-sdk, run
python tools/apply_sdk_patches.py, then initialize the SDK's submodules
recursively. Configure the relwithdebinfo preset with my game, TU3 and FFmpeg
paths and the Clang compilers. Build generate-all with -j2, reconfigure, and
build with -j2. Resolve build failures using the project's documented setup.

Run tools/package_release.py with --build out/build/relwithdebinfo and a new
--output folder named Skate3Recomp-Plus-Windows, using the checked-in release
defaults. Keep its executable, runtime DLL, installer, licenses, settings,
portable.txt and README.txt together. Zip the entire named folder, verify the
archive, and tell me where the folder and ZIP are. Do not start the game;
I will run the completed build myself.
```

## A.I. Usage Notice

This mod was created with help from A.I. If you have a problem with that,
kindly exercise the revolutionary, entirely human-powered option of not
downloading or playing it. Thank you.
