#!/usr/bin/env python3
"""Regenerate Luma assets on macOS with Python's stdlib, sips and iconutil."""
import argparse
import binascii
import hashlib
import json
import os
import re
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def chunk(kind, payload):
    return (struct.pack('>I', len(payload)) + kind + payload +
            struct.pack('>I', binascii.crc32(kind + payload) & 0xffffffff))


def read_png(path):
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError('Expected PNG: {}'.format(path))
    offset, compressed = 8, bytearray()
    while offset < len(data):
        length = struct.unpack_from('>I', data, offset)[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        if kind == b'IHDR':
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
        elif kind == b'IDAT':
            compressed.extend(payload)
        offset += length + 12
    if depth != 8 or color not in (2, 6) or interlace != 0:
        raise ValueError('Expected non-interlaced RGB/RGBA8 PNG: {}'.format(path))
    channels = 4 if color == 6 else 3
    stride = width * channels
    raw = zlib.decompress(compressed)
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        method = raw[start]
        row = bytearray(raw[start + 1:start + 1 + stride])
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            above = previous[x]
            corner = previous[x - channels] if x >= channels else 0
            if method == 1:
                predictor = left
            elif method == 2:
                predictor = above
            elif method == 3:
                predictor = (left + above) // 2
            elif method == 4:
                p = left + above - corner
                a, b, c = abs(p - left), abs(p - above), abs(p - corner)
                predictor = left if a <= b and a <= c else above if b <= c else corner
            elif method == 0:
                predictor = 0
            else:
                raise ValueError('Unsupported PNG filter')
            row[x] = (row[x] + predictor) & 255
        if channels == 3:
            rgba = bytearray()
            for x in range(0, stride, 3):
                rgba.extend(row[x:x + 3])
                rgba.append(255)
            rows.append(rgba)
        else:
            rows.append(row)
        previous = row
    return width, height, rows


def write_png(path, width, height, rows, alpha=True, dpi=72):
    path.parent.mkdir(parents=True, exist_ok=True)
    scanlines = bytearray()
    for row in rows:
        scanlines.append(0)
        if alpha:
            scanlines.extend(row)
        else:
            for x in range(0, len(row), 4):
                a = row[x + 3]
                scanlines.extend((row[x + c] * a + 255 * (255 - a) + 127) // 255 for c in range(3))
    ppm = round(dpi / 0.0254)
    path.write_bytes(PNG_SIGNATURE +
                     chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6 if alpha else 2, 0, 0, 0)) +
                     chunk(b'pHYs', struct.pack('>IIB', ppm, ppm, 1)) +
                     chunk(b'IDAT', zlib.compress(scanlines, 9)) + chunk(b'IEND', b''))


def bounds(rows, predicate):
    xs, ys = [], []
    for y, row in enumerate(rows):
        for x in range(len(row) // 4):
            if predicate(row[x * 4:x * 4 + 4]):
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def square(rows, box, padding, transparent):
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    side = round(max(width, height) * (1 + padding))
    background = bytes((0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    output = [bytearray(background * side) for _ in range(side)]
    x, y = (side - width) // 2, (side - height) // 2
    for i in range(height):
        output[y + i][x * 4:(x + width) * 4] = rows[top + i][left * 4:right * 4]
    return side, output


def resize(source, output, width, height, alpha=False, dpi=72):
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['sips', '-z', str(height), str(width), str(source), '--out', str(output)],
                   check=True, stdout=subprocess.DEVNULL)
    w, h, rows = read_png(output)
    # Write RGB icons explicitly, so even the App Store image has no alpha channel.
    write_png(output, w, h, rows, alpha=alpha, dpi=dpi)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('/tmp/luma-logo'))
    args = parser.parse_args()
    build = ROOT / 'apps/desktop/build'
    public = ROOT / 'apps/web/public'
    ios = ROOT / 'apps/flutter/ios/Runner/Assets.xcassets'
    generated = []
    with tempfile.TemporaryDirectory(prefix='luma-brand-') as tmp:
        temp = Path(tmp)
        _, _, rows = read_png(args.source / '蓝色无圆角 png.png')
        box = bounds(rows, lambda pixel: pixel[0] < 200 and pixel[2] > 200 and pixel[3] > 128)
        # The supplied wordmark already spans 87% of the square. Center the ink
        # and use 5% side margins; more padding would shrink the long wordmark.
        side, icon_rows = square(rows, box, padding=0.10, transparent=False)
        master = temp / 'app-icon.png'
        write_png(master, side, side, icon_rows, alpha=False)
        contents = json.loads((ios / 'AppIcon.appiconset/Contents.json').read_text())
        for item in contents['images']:
            pixels = round(float(item['size'].split('x')[0]) * float(item['scale'][:-1]))
            output = ios / 'AppIcon.appiconset' / item['filename']
            if output in generated:
                continue
            resize(master, output, pixels, pixels)
            generated.append(output)
        for output, size in [(public / 'favicon-32.png', 32),
                             (public / 'apple-touch-icon.png', 180)]:
            resize(master, output, size, size)
            generated.append(output)

        svg = (args.source / '蓝色色 SVG.svg').read_text()
        logo_path = re.search(r'<path[^>]+d="([^"]+)"', svg).group(1)
        favicon = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="45 45 1066 1066">'
                   '<rect x="45" y="45" width="1066" height="1066" rx="180" fill="white"/>'
                   '<path d="{}" transform="translate(0 44)" fill="none" stroke="#2563EB" '
                   'stroke-width="64" stroke-linecap="round"/></svg>\n').format(logo_path)
        (public / 'favicon.svg').write_text(favicon)
        generated.append(public / 'favicon.svg')

        # Use the supplied black SVG with a thicker stroke for tiny tray sizes.
        tray_svg = (args.source / '黑色 SVG.svg').read_text()
        path_data = re.search(r'<path[^>]+d="([^"]+)"', tray_svg).group(1)
        tokens = [token if token in ('M', 'C') else float(token)
                  for token in re.findall(r'[MC]|-?\d+(?:\.\d+)?', path_data)]
        path_json = temp / 'tray-path.json'
        path_json.write_text(json.dumps(tokens))
        screenshot = temp / 'tray-source.png'
        environment = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(temp / 'clang-cache'),
                           SWIFT_MODULECACHE_PATH=str(temp / 'swift-cache'))
        subprocess.run(['swift', str(Path(__file__).with_name('render_tray.swift')),
                        str(path_json), str(screenshot)], check=True, env=environment)
        _, _, tray_rows = read_png(screenshot)
        tray_box = bounds(tray_rows, lambda pixel: pixel[3] > 0)
        tray_side, padded = square(tray_rows, tray_box, padding=0.06, transparent=True)
        tray_master = temp / 'tray.png'
        write_png(tray_master, tray_side, tray_side, padded)
        left, top, right, bottom = tray_box
        # A horizontal 32x16 pt menu-bar image preserves the readable wordmark.
        # Windows notification-area icons remain square at 16/32 px.
        width, height = right - left, bottom - top
        canvas_width = round(max(width * 1.06, height * 2.12))
        canvas_height = canvas_width // 2
        mac_rows = [bytearray(canvas_width * 4) for _ in range(canvas_height)]
        x, y = (canvas_width - width) // 2, (canvas_height - height) // 2
        for row_index in range(height):
            mac_rows[y + row_index][x * 4:(x + width) * 4] = tray_rows[top + row_index][left * 4:right * 4]
        mac_master = temp / 'tray-template.png'
        write_png(mac_master, canvas_width, canvas_height, mac_rows)
        for scale in (1, 2):
            size = 16 * scale
            suffix = '@2x' if scale == 2 else ''
            output = build / ('trayTemplate' + suffix + '.png')
            resize(mac_master, output, size * 2, size, alpha=True, dpi=72 * scale)
            generated.append(output)
            windows = temp / ('windows-tray' + suffix + '.png')
            resize(tray_master, windows, size, size, alpha=True, dpi=72 * scale)
            _, _, colored = read_png(windows)
            for row in colored:
                for i in range(0, len(row), 4):
                    row[i:i + 3] = bytes((37, 99, 235))
            output = build / ('tray' + suffix + '.png')
            write_png(output, size, size, colored, dpi=72 * scale)
            generated.append(output)

        _, _, blue_rows = read_png(args.source / '蓝色 png.png')
        left, top, right, bottom = bounds(blue_rows, lambda pixel: pixel[3] > 0)
        width = right - left
        height = round(width * 115 / 240)
        cropped = [bytearray(width * 4) for _ in range(height)]
        y = (height - (bottom - top)) // 2
        for row_index in range(bottom - top):
            cropped[y + row_index] = blue_rows[top + row_index][left * 4:right * 4]
        launch_master = temp / 'launch.png'
        write_png(launch_master, width, height, cropped)
        for scale in (1, 2, 3):
            output = ios / 'LaunchImage.imageset' / ('LaunchImage{}.png'.format('@{}x'.format(scale) if scale > 1 else ''))
            resize(launch_master, output, 240 * scale, 115 * scale, alpha=True, dpi=72 * scale)
            generated.append(output)

    # Keep desktop platform masks when regenerating the complete brand family.
    from make_icons import generate
    generated.extend(generate(build, source=args.source / '蓝色色 SVG.svg'))

    manifest = []
    for file in generated:
        entry = {'file': str(file.relative_to(ROOT)), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
        if file.suffix == '.png':
            w, h, depth, color, *_ = struct.unpack('>IIBBBBB', file.read_bytes()[16:29])
            entry.update(width=w, height=h, alpha=color == 6)
            if file.parent.name == 'icon.iconset' and file.name in ('icon_16x16.png', 'icon_32x32.png'):
                entry['rgba_sha256'] = hashlib.sha256(b''.join(read_png(file)[2])).hexdigest()
        elif file.suffix == '.ico':
            entry['sizes'] = [16, 24, 32, 48, 64, 128, 256]
        elif file.suffix == '.icns':
            entry['sizes'] = [16, 32, 64, 128, 256, 512, 1024]
        manifest.append(entry)
    (build / 'brand-icons.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print('Generated {} assets; manifest: apps/desktop/build/brand-icons.json'.format(len(manifest)))
    print('Source ink bounds: {}'.format(box))


if __name__ == '__main__':
    main()
