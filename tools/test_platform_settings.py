"""Check native defaults and CMake seeding without overwriting user settings."""
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from platform_settings import settings_for, ROOT


class DesktopDefaults(unittest.TestCase):
    def test_windows_is_byte_identical_and_posix_keeps_gameplay_and_quiet_flags(self):
        source = (ROOT / 'config/release-defaults.toml').read_bytes()
        self.assertEqual(settings_for('Windows'), source)
        original = tomllib.loads(source.decode())
        for platform in ('Linux', 'Darwin'):
            with self.subTest(platform=platform):
                actual = tomllib.loads(settings_for(platform).decode())
                self.assertEqual(actual['input_backend'], 'sdl')
                self.assertFalse(any(k.startswith('d3d12_') for k in actual))
                for key, value in original.items():
                    if key != 'input_backend' and not key.startswith('d3d12_'):
                        self.assertEqual(actual[key], value, key)
                self.assertEqual(actual['gpu_backend'], 'vulkan')
                self.assertEqual(actual['log_level'], 'warn')

    def test_cmake_seeds_all_platforms_and_preserves_existing_user_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            for platform in ('Windows', 'Linux', 'Darwin'):
                with self.subTest(platform=platform):
                    out = Path(temporary) / platform
                    out.mkdir()
                    args = ['cmake', '-DSKATE3_BUILD_ROOT=' + str(out),
                            '-DSKATE3_HOST_PLATFORM=' + platform,
                            '-DSKATE3_PYTHON=' + sys.executable,
                            '-DSKATE3_DEFAULT_SETTINGS=' + str(ROOT / 'config/release-defaults.toml'),
                            '-P', str(ROOT / 'cmake/StageDefaultSettings.cmake')]
                    subprocess.run(args, check=True)
                    self.assertEqual((out / 'settings.toml').read_bytes(), settings_for(platform))
                    sentinel = b"input_backend = 'my-choice'\n# user settings\n"
                    (out / 'settings.toml').write_bytes(sentinel)
                    subprocess.run(args, check=True)
                    self.assertEqual((out / 'settings.toml').read_bytes(), sentinel)


if __name__ == '__main__':
    unittest.main()
