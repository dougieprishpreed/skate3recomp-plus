"""Apply the SDK changes used by Skate 3 Recompiled+ without replacing local work.

Run after initializing the outer SDK submodule, before recursive initialization.
The parent repository pins
the unmodified SDK; this patch and the ImGui revision reproduce the tested build.
"""
import argparse
from pathlib import Path
import subprocess

SDK_REVISION = '7eb0faf7787f5e01333c228b8e3f03c32f7295ea'
IMGUI_REVISION = 'e0a2f6dea98f76a793a6e62a2bdb7ee9b29f5add'
UPSTREAM_IMGUI_REVISION = 'cdda62349d6068e906c2e0ee340d6ec31eedbb9c'
ROOT = Path(__file__).resolve().parent.parent


def git(directory, *args, check=True):
    return subprocess.run(['git', '-C', str(directory), *args], check=check,
                          capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, default=ROOT / 'third_party/rexglue-sdk')
    parser.add_argument('--check', action='store_true', help='verify without changing files')
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    patch = ROOT / 'patches/rexglue-skate3-plus.patch'
    if not (sdk / 'CMakeLists.txt').is_file():
        raise SystemExit('Run: git submodule update --init third_party/rexglue-sdk')
    if git(sdk, 'rev-parse', 'HEAD').stdout.strip() != SDK_REVISION:
        raise SystemExit(f'SDK revision differs from the tested {SDK_REVISION}; no files changed.')
    applied = git(sdk, 'apply', '--reverse', '--check', str(patch), check=False).returncode == 0
    if not applied:
        if args.check:
            raise SystemExit('SDK patch missing. Run: python tools/apply_sdk_patches.py')
        ready = git(sdk, 'apply', '--check', str(patch), check=False)
        if ready.returncode:
            raise SystemExit('SDK patch conflicts with local edits; no files changed.\n' + ready.stderr)

    imgui = sdk / 'thirdparty/imgui'
    indexed = git(sdk, 'ls-files', '--stage', 'thirdparty/imgui').stdout.split()[1]
    initialized = (imgui / '.git').exists()
    current = git(imgui, 'rev-parse', 'HEAD').stdout.strip() if initialized else None
    if indexed not in (UPSTREAM_IMGUI_REVISION, IMGUI_REVISION):
        raise SystemExit('ImGui has a different staged revision; no files changed.')
    if args.check and (indexed != IMGUI_REVISION or current != IMGUI_REVISION):
        raise SystemExit('Initialize the patched SDK dependencies as described in BUILDING.md.')
    if initialized and current != IMGUI_REVISION and git(imgui, 'status', '--porcelain').stdout.strip():
        raise SystemExit('ImGui has local edits; no files changed.')
    if not args.check:
        # The upstream SDK's old ImGui commit is unavailable from ocornut/imgui.
        # Submodule initialization uses this index entry, so pin it before recursing.
        git(sdk, 'update-index', '--cacheinfo', f'160000,{IMGUI_REVISION},thirdparty/imgui')
    if initialized and current != IMGUI_REVISION:
        if git(imgui, 'cat-file', '-e', IMGUI_REVISION + '^{commit}', check=False).returncode:
            git(imgui, 'fetch', 'origin', IMGUI_REVISION)
        git(imgui, 'checkout', '--detach', IMGUI_REVISION)
    if not applied:
        git(sdk, 'apply', str(patch))
    print('Skate 3 Recompiled+ SDK patch and ImGui revision verified.' if initialized else
          'SDK patched. Now run: git -C third_party/rexglue-sdk submodule update --init --recursive')


if __name__ == '__main__':
    main()
