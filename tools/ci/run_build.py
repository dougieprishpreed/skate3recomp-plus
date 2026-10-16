"""One native runner build, generation, installer checks and package export."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def run(*args):
    print('+', ' '.join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--platform', required=True, choices=('win-amd64', 'linux-amd64', 'macos-arm64'))
    a = ap.parse_args()
    windows = a.platform == 'win-amd64'
    preset = {'win-amd64': 'relwithdebinfo', 'linux-amd64': 'linux-relwithdebinfo',
              'macos-arm64': 'macos-relwithdebinfo'}[a.platform]
    inputs = ROOT / 'out/ci-inputs'
    if not inputs.is_dir():
        raise SystemExit('Verify and extract the private input bundle first')
    configure = ['cmake', '--preset', preset,
                 '-DSKATE3_GAME_DATA_ROOT=' + str(inputs / 'game'),
                 '-DSKATE3_TITLE_UPDATE_PACKAGE=' + str(inputs / 'title-update/TU3'),
                 '-DREXGLUE_ENABLE_PERF_COUNTERS=OFF', '-DSKATE3_ENABLE_X86_64_V3=OFF']
    if windows:
        configure += ['-DCMAKE_C_COMPILER=clang', '-DCMAKE_CXX_COMPILER=clang++']
        # Chocolatey's PATH entry may be a shim bound to the runner's C: drive.
        # Package the actual redistributable binaries, never that shim.
        media = Path(os.environ.get('ChocolateyInstall', 'C:/ProgramData/chocolatey')) / 'lib/ffmpeg/tools'
        ffmpeg = next((str(p) for p in media.rglob('ffmpeg.exe')
                       if p.with_name('ffprobe.exe').is_file()), None)
        if not ffmpeg:
            raise SystemExit('Install the pinned FFmpeg tools first')
    else:
        runtime = ROOT / 'out/installer-runtime/python'
        configure += ['-DSKATE3_INSTALLER_PYTHON_ROOT=' + str(runtime)]
        ffmpeg = str(ROOT / 'out/media/bin/ffmpeg')
        if a.platform == 'macos-arm64':
            llvm = subprocess.check_output(['brew', '--prefix', 'llvm@20'], text=True).strip()
            configure += [f'-DCMAKE_C_COMPILER={llvm}/bin/clang',
                          f'-DCMAKE_CXX_COMPILER={llvm}/bin/clang++',
                          f'-DCMAKE_AR={llvm}/bin/llvm-ar', f'-DCMAKE_RANLIB={llvm}/bin/llvm-ranlib',
                          '-DCMAKE_OSX_ARCHITECTURES=arm64']
    configure += ['-DSKATE3_INSTALLER_FFMPEG=' + ffmpeg]
    run(*configure)
    parallel = '1' if a.platform == 'macos-arm64' else '2'
    # Generated source discovery happens on the configure AFTER generation.
    run('cmake', '--build', '--preset', preset, '--target', 'generate-all', '--parallel', parallel)
    run(*configure)
    run('cmake', '--build', '--preset', preset, '--parallel', parallel)
    build = ROOT / 'out/build' / preset
    run(sys.executable, 'tools/test_nsv_install.py')
    run(sys.executable, 'tools/test_nsv_navigation.py', '--baker',
        build / ('nsv_nav_bake.exe' if windows else 'nsv_nav_bake'))
    run(sys.executable, 'tools/test_platform_installer.py')
    run(sys.executable, 'tools/test_platform_settings.py')
    output = ROOT / 'out/packages' / ('Skate3Recomp-Plus-' + a.platform)
    if windows:
        run(sys.executable, 'tools/package_release.py', '--build', build, '--output', output)
        archive = output.with_suffix('.zip')
        with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as z:
            for p in output.rglob('*'):
                if p.is_file():
                    z.write(p, output.name + '/' + p.relative_to(output).as_posix())
        digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
        archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n')
    else:
        run(sys.executable, 'tools/ci/package_posix.py', '--build', build,
            '--output', output, '--platform', a.platform)
    provenance = {
        'platform': a.platform,
        'source': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'runner_image': os.environ.get('ImageOS'), 'runner_image_version': os.environ.get('ImageVersion'),
        'compiler': subprocess.check_output(['clang++', '--version'], text=True).splitlines()[0],
        'build_stamp': (build / 'include/skate3_version.h').read_text(),
        'game_launched': False, 'gameplay_validation': 'awaiting platform testers'}
    (ROOT / 'out/packages' / (a.platform + '-build.json')).write_text(json.dumps(provenance, indent=2) + '\n')
    symbols = ROOT / 'out/symbols'
    symbols.mkdir()
    # Keep a matching executable/runtime pair separate from the normal download.
    # ELF carries its DWARF here; Windows additionally needs both matching PDBs.
    members = [build / ('skate3.exe' if windows else 'skate3')]
    members += list(build.glob('*.dll' if windows else '*.dylib' if a.platform == 'macos-arm64' else '*.so*'))
    if windows:
        members += [build / 'skate3.pdb']
        members += list((ROOT / 'third_party/rexglue-sdk/out/win-amd64').glob('rexruntime*.pdb'))
    if any(not p.is_file() for p in members):
        raise ValueError('Matching symbol input missing')
    with zipfile.ZipFile(symbols / (a.platform + '-matching-symbols.zip'), 'x', zipfile.ZIP_DEFLATED) as z:
        manifest = {}
        for p in members:
            name = p.relative_to(ROOT).as_posix()
            z.write(p, name)
            manifest[name] = hashlib.file_digest(p.open('rb'), 'sha256').hexdigest()
        z.writestr('manifest.json', json.dumps({'build': provenance, 'sha256': manifest}, indent=2))


if __name__ == '__main__':
    main()
