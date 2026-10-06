#!/usr/bin/env python3
"""Generate desktop icons with Python 3.9, CoreGraphics, sips and iconutil on macOS.

Run: python3 apps/desktop/scripts/make_icons.py
Only desktop app icons and their manifest entries are updated; tray assets stay intact.
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from generate_brand_icons import ROOT, read_png, resize, write_png

# Original path from /tmp/luma-logo/蓝色色 SVG.svg, embedded for repeatable builds.
LOGO_PATH = (
    'M1049.5 648C1030.5 676 987.5 731 955 724C897.909 711.704 980.5 600.5 896 579'
    'C860.193 569.89 831.748 590.946 807.811 619C775.262 657.15 751.05 708.238 728 714'
    'C688 724 705 581.5 669 579C633 576.5 595.5 704 585.5 704C575.5 704 597 593 551 593'
    'C505 593 489.5 719.5 455 724C427.4 727.6 423 655 430.5 600.5C430.5 587 377.5 745 343 740'
    'C308.5 735 343 593 324 593C312.5 593 262.5 760.75 205 740C90 698.5 273.5 233 360 342'
    'C405.5 411.5 186.333 599.667 93 648M896 579C977 609 891.5 740 827 740'
    'C784.079 740 751.261 700.6 807.811 619'
)
ICONSET = (
    ('ic04', 16, 1), ('ic11', 16, 2), ('ic05', 32, 1), ('ic12', 32, 2),
    ('ic07', 128, 1), ('ic13', 128, 2), ('ic08', 256, 1), ('ic14', 256, 2),
    ('ic09', 512, 1), ('ic10', 512, 2),
)
WINDOWS_SIZES = (16, 24, 32, 48, 64, 128, 256)


def logo_bounds(path_data, stroke):
    """Find cubic extrema so the ink, including round caps, is centered."""
    tokens = re.findall(r'[MC]|-?\d+(?:\.\d+)?', path_data)
    if re.sub(r'[MC]|-?\d+(?:\.\d+)?|[\s,]', '', path_data):
        raise ValueError('Logo path must use absolute M/C commands')
    points = []
    index = 0
    current = None
    while index < len(tokens):
        command = tokens[index]
        index += 1
        count = 2 if command == 'M' else 6 if command == 'C' else 0
        if not count or index + count > len(tokens):
            raise ValueError('Incomplete logo path')
        values = [float(value) for value in tokens[index:index + count]]
        index += count
        if command == 'M':
            current = tuple(values)
            points.append(current)
            continue
        if current is None:
            raise ValueError('Logo path must start with M')
        controls = (current, tuple(values[:2]), tuple(values[2:4]), tuple(values[4:]))
        positions = {0.0, 1.0}
        for axis in (0, 1):
            p0, p1, p2, p3 = [point[axis] for point in controls]
            a = -p0 + 3 * p1 - 3 * p2 + p3
            b = 2 * (p0 - 2 * p1 + p2)
            c = p1 - p0
            if abs(a) < 1e-9:
                roots = [-c / b] if abs(b) > 1e-9 else []
            else:
                discriminant = b * b - 4 * a * c
                roots = [(-b + sign * math.sqrt(discriminant)) / (2 * a)
                         for sign in (-1, 1)] if discriminant >= 0 else []
            positions.update(value for value in roots if 0 < value < 1)
        for value in positions:
            points.append(tuple((1 - value) ** 3 * controls[0][axis] +
                                3 * (1 - value) ** 2 * value * controls[1][axis] +
                                3 * (1 - value) * value ** 2 * controls[2][axis] +
                                value ** 3 * controls[3][axis] for axis in (0, 1)))
        current = controls[-1]
    if not points:
        raise ValueError('Empty logo path')
    return (min(point[0] for point in points) - stroke / 2,
            min(point[1] for point in points) - stroke / 2,
            max(point[0] for point in points) + stroke / 2,
            max(point[1] for point in points) + stroke / 2)


def squircle(side):
    # n=5 superellipse: continuous curvature without rounded-rectangle joins.
    coordinates = []
    for index in range(720):
        angle = index * 2 * math.pi / 720
        x, y = math.cos(angle), math.sin(angle)
        coordinates.append([
            512 + side / 2 * math.copysign(abs(x) ** 0.4, x),
            512 + side / 2 * math.copysign(abs(y) ** 0.4, y)])
    return coordinates


def icon_spec(path_data, windows=False, small=False):
    side = 1024 * 0.88 if windows else 824
    margin = (1024 - side) / 2
    stroke = 76 if small else 51
    left, top, right, bottom = logo_bounds(path_data, stroke)
    scale = side * 0.72 / (right - left)
    x = 512 - (left + right) / 2 * scale
    y = 512 - (top + bottom) / 2 * scale
    tokens = [token if token in ('M', 'C') else float(token)
              for token in re.findall(r'[MC]|-?\d+(?:\.\d+)?', path_data)]
    return {'canvas': 1024, 'windows': windows, 'small': small, 'side': side, 'margin': margin,
            'radius': side * 0.18, 'body_points': squircle(side),
            'shadow_dy': 6 if windows else 10, 'shadow_blur': 12 if windows else 20,
            'logo_tokens': tokens, 'logo_stroke': stroke, 'logo_scale': scale,
            'logo_x': x, 'logo_y': y}


def render(renderer, temp, name, spec):
    document = temp / (name + '.json')
    output = temp / (name + '.png')
    document.write_text(json.dumps(spec), encoding='utf-8')
    subprocess.run([str(renderer), str(document), str(output)], check=True, timeout=30)
    width, height, rows = read_png(output)
    if (width, height) != (1024, 1024):
        raise ValueError('Rendered icon must be 1024x1024')
    write_png(output, width, height, rows)
    verify_png(output, 1024)
    return output


def verify_png(file, expected):
    width, height, rows = read_png(file)
    corners = [rows[y][x * 4 + 3] for x, y in
               ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))]
    if (width, height) != (expected, expected) or any(corners):
        raise ValueError('{} must be {}px with transparent corners'.format(file, expected))
    if rows[height // 2][width // 2 * 4 + 3] != 255:
        raise ValueError('{} must have an opaque center'.format(file))


def iconset_name(points, scale):
    return 'icon_{}x{}{}.png'.format(points, points, '@2x' if scale == 2 else '')


def argb_payload(file):
    # macOS corrupts PNG-backed icp4/icp5. Native ic04/ic05 use straight ARGB
    # planes with ICNS RLE. Literal runs suffice for these two tiny images.
    _, _, rows = read_png(file)
    rgba = b''.join(rows)
    payload = bytearray(b'ARGB')
    for channel in (3, 0, 1, 2):
        plane = rgba[channel::4]
        for offset in range(0, len(plane), 128):
            block = plane[offset:offset + 128]
            payload.append(len(block) - 1)
            payload.extend(block)
    return bytes(payload)


def decode_argb(payload, size):
    if not payload.startswith(b'ARGB'):
        raise ValueError('Expected native ICNS ARGB')
    offset, planes = 4, []
    for _ in range(4):
        plane = bytearray()
        while len(plane) < size * size:
            if offset >= len(payload):
                raise ValueError('Truncated ICNS ARGB plane')
            control = payload[offset]
            offset += 1
            count = control + 1 if control < 128 else control - 125
            length = count if control < 128 else 1
            if len(plane) + count > size * size or offset + length > len(payload):
                raise ValueError('Invalid ICNS ARGB run')
            plane.extend(payload[offset:offset + length] if control < 128 else
                         payload[offset:offset + 1] * count)
            offset += length
        planes.append(plane)
    if offset != len(payload):
        raise ValueError('Trailing ICNS ARGB bytes')
    rgba = bytearray(size * size * 4)
    for plane, channel in zip(planes, (3, 0, 1, 2)):
        rgba[channel::4] = plane
    return bytes(rgba)


def verify_icns_payloads(iconset, output):
    data = output.read_bytes()
    if data[:4] != b'icns' or struct.unpack_from('>I', data, 4)[0] != len(data):
        raise ValueError('Invalid ICNS header')
    expected = {kind: (points, scale) for kind, points, scale in ICONSET}
    found, offset = set(), 8
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('Truncated ICNS chunk')
        kind = data[offset:offset + 4].decode('ascii')
        length = struct.unpack_from('>I', data, offset + 4)[0]
        if length < 8 or offset + length > len(data):
            raise ValueError('Invalid ICNS chunk length')
        payload = data[offset + 8:offset + length]
        if kind in ('icp4', 'icp5'):
            raise ValueError('Small ICNS images must use native ARGB')
        if kind in expected:
            if kind in found:
                raise ValueError('Duplicate ICNS representation')
            found.add(kind)
            points, scale = expected[kind]
            file = iconset / iconset_name(points, scale)
            if kind in ('ic04', 'ic05'):
                if decode_argb(payload, points) != b''.join(read_png(file)[2]):
                    raise ValueError('ICNS ARGB pixels changed')
            elif payload != file.read_bytes():
                raise ValueError('ICNS PNG pixels or metadata changed')
        offset += length
    if found != set(expected):
        raise ValueError('ICNS is missing a standard representation')


def write_icns(iconset, output, temp):
    result = subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(output)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    canonical = False
    if result.returncode == 0:
        try:
            verify_icns_payloads(iconset, output)
            canonical = True
        except ValueError:
            pass
    if not canonical:
        parts = []
        for kind, points, scale in ICONSET:
            file = iconset / iconset_name(points, scale)
            payload = argb_payload(file) if kind in ('ic04', 'ic05') else file.read_bytes()
            parts.append(kind.encode('ascii') + struct.pack('>I', len(payload) + 8) + payload)
        body = b''.join(parts)
        output.write_bytes(b'icns' + struct.pack('>I', len(body) + 8) + body)
        print('Used native ARGB for 16/32px and PNG for the eight remaining ICNS representations.')
    verify_icns_payloads(iconset, output)
    decoded = temp / 'verified.iconset'
    subprocess.run(['iconutil', '-c', 'iconset', str(output), '-o', str(decoded)],
                   check=True, stdout=subprocess.DEVNULL)
    for kind, points, scale in ICONSET:
        file = decoded / iconset_name(points, scale)
        verify_png(file, points * scale)
        # Apple's ARGB-to-PNG export rounds translucent RGB; the straight pixels
        # above are the lossless oracle for those two native representations.
        if kind not in ('ic04', 'ic05') and read_png(file) != read_png(iconset / file.name):
            raise ValueError('ICNS round trip changed ' + file.name)


def write_ico(pngs, output):
    container = bytearray(struct.pack('<HHH', 0, 1, len(pngs)))
    offset = 6 + 16 * len(pngs)
    for size, payload in pngs:
        container.extend(struct.pack('<BBBBHHII', size if size < 256 else 0,
                                     size if size < 256 else 0, 0, 0, 1, 32, len(payload), offset))
        offset += len(payload)
    for _, payload in pngs:
        container.extend(payload)
    output.write_bytes(container)


def update_manifest(build, generated):
    manifest_file = build / 'brand-icons.json'
    manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else []
    entries = {}
    for file in generated:
        entry = {'file': str(file.relative_to(ROOT)),
                 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
        if file.suffix == '.png':
            width, height, *_ = struct.unpack('>IIBBBBB', file.read_bytes()[16:29])
            entry.update(width=width, height=height, alpha=True)
            if file.parent.name == 'icon.iconset' and file.name in ('icon_16x16.png', 'icon_32x32.png'):
                entry['rgba_sha256'] = hashlib.sha256(b''.join(read_png(file)[2])).hexdigest()
        elif file.suffix == '.ico':
            entry['sizes'] = list(WINDOWS_SIZES)
        else:
            entry['sizes'] = [16, 32, 64, 128, 256, 512, 1024]
        entries[entry['file']] = entry
    updated = [entries.pop(entry['file'], entry) for entry in manifest]
    updated.extend(entries.values())
    manifest_file.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n')


def generate(build=None, source=None):
    build = build or ROOT / 'apps/desktop/build'
    path_data = LOGO_PATH
    if source:
        svg = ElementTree.parse(source).getroot()
        paths = svg.findall('{http://www.w3.org/2000/svg}path')
        if len(paths) != 1:
            raise ValueError('Expected the supplied single-path SVG wordmark')
        path_data = paths[0].attrib['d']
    generated = []
    with tempfile.TemporaryDirectory(prefix='luma-desktop-icons-') as directory:
        temp = Path(directory)
        staging = temp / 'build'
        staging.mkdir()
        renderer = temp / 'render-icon'
        environment = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(temp / 'clang-cache'),
                           SWIFT_MODULECACHE_PATH=str(temp / 'swift-cache'))
        subprocess.run(['swiftc', str(Path(__file__).with_name('render_icon.swift')),
                        '-o', str(renderer)], check=True, env=environment)
        mac = render(renderer, temp, 'macos', icon_spec(path_data))
        windows = render(renderer, temp, 'windows', icon_spec(path_data, windows=True))
        small = render(renderer, temp, 'windows-small', icon_spec(path_data, windows=True, small=True))
        shutil.copyfile(mac, staging / 'icon-macos-1024.png')
        resize(mac, staging / 'icon.png', 512, 512, alpha=True)
        iconset = staging / 'icon.iconset'
        for _, points, scale in ICONSET:
            file = iconset / iconset_name(points, scale)
            resize(mac, file, points * scale, points * scale, alpha=True, dpi=72 * scale)
            verify_png(file, points * scale)
        write_icns(iconset, staging / 'icon.icns', temp)
        pngs = []
        for size in WINDOWS_SIZES:
            file = staging / 'icon-windows-{}.png'.format(size)
            resize(small if size <= 32 else windows, file, size, size, alpha=True)
            verify_png(file, size)
            pngs.append((size, file.read_bytes()))
        write_ico(pngs, staging / 'icon.ico')
        for file in sorted(staging.rglob('*')):
            if file.is_file():
                output = build / file.relative_to(staging)
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, output)
                generated.append(output)
    print('Generated {} desktop icon files; corners alpha=0, centers alpha=255.'.format(len(generated)))
    return generated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Optional replacement for the embedded blue SVG')
    args = parser.parse_args()
    generated = generate(source=args.source)
    update_manifest(ROOT / 'apps/desktop/build', generated)


if __name__ == '__main__':
    main()
