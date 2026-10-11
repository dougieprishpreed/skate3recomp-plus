"""Publication failure must leave a previously working installation intact."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import prepare_new_san_van as install


class PublicationTest(unittest.TestCase):
    def test_failed_replacement_rolls_back_every_changed_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            stage, root = base / 'stage', base / 'installed'
            for folder in (stage, root):
                for name in install.FOLDERS:
                    p = folder / 'mods/skate2' / name
                    p.mkdir(parents=True)
                    (p / 'asset').write_bytes(folder.name.encode())
                for name in ('nsv_spawns.txt', 'install-manifest.json'):
                    (folder / 'mods/skate2' / name).write_bytes(folder.name.encode())
                for branch in ('00000002', 'Headers/00000002'):
                    (folder / install.CONTENT / branch).mkdir(parents=True)
            move = install.shutil.move
            count = 0

            def fail_second(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError('simulated publication failure')
                return move(*args, **kwargs)

            with patch.object(install.shutil, 'move', fail_second):
                with self.assertRaisesRegex(OSError, 'simulated'):
                    install.publish(stage, root, root)
            for p in (root / 'mods/skate2').rglob('*'):
                if p.is_file():
                    self.assertEqual(p.read_bytes(), b'installed')
            self.assertFalse((root / 'install-state/nsv-transaction.json').exists())

    def test_recover_after_backup_and_partial_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            dest = root / 'mods/skate2/nsv_spawns.txt'
            backup = root / 'install-backups/test/0'
            dest.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            dest.write_bytes(b'new')
            backup.write_bytes(b'working')
            journal = root / 'install-state/nsv-transaction.json'
            journal.parent.mkdir()
            journal.write_text(json.dumps([dict(destination=str(dest), backup=str(backup), had_old=True)]))
            install.recover(root, root)
            self.assertEqual(dest.read_bytes(), b'working')
            self.assertFalse(journal.exists())


if __name__ == '__main__':
    unittest.main()
