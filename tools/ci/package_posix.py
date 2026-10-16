"""Package native Linux/macOS builds with the shared NSV installer; no game data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True,
                          text=True, **kwargs).stdout


def notices(destination):
    destination.mkdir()
    sdk = ROOT / 'third_party/rexglue-sdk'
    for folder in (sdk, *(p for p in (sdk / 'thirdparty').iterdir() if p.is_dir())):
        for p in folder.iterdir():
            if p.is_file() and any(s in p.name.lower() for s in ('license', 'copying', 'notice')):
                shutil.copy2(p, destination / (folder.name + '-' + p.name))


def mac_dependencies(binary_dir):
    """Vendor non-system dylibs and rewrite every reference before ad-hoc signing."""
    queue = [p for p in binary_dir.iterdir() if p.is_file() and p.name != 'MoltenVK_icd.json']
    seen = set()
    while queue:
        binary = queue.pop()
        if binary in seen:
            continue
        seen.add(binary)
        lines = run('otool', '-L', binary).splitlines()[1:]
        for line in lines:
            dependency = line.strip().split(' (', 1)[0]
            if dependency.startswith(('/System/', '/usr/lib/')):
                continue
            name = Path(dependency).name
            destination = binary_dir / name
            if not destination.exists():
                candidates = [Path(dependency), Path('/opt/homebrew/lib') / name]
                if dependency.startswith('@loader_path/'):
                    candidates.insert(0, binary.parent / dependency.removeprefix('@loader_path/'))
                candidates.extend(Path('/opt/homebrew/opt').glob(f'*/lib/{name}'))
                source = next((p for p in candidates if p.is_file()), None)
                if source is None:
                    raise ValueError(f'Unresolved macOS dependency: {dependency}')
                shutil.copy2(source, destination)
                queue.append(destination)
            if destination != binary:
                run('install_name_tool', '-change', dependency, '@loader_path/' + name, binary)
        if binary.suffix == '.dylib':
            run('install_name_tool', '-id', '@rpath/' + binary.name, binary)
    # Sign after all load commands have been rewritten. Python's standalone tree
    # keeps its relocatable paths and is separately verified from the final location.
    for p in seen:
        run('codesign', '--force', '--sign', '-', p)
    for p in seen:
        for line in run('otool', '-L', p).splitlines()[1:]:
            dep = line.strip().split(' (', 1)[0]
            if not dep.startswith(('/System/', '/usr/lib/', '@loader_path/', '@rpath/')):
                raise ValueError(f'Nonportable macOS load command: {dep}')


def verify_installer(resources, platform):
    installer = resources / 'installer'
    python = installer / 'python/bin/python3'
    for name in ('tools/prepare_new_san_van.py', 'tools/nsv_nav_bake', 'media/ffmpeg', 'media/ffprobe'):
        if not (installer / name).is_file():
            raise ValueError(f'Installer component missing: {name}')
    # Run only the private preparation tools. Never execute the game in CI.
    run(python, '-E', '-s', '-c', 'import numpy, ssl, ctypes, multiprocessing', cwd=resources.parent)
    for name in ('ffmpeg', 'ffprobe'):
        run(installer / 'media' / name, '-version')
    env = dict(os.environ, PYTHONPATH=str(installer / 'tools'))
    run(python, '-s', '-c', 'import prepare_new_san_van,build_skate2_dlc_maps,build_skate2_audio', env=env)
    return {'private_python': True, 'numpy_and_preparation_imports': True,
            'media_tools': True, 'game_launched': False, 'runtime_tested': False}


def package(build, output, platform):
    if output.exists():
        raise ValueError('Choose a new package folder; existing releases are preserved')
    if not (build / 'skate3').is_file() or not (build / 'installer').is_dir():
        raise ValueError('Build and stage the complete installer first')
    output.mkdir(parents=True)
    if platform == 'macos-arm64':
        app = output / 'Skate 3 Recompiled+.app'
        binaries, resources = app / 'Contents/MacOS', app / 'Contents/Resources'
        binaries.mkdir(parents=True)
        resources.mkdir()
        info = {'CFBundleExecutable': 'skate3', 'CFBundleName': 'Skate 3 Recompiled+',
                'CFBundleIdentifier': 'org.skate3recomp.plus', 'CFBundlePackageType': 'APPL',
                'CFBundleShortVersionString': '0.2.0', 'CFBundleVersion': '0.2.0',
                'LSMinimumSystemVersion': '12.0', 'NSHighResolutionCapable': True}
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    else:
        binaries = resources = output
    shutil.copy2(build / 'skate3', binaries / 'skate3')
    pattern = '*.dylib' if platform == 'macos-arm64' else '*.so*'
    libraries = list(build.glob(pattern))
    if not any('rexruntime' in p.name for p in libraries):
        raise ValueError('Runtime shared library missing')
    for p in libraries:
        shutil.copy2(p, binaries / p.name)
    shutil.copytree(build / 'installer', resources / 'installer', symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(ROOT / 'config/release-defaults.toml', resources / 'settings.toml')
    shutil.copy2(ROOT / 'packaging/README.txt', output / 'README.txt')
    shutil.copy2(ROOT / 'packaging/PLATFORM_SETUP.md', output / 'PLATFORM_SETUP.md')
    notices(resources / 'licenses')
    if platform == 'macos-arm64':
        for name in ('libvulkan.1.dylib', 'libMoltenVK.dylib', 'MoltenVK_icd.json'):
            if not (build / name).is_file():
                raise ValueError('Missing Vulkan/MoltenVK runtime: ' + name)
        shutil.copy2(build / 'MoltenVK_icd.json', binaries / 'MoltenVK_icd.json')
        mac_dependencies(binaries)
        # Sign the entire portable interpreter (extensions and media/baker too),
        # then the outer app. Ad-hoc signing is not notarization.
        for p in resources.rglob('*'):
            if not p.is_file() or p.is_symlink():
                continue
            magic = p.read_bytes()[:4]
            if magic in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
                run('codesign', '--force', '--sign', '-', p)
        run('codesign', '--force', '--sign', '-', app)
        run('codesign', '--verify', '--deep', '--strict', app)
    else:
        dependencies = run('ldd', binaries / 'skate3')
        if 'not found' in dependencies:
            raise ValueError('Unresolved Linux runtime dependency:\n' + dependencies)
        (output / 'linux-runtime-dependencies.txt').write_text(dependencies)
    checks = verify_installer(resources, platform)
    provenance = {
        'source': run('git', 'rev-parse', 'HEAD', cwd=ROOT).strip(),
        'sdk': run('git', '-C', ROOT / 'third_party/rexglue-sdk', 'rev-parse', 'HEAD').strip(),
        'sdk_patch_sha256': hashlib.sha256((ROOT / 'patches/rexglue-skate3-plus.patch').read_bytes()).hexdigest(),
        'imgui': run('git', '-C', ROOT / 'third_party/rexglue-sdk/thirdparty/imgui', 'rev-parse', 'HEAD').strip(),
        'platform': platform, 'checks': checks,
        'installer_policy': {'navigation': 4, 'dlc_livingworld': 5},
        'upgrade': 'Existing complete v0.1b Windows installs need program files only; no NSV rebake. '
                   'Fresh Linux/macOS installs run the shared setup wizard.'}
    (output / 'build-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    # Explicit package contents: original inputs, generated C++ and game patches
    # can never enter through a broad build-directory copy.
    for p in output.rglob('*'):
        if p.suffix.lower() in ('.xex', '.xexp', '.iso', '.big', '.rx2', '.bh'):
            raise ValueError('Unexpected game data in release payload')
    archive = output.with_suffix('.tar.gz')
    with tarfile.open(archive, 'x:gz') as t:
        t.add(output, arcname=output.name, filter=lambda info: None if '__pycache__' in info.name else info)
    digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '  ' + archive.name + '\n')
    print(archive)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--platform', choices=('linux-amd64', 'macos-arm64'), required=True)
    a = ap.parse_args()
    package(a.build.resolve(), a.output.resolve(), a.platform)


if __name__ == '__main__':
    main()
