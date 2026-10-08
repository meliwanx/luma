#!/usr/bin/env python3
"""Write platform icons under --out. Never touches the repository's own icons.

Called by scripts/brand-assets.sh. Uses sips, iconutil and the Python stdlib.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "desktop" / "scripts"))
from generate_brand_icons import resize  # noqa: E402

CONTENTS = ROOT / "apps/flutter/ios/Runner/Assets.xcassets/AppIcon.appiconset/Contents.json"

# Destinations the open-source tree already ships. A company build passes a
# private directory and copies the result over these at pack time.
_PROTECTED = (
    ROOT / "apps/flutter/ios",
    ROOT / "apps/flutter/android",
    ROOT / "apps/flutter/macos",
    ROOT / "apps/flutter/windows",
    ROOT / "apps/desktop/build",
    ROOT / "apps/web/public",
)

ANDROID = (
    ("mipmap-mdpi", 48),
    ("mipmap-hdpi", 72),
    ("mipmap-xhdpi", 96),
    ("mipmap-xxhdpi", 144),
    ("mipmap-xxxhdpi", 192),
)

ICONSET = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def fail(message: str) -> None:
    print("错误：%s" % message, file=sys.stderr)
    raise SystemExit(1)


def require_tool(name: str) -> None:
    if subprocess.call(["/usr/bin/which", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        fail("找不到 %s" % name)


def png_size(path: Path) -> tuple:
    output = subprocess.check_output(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        text=True,
    )
    values = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return int(values["pixelWidth"]), int(values["pixelHeight"])


def assert_outside_repo(out: Path) -> None:
    resolved = out.resolve()
    for protected in _PROTECTED:
        try:
            resolved.relative_to(protected.resolve())
        except ValueError:
            continue
        fail("输出目录不能放在仓库自带的图标目录里：%s" % protected)


def write_ico(pngs, output: Path) -> None:
    container = bytearray(struct.pack("<HHH", 0, 1, len(pngs)))
    offset = 6 + 16 * len(pngs)
    for size, payload in pngs:
        container.extend(
            struct.pack(
                "<BBBBHHII",
                size if size < 256 else 0,
                size if size < 256 else 0,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        offset += len(payload)
    for _, payload in pngs:
        container.extend(payload)
    output.write_bytes(container)


def rasterize_svg(svg: Path, dest: Path, size: int = 512) -> None:
    require_tool("qlmanage")
    with tempfile.TemporaryDirectory(prefix="brand-svg-") as tmp:
        folder = Path(tmp)
        result = subprocess.run(
            ["qlmanage", "-t", "-s", str(size), "-o", str(folder), str(svg)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        produced = list(folder.glob("*.png"))
        if result.returncode != 0 or not produced:
            fail("无法从 SVG 生成托盘图：%s" % (result.stderr or svg))
        dest.write_bytes(produced[0].read_bytes())


def ios_icons(master: Path, out: Path) -> None:
    if not CONTENTS.is_file():
        fail("缺少 iOS 图标目录模板 %s" % CONTENTS)
    appicon = out / "ios" / "AppIcon.appiconset"
    appicon.mkdir(parents=True, exist_ok=True)
    contents = json.loads(CONTENTS.read_text(encoding="utf-8"))
    (appicon / "Contents.json").write_text(
        json.dumps(contents, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    written = set()
    for item in contents["images"]:
        filename = item.get("filename")
        if not filename or filename in written:
            continue
        points = float(item["size"].split("x")[0])
        scale = int(str(item["scale"]).rstrip("x"))
        pixels = int(round(points * scale))
        # The App Store 1024 image must not carry an alpha channel. The other
        # slots are flattened the same way so a later copy cannot reintroduce it.
        resize(master, appicon / filename, pixels, pixels, alpha=False)
        written.add(filename)


def android_icons(master: Path, out: Path) -> None:
    for folder, pixels in ANDROID:
        dest = out / "android" / folder / "ic_launcher.png"
        resize(master, dest, pixels, pixels, alpha=True)


def desktop_icons(master: Path, tray: Path, out: Path) -> None:
    desktop = out / "desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    resize(master, desktop / "icon.png", 1024, 1024, alpha=True)
    with tempfile.TemporaryDirectory(prefix="brand-iconset-") as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for name, pixels in ICONSET:
            resize(master, iconset / name, pixels, pixels, alpha=True)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(desktop / "icon.icns")],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    pngs = []
    for size in ICO_SIZES:
        dest = desktop / ("icon-%d.png" % size)
        resize(master, dest, size, size, alpha=True)
        pngs.append((size, dest.read_bytes()))
        dest.unlink()
    write_ico(pngs, desktop / "icon.ico")
    for name, pixels in (
        ("tray.png", 16),
        ("tray@2x.png", 32),
        ("trayTemplate.png", 16),
        ("trayTemplate@2x.png", 32),
    ):
        resize(tray, desktop / name, pixels, pixels, alpha=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", required=True, type=Path, help="1024x1024 PNG")
    parser.add_argument("--out", required=True, type=Path, help="directory to create")
    parser.add_argument("--svg", type=Path, help="optional SVG used for tray icons")
    parser.add_argument("--tray", type=Path, help="optional tray PNG; wins over --svg")
    args = parser.parse_args()
    require_tool("sips")
    require_tool("iconutil")
    if not args.png.is_file():
        fail("找不到 PNG：%s" % args.png)
    if png_size(args.png) != (1024, 1024):
        fail("PNG 必须是 1024×1024")
    if args.svg is not None and not args.svg.is_file():
        fail("找不到 SVG：%s" % args.svg)
    if args.tray is not None and not args.tray.is_file():
        fail("找不到托盘 PNG：%s" % args.tray)
    out = args.out
    assert_outside_repo(out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="brand-assets-") as tmp:
        folder = Path(tmp)
        tray = folder / "tray-master.png"
        if args.tray is not None:
            tray.write_bytes(args.tray.read_bytes())
        elif args.svg is not None:
            rasterize_svg(args.svg, tray)
        else:
            tray.write_bytes(args.png.read_bytes())
        ios_icons(args.png, out)
        android_icons(args.png, out)
        desktop_icons(args.png, tray, out)
    print("已写入 %s" % out)


if __name__ == "__main__":
    main()
