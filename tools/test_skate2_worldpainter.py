"""Regression checks for fail-closed WorldPainter stream discovery."""
import unittest
from pathlib import Path
from unittest.mock import patch
from skate2_worldpainter import stream_layers


class StreamDiscoveryTests(unittest.TestCase):
    def test_inaccessible_map_propagates_error(self):
        with patch.object(Path, 'iterdir', side_effect=PermissionError('map access denied')):
            with self.assertRaises(PermissionError):
                stream_layers(Path('unreadable-map'))

    def test_empty_map_is_not_a_valid_manifest_source(self):
        with patch.object(Path, 'iterdir', return_value=iter(())):
            with self.assertRaisesRegex(ValueError, 'no collision cells'):
                stream_layers(Path('empty-map'))


if __name__ == '__main__':
    unittest.main()
