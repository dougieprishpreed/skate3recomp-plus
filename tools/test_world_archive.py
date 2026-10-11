import struct
import tempfile
import unittest
from pathlib import Path

from convert_world_archive import convert
from world_archive import big_entries, catalog, eb_entries, path_hash


def fixture():
    # Distinct sizes and last entry catch the former off-by-one BIG parser.
    names = ['data/content/world/stream/BAM/BAM_Pres.xmm',
             'data/content/world/stream/BAM/BAM_Pres.xst',
             'data/content/world/stream/BAM/BAM_Sim.loc']
    payloads = [struct.pack('>6I', 0x4d4d4150, 3, 0, 7, 0, 0),
                struct.pack('>6I', 0x41544f43, 3, 0, 7, 1, 0) + bytes(64),
                b'<Location>last entry</Location>']
    data = bytearray(1408)
    struct.pack_into('>4s3I', data, 0, b'BIG4', len(data), len(names), 1024)
    pos = 16
    for index, (name, payload) in enumerate(zip(names, payloads)):
        offset = 1024 + index * 128
        struct.pack_into('>2I', data, pos, offset, len(payload))
        encoded = name.replace('/', '\\').encode() + b'\0'
        data[pos + 8:pos + 8 + len(encoded)] = encoded
        pos += 8 + len(encoded)
        data[offset:offset + len(payload)] = payload
    return data, names, payloads


class ArchiveTests(unittest.TestCase):
    def test_first_and_last_big_entries(self):
        data, names, payloads = fixture()
        parsed = big_entries(data)
        self.assertEqual([e.name for e in parsed], names)
        self.assertEqual([data[e.offset:e.offset + e.size] for e in parsed], payloads)
        self.assertEqual(len(catalog(data)), 2)

    def test_reject_truncated_and_invalid_ranges(self):
        data, _, _ = fixture()
        struct.pack_into('>I', data, 16, len(data))
        with self.assertRaises(ValueError):
            big_entries(data)
        with self.assertRaises(ValueError):
            big_entries(b'BIG4')

    def test_known_retail_hash_is_case_sensitive(self):
        name = 'data/content/world/stream/DIST_BlackBoxPark/DIST_BlackBoxPark_Tex.xmm'
        self.assertEqual(path_hash(name), 0x00102af7)
        self.assertNotEqual(path_hash(name.lower()), path_hash(name))
        self.assertEqual(path_hash(name.replace('/', '\\')), path_hash(name))

    def test_eb_roundtrip_and_zero_index_rejection(self):
        data, names, payloads = fixture()
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'source.big', Path(directory) / 'output.big'
            source.write_bytes(data)
            convert(source, output)
            eb = bytearray(output.read_bytes())
            parsed = {e.name: e for e in eb_entries(eb)}
            for name, payload in zip(names, payloads):
                entry = parsed[name]
                self.assertEqual(eb[entry.offset:entry.offset + entry.size], payload)
            self.assertEqual(eb[1024:], data[1024:])
            with self.assertRaises(ValueError):
                convert(source, output)
            eb[48:64] = bytes(16)
            with self.assertRaises(ValueError):
                eb_entries(eb)


if __name__ == '__main__':
    unittest.main()
