# v0.2 native runner builds

Development starts at the published v0.1b source `f39eca6d333c7cbaceb0a95ef4ee29237c16c36a`.
The development branch is `v0.2-dev`; its Windows preset and authored release
settings remain unchanged. The application identity is `0.2-dev`, numeric `0.2.0`.

`.github/workflows/build.yml` builds Windows x64 on `windows-2022`, Linux x64
on `ubuntu-24.04`, and macOS ARM64 on `macos-15`. These are native builds on
GitHub-hosted machines. Each job generates both guest translations, configures
again to discover the generated sources, builds, runs offline installer checks,
and creates a package. The workflow never starts the game or publishes a release.

Pushes to the trusted `v0.2-dev` branch trigger development builds. Manual
`workflow_dispatch` becomes available in the Actions UI after the workflow is
present on the default branch. The job additionally checks the repository and
trusted branch; there are no PR, fork or `pull_request_target` triggers.

## Private code-generation inputs

The separate **private** `dougieprishpreed/skate3recomp-plus-build-inputs`
repository stores `codegen-inputs.zip` and its hash manifest. The workflow pins
its immutable commit, then verifies all three expected files with the public
hash allowlist in `tools/ci/build_inputs.py`. Total uncompressed input size is
13,312,000 bytes. No full game, maps, DLC, textures or audio are required by CI.

Private-input repository: install the dedicated **read-only deploy key**; leave
write access disabled. Public application repository: put its matching private
key in the Actions repository secret **`BUILD_INPUTS_SSH_KEY`**. This key grants
read access to only that private repository and cannot push source. Never put
the key or original inputs into public commits, workflow logs, artifacts or caches.
The checkout action removes the temporary credential with `persist-credentials: false`.

To deliberately replace supported inputs, create a new private bundle locally:

```sh
python tools/ci/build_inputs.py create --game /path/to/retail/game --tu /path/to/TU3 --output out/private-bundle
```

The script refuses other retail/TU revisions and never overwrites an existing
bundle. Commit it only to the private repository, verify its contents, and
deliberately update the workflow's private-input commit pin.

## Dependency and installer parity

All jobs initialize the outer SDK without recursively initializing dependencies,
run `tools/apply_sdk_patches.py`, then initialize SDK dependencies recursively.
This order corrects the upstream unavailable ImGui gitlink. The SDK remains
`7eb0faf7787f5e01333c228b8e3f03c32f7295ea` with the checked-in 15-file patch;
ImGui remains `e0a2f6dea98f76a793a6e62a2bdb7ee9b29f5add`. No SDK fork is used.

Clang/LLVM 20 is selected explicitly. Linux/macOS installer Python and NumPy
downloads are hash-pinned in `tools/ci/runtime-lock.json`. Their FFmpeg/ffprobe
are built without GPL/nonfree additions from the already pinned LGPL FFmpeg
submodule. Windows retains CPython/NumPy staging and the pinned Chocolatey
FFmpeg distribution. Packages include the shared preparation modules, sidecars,
Recast baker and dependency notices. Tool versions and source identities are
recorded with each build. Linux targets Ubuntu 24.04/glibc 2.39; macOS packages
are ARM64 app bundles with vendored Vulkan/MoltenVK libraries and ad-hoc signing.

Linux/macOS start the same NSV wizard as Windows. GTK/Cocoa keep their normal
event loop; installation runs on the existing worker. Direct process arguments
preserve spaces, Unicode and apostrophes without invoking a shell. Packaged
resources are read-only; installed game/mod/content/settings data use the user
data root, outside the macOS app bundle. Windows portable paths are preserved.
The existing transactional preparation, V4 navigation, V5 DLC livingworld,
validation, readiness and rollback mechanisms are shared without rebake changes.

## Artifacts and acceptance

Actions uploads only explicit package archives, checksums and build metadata
from `out/packages`. It never uploads a whole build directory, raw inputs or
generated C++. Downloadable build artifacts expire after 14 days; retain the
approved packages and matching symbols before expiration or another build.
Renderer/backend work remains Vulkan/native, with the Windows D3D12 fallback
preserved. Compilation, installer imports and synthetic tests are separate from
gameplay/complete real-data installation acceptance. Follow `PLATFORM_SETUP.md`
inside experimental packages and record each tester's OS, GPU and build stamp.

Complete v0.1b Windows installs require replacing program files together, with
no setup rerun or NSV rebake for this portability work. Fresh Linux/macOS
installations run setup. Every future release must explicitly recheck and state
upgrade/rebake requirements. Existing README files remain the author's text.

References: [runner images](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[private checkouts](https://github.com/actions/checkout#checkout-multiple-repos-private),
[redistributable Python](https://github.com/astral-sh/python-build-standalone),
[LLVM 20](https://formulae.brew.sh/formula/llvm@20).
