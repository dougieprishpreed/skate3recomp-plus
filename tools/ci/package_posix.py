"""Package native Linux/macOS builds with the shared NSV installer; no game data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nsv_components  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from platform_settings import settings_for
from test_media_runtime import verify_media

# A release link to a file up to this size is replaced by a copy (terminfo, man, scripts).
LINK_COPY_LIMIT = 1 << 20


def run(*args, **kwargs):
    try:
        return subprocess.run([str(a) for a in args], check=True, capture_output=True,
                              text=True, **kwargs).stdout
    except subprocess.CalledProcessError as error:
        # Captured diagnostics must survive in the job log, especially codesign.
        print(error.stdout or '', end='', file=sys.stderr)
        print(error.stderr or '', end='', file=sys.stderr)
        raise


def notices(destination):
    destination.mkdir()
    sdk = ROOT / 'third_party/rexglue-sdk'
    for folder in (sdk, *(p for p in (sdk / 'thirdparty').iterdir() if p.is_dir())):
        for p in folder.iterdir():
            if p.is_file() and any(s in p.name.lower() for s in ('license', 'copying', 'notice')):
                shutil.copy2(p, destination / (folder.name + '-' + p.name))


def mac_dependencies(binary_dir):
    """Vendor non-system dylibs and rewrite every reference before ad-hoc signing."""
    queue = [p for p in binary_dir.iterdir() if p.is_file()]
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
    # Relocation and signing are separate phases. The main executable is signed
    # through the completed app bundle only after nested code and metadata.
    for p in sorted(seen):
        for line in run('otool', '-L', p).splitlines()[1:]:
            dep = line.strip().split(' (', 1)[0]
            if not dep.startswith(('/System/', '/usr/lib/', '@loader_path/', '@rpath/')):
                raise ValueError(f'Nonportable macOS load command: {dep}')


def mac_bundle_icd(source, app):
    # MacOS is a code-only signing location. The loader discovers manifests in
    # Resources/vulkan/icd.d; their library paths are relative to the JSON file.
    library = app / 'Contents/MacOS/libMoltenVK.dylib'
    if not library.is_file():
        raise ValueError('Missing bundled MoltenVK library')
    manifest = app / 'Contents/Resources/vulkan/icd.d/MoltenVK_icd.json'
    data = json.loads(source.read_text())
    data['ICD']['library_path'] = '../../../MacOS/libMoltenVK.dylib'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes((json.dumps(data, indent=2) + '\n').encode('utf-8'))
    return manifest


def verify_installer(resources, platform):
    installer = resources / 'installer'
    python = installer / 'python/bin/python3'
    for name in ('tools/prepare_new_san_van.py', 'tools/nsv_nav_bake', 'media/ffmpeg', 'media/ffprobe'):
        if not (installer / name).is_file():
            raise ValueError(f'Installer component missing: {name}')
    # Run only the private preparation tools. Never execute the game in CI.
    run(python, '-B', '-E', '-s', '-c', 'import numpy, ssl, ctypes, multiprocessing', cwd=resources.parent)
    for name in ('ffmpeg', 'ffprobe'):
        run(installer / 'media' / name, '-version')
    verify_media(installer / 'media/ffmpeg', installer / 'media/ffprobe')
    env = dict(os.environ, PYTHONPATH=str(installer / 'tools'))
    run(python, '-B', '-s', '-c', 'import prepare_new_san_van,build_skate2_dlc_maps,build_skate2_audio', env=env)
    if any(installer.rglob('__pycache__')) or any(installer.rglob('*.pyc')):
        raise ValueError('Installer verification modified bundled Python resources')
    return {'private_python': True, 'numpy_and_preparation_imports': True,
            'media_tools': True, 'media_pipeline_fixture': True,
            'game_launched': False, 'runtime_tested': False}


def mac_minimum(binary):
    # The app must satisfy the actual deployment targets of vendored runtimes,
    # including Python/NumPy, rather than only the application's CMake target.
    versions = [(12, 0, 0)]
    field = None
    for line in run('otool', '-l', binary).splitlines():
        parts = line.split()
        if parts[:1] == ['cmd']:
            field = {'LC_BUILD_VERSION': 'minos', 'LC_VERSION_MIN_MACOSX': 'version'}.get(parts[1])
        elif field and parts[:1] == [field]:
            values = [int(v) for v in parts[1].split('.')]
            versions.append(tuple((values + [0, 0])[:3]))
    return max(versions)


def mac_loadable(binary):
    with binary.open('rb') as stream:
        header = stream.read(16)
    byteorder = {b'\xcf\xfa\xed\xfe': 'little', b'\xfe\xed\xfa\xcf': 'big'}.get(header[:4])
    if byteorder:
        # A relocatable object (MH_OBJECT) is a resource, not
        # executable code to sign. Only MH_EXECUTE, MH_DYLIB and MH_BUNDLE load.
        return len(header) == 16 and int.from_bytes(header[12:16], byteorder) in (2, 6, 8)
    if header[:4] in (b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                      b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'):
        return bool({'EXECUTE', 'DYLIB', 'BUNDLE'} & set(run('otool', '-hv', binary).split()))
    return False


def mac_sign_app(app, info):
    main = app / 'Contents/MacOS' / info['CFBundleExecutable']
    images = [p for p in app.rglob('*') if p.is_file() and not p.is_symlink() and mac_loadable(p)]
    if main not in images:
        raise ValueError('The app main executable is not a loadable Mach-O image')
    minimum = max(mac_minimum(p) for p in images)
    info['LSMinimumSystemVersion'] = '.'.join(map(str, minimum))
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    print('Bundled runtime minimum macOS: ' + info['LSMinimumSystemVersion'], flush=True)
    # Sign nested code inside out. Signing the enclosing app handles its main
    # executable and seals resources once, after every bundle mutation is done.
    for p in sorted(images, key=lambda p: (len(p.parts), p.as_posix()), reverse=True):
        if p != main:
            run('codesign', '--force', '--sign', '-', p)
    run('codesign', '--force', '--sign', '-', app)
    run('codesign', '--verify', '--deep', '--strict', app)


def remove_links(output, interpreter):
    """Leave no symbolic links in a release folder.

    Extractors that do not restore ZIP links (Windows tools, exFAT/NTFS drives, some
    file managers) write each link as a small text file holding its target. Running
    that file fails with "Exec format error" (v0.2c on a Steam Deck: the installer's
    python3, a link to python3.13).
    """
    links = sorted(p for p in output.rglob('*') if p.is_symlink())
    for link in links:
        target = link.resolve()
        if not target.is_relative_to(output.resolve()):
            raise ValueError('Release link points outside the package: ' + str(link))
        if target.is_dir():
            raise ValueError('Release folder links a directory: ' + str(link))
    # The interpreter the installer runs takes its target's place, so the binary is
    # stored once (bin/python3 -> python3.13 becomes the real bin/python3).
    if interpreter.is_symlink():
        real = interpreter.resolve()
        interpreter.unlink()
        real.rename(interpreter)
    copied, dropped = 0, []
    for link in links:
        if not link.is_symlink():
            continue
        target = link.resolve()
        link.unlink()
        if target.is_file() and target.stat().st_size <= LINK_COPY_LIMIT:
            shutil.copy2(target, link)
            copied += 1
        else:
            # Aliases of large binaries the installer never runs (bin/python,
            # the libpython development link) would double the download.
            dropped.append(link.relative_to(output).as_posix())
    print(f'Release links: {copied} replaced by copies, dropped {dropped}', flush=True)


def archive_package(output, platform):
    archive = output.with_suffix('.zip')
    if archive.exists():
        raise FileExistsError(archive)
    if any(p.is_symlink() for p in output.rglob('*')):
        raise ValueError('Release folders must not contain links (see remove_links)')
    if platform == 'macos-arm64':
        # Preserve app-bundle metadata and executable modes.
        run('/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', output, archive)
    else:
        with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
            for p in [output, *sorted(output.rglob('*'))]:
                z.write(p, p.relative_to(output.parent).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('Package ZIP CRC verification failed')
        if any(stat.S_ISLNK(i.external_attr >> 16) for i in z.infolist()):
            raise ValueError('Package ZIP contains a link entry')
    # Test the download after a normal native extraction to a new location.
    # Never run the game; execute only its installer interpreter/media tools.
    check_root = ROOT / 'out/package-check' / output.name
    check_root.mkdir(parents=True)
    if platform == 'macos-arm64':
        run('/usr/bin/ditto', '-x', '-k', archive, check_root)
    else:
        run('unzip', '-q', archive, '-d', check_root)
    restored = check_root / output.name
    for p in output.rglob('*'):
        q = restored / p.relative_to(output)
        if p.is_file():
            expected = stat.S_IMODE(p.stat().st_mode) & 0o111
            if not q.is_file() or stat.S_IMODE(q.stat().st_mode) & 0o111 != expected:
                raise ValueError('Package ZIP did not preserve executable modes: ' + str(p))
    if platform == 'macos-arm64':
        app = restored / 'Skate 3 Recompiled+.app'
        run('codesign', '--verify', '--deep', '--strict', app)
        resources = app / 'Contents/Resources'
    else:
        dependencies = run('ldd', restored / 'skate3')
        if 'not found' in dependencies or not any(
                'rexruntime' in line and str(restored) in line for line in dependencies.splitlines()):
            raise ValueError('Extracted Linux runtime is not self-contained:\n' + dependencies)
        resources = restored
    verify_installer(resources, platform)
    print('ZIP CRC, no links, extracted modes and relocated installer checks passed.', flush=True)
    return archive


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
        version = numeric_build_version(build)
        info = {'CFBundleExecutable': 'skate3', 'CFBundleName': 'Skate 3 Recompiled+',
                'CFBundleIdentifier': 'org.skate3recomp.plus', 'CFBundlePackageType': 'APPL',
                'CFBundleShortVersionString': version, 'CFBundleVersion': version,
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
    (resources / 'settings.toml').write_bytes(settings_for('Darwin' if platform == 'macos-arm64' else 'Linux'))
    shutil.copy2(ROOT / 'packaging/README.txt', output / 'README.txt')
    shutil.copy2(ROOT / 'packaging/PLATFORM_SETUP.md', output / 'PLATFORM_SETUP.md')
    if platform != 'macos-arm64':
        # Steam / Steam Deck launcher (mchughalex/skate3recomp#191).
        launcher = output / 'steam-skate3.sh'
        launcher.write_bytes((ROOT / 'packaging/steam-skate3.sh').read_bytes().replace(b'\r\n', b'\n'))
        launcher.chmod(0o755)
    notices(resources / 'licenses')
    remove_links(output, resources / 'installer/python/bin/python3')
    if platform == 'macos-arm64':
        for name in ('libvulkan.1.dylib', 'libMoltenVK.dylib', 'MoltenVK_icd.json'):
            if not (build / name).is_file():
                raise ValueError('Missing Vulkan/MoltenVK runtime: ' + name)
        mac_bundle_icd(build / 'MoltenVK_icd.json', app)
        mac_dependencies(binaries)
        mac_sign_app(app, info)
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
        # New San Van data versions this build requires (tools/nsv_components.py); an
        # existing installation updates only the components whose markers differ.
        'nsv_components': {c.id: list(c.markers) for c in nsv_components.COMPONENTS},
        'upgrade': 'Program files update in place (in-game updater or manual extract); New San Van '
                   'components this build needs are updated at first start.'}
    if platform == 'macos-arm64':
        provenance['macos_minimum_system_version'] = info['LSMinimumSystemVersion']
    (output / 'build-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    # Explicit package contents: original inputs, generated C++ and game patches
    # can never enter through a broad build-directory copy.
    for p in output.rglob('*'):
        if p.suffix.lower() in ('.xex', '.xexp', '.iso', '.big', '.rx2', '.bh'):
            raise ValueError('Unexpected game data in release payload')
    archive = archive_package(output, platform)
    digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '  ' + archive.name + '\n')
    print(archive)


def numeric_build_version(build):
    # Keep the app metadata aligned with the CMake/Windows version on later
    # releases; the display name may have an alphabetic development suffix.
    header = (build / 'include/skate3_version.h').read_text()
    version = re.search(r'^#define SKATE3_VERSION_NUMERIC "([0-9]+\.[0-9]+\.[0-9]+)"$',
                        header, re.MULTILINE)
    if version is None:
        raise ValueError('Missing numeric build version for macOS app metadata')
    return version[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--platform', choices=('linux-amd64', 'macos-arm64'), required=True)
    a = ap.parse_args()
    package(a.build.resolve(), a.output.resolve(), a.platform)


if __name__ == '__main__':
    main()
