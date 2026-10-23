"""Offline portability checks. Uses synthetic inputs; never launches the game."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import prepare_new_san_van as install

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('build_inputs', ROOT / 'tools/ci/build_inputs.py')
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


class PortableInputs(unittest.TestCase):
    def test_preparation_children_preserve_read_only_resources(self):
        with tempfile.TemporaryDirectory(prefix="NSV signed user's ") as tmp:
            root = Path(tmp)
            (root / 'payload.py').write_text('value = 17\n')
            (root / 'child.py').write_text(
                'import payload,sys\nfrom pathlib import Path\n'
                'assert payload.value == 17\nPath(sys.argv[1]).write_text("prepared")\n')
            result = root / 'result.txt'
            with patch.object(install, 'HERE', root), patch.object(sys, 'dont_write_bytecode', True):
                install.run('child', result)
            self.assertEqual(result.read_text(), 'prepared')
            self.assertFalse((root / '__pycache__').exists())

    def test_extra_and_traversal_entries_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'invalid.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../escaped', b'x')
            with self.assertRaisesRegex(ValueError, 'Unexpected entries'):
                inputs.extract(archive, root / 'out')
            self.assertFalse((root / 'out').exists())
            self.assertFalse((root.parent / 'escaped').exists())

    def test_damaged_bytes_rejected(self):
        with self.assertRaisesRegex(ValueError, 'damaged'):
            inputs.verify(b'wrong', 'game/default.xex')

    def test_custom_unicode_roots_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="NSV user's ") as tmp:
            base = Path(tmp).resolve()
            root, content, stage = base / 'city-é', base / 'content-other', base / 'stage'
            for owner in (root, stage):
                for name in (*install.FOLDERS, 'nsv_spawns.txt', 'install-manifest.json'):
                    p = owner / 'mods/skate2' / name
                    if '.' not in name:
                        p = p / 'asset'
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b'old' if owner == root else b'new')
            for owner in (content, stage):
                for branch in ('00000002', 'Headers/00000002'):
                    p = owner / install.CONTENT / branch / 'test'
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b'old' if owner == content else b'new')
            original = install.shutil.move
            counter = 0

            def fail_after_changes(*a, **kw):
                nonlocal counter
                counter += 1
                if counter == 4:
                    raise OSError('simulated interruption')
                return original(*a, **kw)

            with patch.object(install.shutil, 'move', fail_after_changes):
                with self.assertRaises(OSError):
                    install.publish(stage, root, content)
            for owner in (root / 'mods', content / install.CONTENT):
                for p in owner.rglob('*'):
                    if p.is_file():
                        self.assertEqual(p.read_bytes(), b'old')
            self.assertFalse((root / 'install-state/nsv-transaction.json').exists())


class DirectProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="NSV process user's ")
        cls.root = Path(cls.tmp.name)
        driver = cls.root / 'driver.cpp'
        driver.write_text(r'''
#include "skate3_setup_process.h"
#include <iostream>
#if defined(_WIN32)
int wmain(int argc, wchar_t** argv) {
#else
int main(int argc, char** argv) {
#endif
  if (argc < 3) return 2;
  std::vector<std::string> args;
  for (int i=3; i<argc; ++i) {
    auto u8 = std::filesystem::path(argv[i]).u8string();
    args.emplace_back(u8.begin(), u8.end());
  }
  std::string error;
  const bool ok = skate3::RunSetupProcess(argv[1], args, argv[2], error);
  if (!ok) std::cerr << error;
  return ok ? 0 : 1;
}
''')
        cls.binary = cls.root / ('driver.exe' if sys.platform == 'win32' else 'driver')
        compiler = shutil.which('clang++') or shutil.which('clang++-20')
        if not compiler:
            raise RuntimeError('Install the build compiler before process verification')
        subprocess.run([compiler, '-std=c++23', '-I', str(ROOT / 'src'), str(driver),
                        str(ROOT / 'src/skate3_setup_process.cpp'), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_literal_arguments_unicode_and_redirection(self):
        script = self.root / 'args-é.py'
        script.write_text('import json,sys; print(json.dumps(sys.argv[1:])); print("stderr",file=sys.stderr)')
        log = self.root / 'output-é.log'
        values = ['space name', "apostrophe's", 'quote"value', 'trailing\\', '',
                  'é-city', '$(must_stay_literal)', '; & | < >']
        subprocess.run([str(self.binary), sys.executable, str(log), str(script), *values], check=True)
        lines = log.read_text().splitlines()
        self.assertEqual(json.loads(next(s for s in lines if s.startswith('['))), values)
        self.assertIn('stderr', lines)

    def test_nonzero_exit_and_missing_program(self):
        log = self.root / 'failure.log'
        result = subprocess.run([str(self.binary), sys.executable, str(log), '-c', 'raise SystemExit(7)'],
                                capture_output=True)
        self.assertEqual(result.returncode, 1)
        result = subprocess.run([str(self.binary), str(self.root / 'missing-program'), str(log)],
                                capture_output=True)
        self.assertEqual(result.returncode, 1)

    @unittest.skipIf(sys.platform == 'win32', 'Unix permissions')
    def test_program_copied_without_execute_permission_still_runs(self):
        # Files copied through a Windows PC lose their execute bits.
        helper = self.root / "helper copied from windows"
        helper.write_text('#!/bin/sh\necho helper-ran\n')
        helper.chmod(0o644)
        log = self.root / 'helper.log'
        subprocess.run([str(self.binary), str(helper), str(log)], check=True)
        self.assertIn('helper-ran', log.read_text())
        self.assertEqual(helper.stat().st_mode & 0o777, 0o755)


if __name__ == '__main__':
    unittest.main()
