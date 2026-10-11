"""Speech metadata/namespace tests; fixtures contain no game assets."""
import struct
import unittest

from skate2_speech import isolate_events, read_events, PREFERRED_NAMESPACE


def fixture():
    data = bytearray(28)
    data[:4] = bytes.fromhex('0315b007')
    struct.pack_into('>I', data, 4, 44)
    data[8] = 1
    struct.pack_into('>H', data, 16, 2)
    struct.pack_into('>2H', data, 24, 7, 9)
    data.extend(struct.pack('>4H', 0x2001, 200, 500, 0))
    data.extend(struct.pack('>4H', 0x2002, 200, 500, 0))
    data.extend(struct.pack('>8I', 0, 0x12340001, 0, 0, 0, 0, 0, 0))
    data.extend(struct.pack('>8I', 6, 0x12340002, 0, 0, 0, 0, 0, 0))
    data.extend(b'first\0second\0')
    return bytes(data)


class SpeechTests(unittest.TestCase):
    def test_native_fixed_index_with_variable_length_source_names(self):
        from world_archive import build_eb_archive, eb_entries
        files = [('a.hdr', b'first header'), ('longer_sample_name.hdr', b'last header'*3)]
        packed = build_eb_archive(files)
        for index, entry in enumerate(eb_entries(packed)):
            offset = struct.unpack_from('>I',packed,48+index*16)[0] << packed[10]
            self.assertEqual(packed[offset:offset+entry.size], dict(files)[entry.name[2:]])
        # Retail layout: one zero storage-mode byte per entry right after the
        # index (where the game's lookup reads it), padded to 16, then names.
        count, names_at = struct.unpack_from('>I', packed, 4)[0], struct.unpack_from('>I', packed, 12)[0]
        modes_at = (48 + 16 * count + 15) & ~15
        self.assertEqual(names_at, (modes_at + count + 15) & ~15)
        self.assertEqual(packed[modes_at:names_at], bytes(names_at - modes_at))
        with self.assertRaises(ValueError):
            build_eb_archive(files+files)


    def test_namespace_preserves_programs_and_bindings(self):
        source = fixture()
        result = isolate_events(source, 0x1234, {0, 1, 2, 3})
        self.assertEqual(result[:8], source[:8])
        self.assertEqual(result[8:10], b'\x34\x12')
        self.assertEqual(result[10:], source[10:])
        self.assertEqual(read_events(result).events, read_events(source).events)

    def test_actual_native_namespaces_include_cameraman_four(self):
        occupied = {0, 1, 3, 4}
        with self.assertRaisesRegex(ValueError, 'namespace'):
            isolate_events(fixture(), 4, occupied)
        result = isolate_events(fixture(), PREFERRED_NAMESPACE, occupied)
        self.assertEqual(read_events(result).namespace, 0x4E53)
        self.assertEqual(result[8:10], b'\x53\x4e')
        self.assertEqual(result[10:], fixture()[10:])

    def test_namespace_collisions_refused(self):
        for namespace in (-1, 0, 1, 2, 3, 0x10000):
            with self.subTest(namespace=namespace), self.assertRaisesRegex(ValueError, 'namespace'):
                isolate_events(fixture(), namespace, {0, 1, 2, 3})

    def test_malformed_tables_and_runtime_pointers_refused(self):
        changes = [(4, '>I', 0xfffffff0), (24, '>H', 0),
                   (26, '>H', 7), (36, '>H', 0x2001),
                   (44, '>I', 0xffffff00), (52, '>I', 0x90000000)]
        for at, fmt, value in changes:
            broken = bytearray(fixture())
            struct.pack_into(fmt, broken, at, value)
            with self.subTest(offset=at), self.assertRaises(ValueError):
                read_events(broken)
        with self.assertRaises(ValueError):
            read_events(fixture()[:-1])


if __name__ == '__main__':
    unittest.main()
