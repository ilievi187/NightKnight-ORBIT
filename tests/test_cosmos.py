"""Regression coverage for offline COSMOS visuals and asset verification."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cosmos_installer", ROOT / "scripts" / "install_cosmos_assets.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class CosmosTests(unittest.TestCase):
    def test_only_six_expected_asset_names(self):
        self.assertEqual(len(installer.ASSETS), 6)
        for filename, (size, digest) in installer.ASSETS.items():
            self.assertTrue(filename.endswith(".png"))
            self.assertNotIn("/", filename)
            self.assertNotIn("..", filename)
            self.assertIn(size, ((3840, 2160), (4096, 2048)))
            self.assertEqual(len(digest), 64)

    def test_tampered_asset_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "test.png"
            # A minimal header is sufficient for checking rejection paths.
            file.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) +
                             b"IHDR" + struct.pack(">II", 4096, 2048))
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                installer.validate_file(file, (4096, 2048), "0" * 64)
            with self.assertRaisesRegex(ValueError, "expected"):
                installer.validate_file(file, (3840, 2160), "0" * 64)

    def test_cannot_accept_non_png(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "fake.png"
            file.write_bytes(b"x" * 48)
            with self.assertRaisesRegex(ValueError, "PNG header"):
                installer.validate_file(file, (4096, 2048), "0" * 64)

    def test_frontend_is_local_and_progressive(self):
        html = (ROOT / "web" / "index.html").read_text()
        js = (ROOT / "web" / "cosmos.js").read_text()
        app_js = (ROOT / "web" / "app.js").read_text()
        css = (ROOT / "web" / "cosmos.css").read_text()
        self.assertIn('/assets/cosmos.js', html)
        self.assertIn('/assets/cosmos.css', html)
        self.assertIn("getContext('webgl'", js)
        self.assertIn("fallbackTexture", js)
        self.assertIn("prefers-reduced-motion", js)
        self.assertIn("planet_violet_texture.png", js)
        self.assertIn("window.ORBIT_COSMOS?.mount", app_js)
        self.assertIn("cosmos_alex_background.png", css)
        self.assertIn("cosmos_nova_background.png", css)
        self.assertNotIn("https://", js)
        self.assertNotIn("http://", js)


if __name__ == "__main__":
    unittest.main()
