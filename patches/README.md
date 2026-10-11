# ReXGlue changes for Skate 3 Recompiled+

The SDK submodule remains pinned to upstream commit
`7eb0faf7787f5e01333c228b8e3f03c32f7295ea`. Its local renderer, settings,
presenter and filesystem changes are recorded in `rexglue-skate3-plus.patch`.
The tested ImGui revision is `e0a2f6dea98f76a793a6e62a2bdb7ee9b29f5add`.

Initialize the outer SDK, patch it, then initialize its dependencies:

```powershell
git submodule update --init third_party/rexglue-sdk
python tools/apply_sdk_patches.py
git -C third_party/rexglue-sdk submodule update --init --recursive
```

The script checks the pinned SDK, applies the patch once, and records the pinned
ImGui revision in the SDK index before recursive initialization. The upstream
SDK references an ImGui commit unavailable from its configured remote, so do
these steps in order instead of starting with a recursive clone.
The script refuses conflicting local edits. Re-running it is safe.
CMake verifies these changes before compiling. The resulting modified SDK
submodule status is expected; the parent repository stores the patch rather
than a pointer to an unpublished SDK commit.

After a later `git submodule update --recursive`, run this script again before
building. The SDK and ImGui retain their upstream licenses.
