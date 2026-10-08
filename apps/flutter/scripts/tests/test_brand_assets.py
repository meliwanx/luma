"""Icon generation stays out of the repository and covers every platform slot."""

import hashlib
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "brand-assets.sh"
IOS_ICON = (
    ROOT
    / "apps/flutter/ios/Runner/Assets.xcassets/AppIcon.appiconset/Icon-App-1024x1024@1x.png"
)


def solid_png(path, size=1024, color=(30, 102, 245, 255)):
    raw = b"".join(b"\x00" + bytes(color) * size for _ in range(size))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def color_type(path):
    data = path.read_bytes()
    return data[25]


class BrandAssetsTests(unittest.TestCase):
    def test_script_writes_only_the_requested_directory(self):
        before = hashlib.sha256(IOS_ICON.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="oc-brand-assets-") as tmp:
            folder = Path(tmp)
            source = folder / "icon.png"
            tray = folder / "tray.png"
            svg = folder / "logo.svg"
            out = folder / "out"
            solid_png(source)
            solid_png(tray, size=64, color=(0, 0, 0, 255))
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
                '<rect width="64" height="64" fill="#111111"/></svg>',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--png",
                    str(source),
                    "--svg",
                    str(svg),
                    "--tray",
                    str(tray),
                    "--out",
                    str(out),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            appicon = out / "ios" / "AppIcon.appiconset"
            marketing = appicon / "Icon-App-1024x1024@1x.png"
            self.assertTrue((appicon / "Contents.json").is_file())
            self.assertEqual(color_type(marketing), 2)
            for folder_name in (
                "mipmap-mdpi",
                "mipmap-hdpi",
                "mipmap-xhdpi",
                "mipmap-xxhdpi",
                "mipmap-xxxhdpi",
            ):
                self.assertTrue(
                    (out / "android" / folder_name / "ic_launcher.png").is_file(),
                    folder_name,
                )
            desktop = out / "desktop"
            for name in (
                "icon.icns",
                "icon.ico",
                "icon.png",
                "tray.png",
                "tray@2x.png",
                "trayTemplate.png",
                "trayTemplate@2x.png",
            ):
                self.assertTrue((desktop / name).is_file(), name)
                self.assertGreater((desktop / name).stat().st_size, 32)
        self.assertEqual(hashlib.sha256(IOS_ICON.read_bytes()).hexdigest(), before)

    def test_missing_output_and_repo_icon_dirs_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="oc-brand-assets-") as tmp:
            source = Path(tmp) / "icon.png"
            solid_png(source)
            missing = subprocess.run(
                [str(SCRIPT), "--png", str(source)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("--out", missing.stderr)
            blocked = subprocess.run(
                [
                    str(SCRIPT),
                    "--png",
                    str(source),
                    "--out",
                    str(IOS_ICON.parent),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("输出目录", blocked.stderr)


if __name__ == "__main__":
    unittest.main()
