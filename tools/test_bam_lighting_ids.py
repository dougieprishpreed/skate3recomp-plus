import tempfile
import unittest
from pathlib import Path
from build_bam_lighting_ids import exports, inventory

NAME = "chromo_[0x0000000c03e38704][0x000002ea03e38705][0x000402ed03e38706]_HighLOD_0003.Texture"

def record(guid, name=NAME):
    return guid.to_bytes(8, "big") + bytes.fromhex("ac462e4a00000001") + name.encode() + b"\0"

class InventoryTests(unittest.TestCase):
    def test_export_guards(self):
        good = record(0x7962212EEDACAFC0)
        self.assertEqual(list(exports(good))[0][:2], (0x7962212EEDACAFC0, NAME))
        self.assertEqual(list(exports(b"x" + good)), [])
        self.assertEqual(list(exports(good.replace(bytes.fromhex("ac462e4a"), b"xxxx"))), [])
        self.assertEqual(list(exports(good[:-1])), [])

    def test_pairs_and_key_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cTex.xsf").write_bytes(record(0x7962212EEDACAFC0))
            (root / "cPres.xsf").write_bytes(record(0x7962212EEDACAFC1))
            lighting, names = inventory(root)
            self.assertEqual(len(lighting), 2)
            (root / "alias.xsf").write_bytes(record(0x3962212EEDACAFC0))
            with self.assertRaisesRegex(ValueError, "collision"):
                inventory(root)

    def test_conflicting_variant_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cTex.xsf").write_bytes(record(0x7962212EEDACAFC0))
            (root / "cPres.xsf").write_bytes(record(0x7962212EEDACAFC1, NAME.replace("0003.Texture", "0004.Texture")))
            with self.assertRaisesRegex(ValueError, "Variant names differ"):
                inventory(root)

if __name__ == "__main__":
    unittest.main()
