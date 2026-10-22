"""Check package diagnostics and native signing with synthetic code, never the game."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from ci import package_posix as package


class CommandDiagnostics(unittest.TestCase):
    def test_failed_tool_preserves_stdout_stderr_and_exit_status(self):
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics), self.assertRaises(subprocess.CalledProcessError) as error:
            package.run(sys.executable, '-c',
                        'import sys; print("tool-stdout"); print("tool-stderr",file=sys.stderr); sys.exit(7)')
        self.assertEqual(error.exception.returncode, 7)
        self.assertIn('tool-stdout', diagnostics.getvalue())
        self.assertIn('tool-stderr', diagnostics.getvalue())


class MacManifest(unittest.TestCase):
    def test_app_version_follows_generated_build_header(self):
        with tempfile.TemporaryDirectory(prefix='NSV version ') as tmp:
            build = Path(tmp)
            header = build / 'include/skate3_version.h'
            header.parent.mkdir()
            for version in ('0.2.1', '1.20.3'):
                header.write_text('#define SKATE3_VERSION_STRING "0.2a-dev"\n'
                                  f'#define SKATE3_VERSION_NUMERIC "{version}"\n')
                self.assertEqual(package.numeric_build_version(build), version)
            header.write_text('#define SKATE3_VERSION_NUMERIC "0.2a-dev"\n')
            with self.assertRaisesRegex(ValueError, 'Missing numeric build version'):
                package.numeric_build_version(build)

    def test_bundled_manifest_is_relocatable_and_preserves_driver_metadata(self):
        with tempfile.TemporaryDirectory(prefix="NSV manifest user's ") as tmp:
            root = Path(tmp)
            source = root / 'build-icd.json'
            data = {'file_format_version': '1.0.0', 'ICD': {
                'library_path': './libMoltenVK.dylib', 'api_version': '1.4.0',
                'is_portability_driver': True}}
            original = json.dumps(data).encode('utf-8')
            source.write_bytes(original)
            app = root / 'Skate 3 Recompiled+.app'
            with self.assertRaisesRegex(ValueError, 'Missing bundled MoltenVK'):
                package.mac_bundle_icd(source, app)
            library = app / 'Contents/MacOS/libMoltenVK.dylib'
            library.parent.mkdir(parents=True)
            library.write_bytes(b'fixture')
            manifest = package.mac_bundle_icd(source, app)
            self.assertEqual(manifest.parent, app / 'Contents/Resources/vulkan/icd.d')
            bundled = json.loads(manifest.read_text())
            relative = Path(bundled['ICD']['library_path'])
            self.assertFalse(relative.is_absolute())
            self.assertEqual((manifest.parent / relative).resolve(), library.resolve())
            data['ICD']['library_path'] = relative.as_posix()
            self.assertEqual(bundled, data)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse((library.parent / 'MoltenVK_icd.json').exists())


@unittest.skipUnless(sys.platform == 'darwin', 'Requires native macOS signing tools')
class MacSigning(unittest.TestCase):
    def test_relocated_app_nested_code_object_resource_and_zip_signature(self):
        with tempfile.TemporaryDirectory(prefix="NSV package user's ") as tmp:
            root = Path(tmp)
            app = root / 'Synthetic NSV.app'
            binaries, resources = app / 'Contents/MacOS', app / 'Contents/Resources'
            binaries.mkdir(parents=True)
            resources.mkdir()
            source = root / 'helper.c'
            source.write_text('int fixture_value(void) { return 17; }\n')
            compiler = os.environ.get('CC', 'clang')
            library = binaries / 'libfixture.dylib'
            package.run(compiler, '-mmacosx-version-min=12.0', '-dynamiclib', source,
                        '-Wl,-install_name,' + str(library), '-o', library)
            shutil.copy2(library, binaries / 'libMoltenVK.dylib')
            main_source = root / 'main.c'
            main_source.write_text('int fixture_value(void); int main(void) { return fixture_value(); }\n')
            main = binaries / 'packaging-fixture'
            package.run(compiler, '-mmacosx-version-min=12.0', main_source, library, '-o', main)
            private = resources / 'installer/tools'
            private.mkdir(parents=True)
            nested = private / 'synthetic-helper'
            shutil.copy2(main, nested)
            # Also cover object resources if a Python runtime ships Mach-O objects.
            obj = private / 'python.o'
            package.run(compiler, '-c', source, '-o', obj)
            original_object = obj.read_bytes()
            self.assertTrue(package.mac_loadable(main))
            self.assertTrue(package.mac_loadable(library))
            self.assertFalse(package.mac_loadable(obj))
            resource = resources / 'fixture.txt'
            resource.write_text('NSV packaging fixture\n')
            info = {'CFBundleExecutable': main.name, 'CFBundleIdentifier': 'org.skate3recomp.package-fixture',
                    'CFBundlePackageType': 'APPL', 'CFBundleVersion': '0.2.0'}
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
            identity = package.run('dwarfdump', '--uuid', main).split()[1]
            package.mac_dependencies(binaries)
            dependencies = package.run('otool', '-L', main).splitlines()[1:]
            self.assertTrue(any('@loader_path/libfixture.dylib' in line for line in dependencies))
            # Reproduce log211's exact resource-in-code-directory failure first.
            bad_manifest = binaries / 'MoltenVK_icd.json'
            bad_manifest.write_text(json.dumps({'file_format_version': '1.0.0', 'ICD': {
                'library_path': './libMoltenVK.dylib', 'api_version': '1.4.0',
                'is_portability_driver': True}}))
            with redirect_stderr(io.StringIO()), self.assertRaises(subprocess.CalledProcessError) as error:
                package.mac_sign_app(app, info)
            self.assertIn('MoltenVK_icd.json', error.exception.stderr)
            manifest = package.mac_bundle_icd(bad_manifest, app)
            bad_manifest.unlink()
            package.mac_sign_app(app, info)
            self.assertEqual(info['LSMinimumSystemVersion'], '12.0.0')
            self.assertEqual(obj.read_bytes(), original_object)
            self.assertEqual(package.run('dwarfdump', '--uuid', main).split()[1], identity)
            archive = root / 'fixture.zip'
            package.run('/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, archive)
            extracted = root / 'extracted'
            extracted.mkdir()
            package.run('/usr/bin/ditto', '-x', '-k', archive, extracted)
            package.run('codesign', '--verify', '--deep', '--strict', extracted / app.name)
            restored_manifest = extracted / app.name / manifest.relative_to(app)
            relative = json.loads(restored_manifest.read_text())['ICD']['library_path']
            self.assertEqual((restored_manifest.parent / relative).resolve(),
                             (extracted / app.name / 'Contents/MacOS/libMoltenVK.dylib').resolve())
            self.assertTrue((restored_manifest.parent / relative).is_file())
            # Confirm the verifier actually catches a resource changed after sealing.
            (extracted / app.name / 'Contents/Resources/fixture.txt').write_text('changed\n')
            with redirect_stderr(io.StringIO()), self.assertRaises(subprocess.CalledProcessError):
                package.run('codesign', '--verify', '--deep', '--strict', extracted / app.name)


if __name__ == '__main__':
    unittest.main()
