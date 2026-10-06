const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const zlib = require('node:zlib')

const root = path.resolve(__dirname, '../../..')
const build = path.join(root, 'apps/desktop/build')
const assets = path.join(root, 'apps/flutter/ios/Runner/Assets.xcassets')
const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])
const pngCache = new Map()
const pixelCache = new WeakMap()
const argbSizes = new Map([['ic04', 16], ['ic05', 32]])
const iconsetTypes = new Map([
  ['icon_16x16.png', 'ic04'], ['icon_16x16@2x.png', 'ic11'],
  ['icon_32x32.png', 'ic05'], ['icon_32x32@2x.png', 'ic12'],
  ['icon_128x128.png', 'ic07'], ['icon_128x128@2x.png', 'ic13'],
  ['icon_256x256.png', 'ic08'], ['icon_256x256@2x.png', 'ic14'],
  ['icon_512x512.png', 'ic09'], ['icon_512x512@2x.png', 'ic10'],
])
const crcTable = Array.from({ length: 256 }, (_, value) => {
  for (let bit = 0; bit < 8; bit += 1) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1
  return value >>> 0
})

function crc32(data) {
  let value = 0xffffffff
  for (const byte of data) value = crcTable[(value ^ byte) & 255] ^ (value >>> 8)
  return (value ^ 0xffffffff) >>> 0
}

function parsePng(data, name) {
  assert.ok(data.subarray(0, 8).equals(signature), `${name}: PNG signature`)
  let offset = 8
  let header
  let dpi
  let ended = false
  const compressed = []
  while (offset < data.length) {
    assert.ok(offset + 12 <= data.length, `${name}: complete chunk header`)
    const length = data.readUInt32BE(offset)
    const end = offset + length + 12
    assert.ok(end <= data.length, `${name}: complete chunk`)
    const type = data.toString('ascii', offset + 4, offset + 8)
    const payload = data.subarray(offset + 8, end - 4)
    assert.equal(data.readUInt32BE(end - 4), crc32(data.subarray(offset + 4, end - 4)), `${name}: ${type} CRC`)
    if (!header) {
      assert.equal(type, 'IHDR', `${name}: IHDR comes first`)
      assert.equal(length, 13)
      header = {
        width: payload.readUInt32BE(0), height: payload.readUInt32BE(4),
        depth: payload[8], color: payload[9], interlace: payload[12],
      }
      assert.equal(header.depth, 8, `${name}: 8-bit channels`)
      assert.ok([2, 6].includes(header.color), `${name}: RGB/RGBA`)
      assert.equal(payload[10], 0, `${name}: standard compression`)
      assert.equal(payload[11], 0, `${name}: standard filtering`)
      assert.equal(header.interlace, 0, `${name}: non-interlaced`)
    } else if (type === 'IHDR') assert.fail(`${name}: duplicate IHDR`)
    if (type === 'pHYs') {
      assert.equal(length, 9)
      assert.equal(payload[8], 1, `${name}: density in metres`)
      dpi = { x: payload.readUInt32BE(0) * 0.0254, y: payload.readUInt32BE(4) * 0.0254 }
    }
    if (type === 'IDAT') compressed.push(payload)
    if (type === 'tRNS') assert.fail(`${name}: RGB transparency chunk is unexpected`)
    if (type === 'IEND') {
      assert.equal(length, 0)
      assert.equal(end, data.length, `${name}: no trailing data`)
      ended = true
    }
    offset = end
  }
  assert.ok(ended && compressed.length, `${name}: complete image`)
  const channels = header.color === 6 ? 4 : 3
  const stride = header.width * channels
  const scanlines = zlib.inflateSync(Buffer.concat(compressed))
  assert.equal(scanlines.length, (stride + 1) * header.height, `${name}: decoded dimensions`)
  for (let y = 0; y < header.height; y += 1) assert.ok(scanlines[y * (stride + 1)] <= 4, `${name}: PNG filter`)
  return { ...header, dpi, channels, scanlines }
}

function parseIcns(data) {
  assert.ok(data.length >= 8, 'complete ICNS header')
  assert.equal(data.toString('ascii', 0, 4), 'icns')
  assert.equal(data.readUInt32BE(4), data.length)
  const images = new Map()
  let offset = 8
  while (offset < data.length) {
    assert.ok(offset + 8 <= data.length, 'complete ICNS chunk header')
    const type = data.toString('ascii', offset, offset + 4)
    const length = data.readUInt32BE(offset + 4)
    assert.ok(length >= 8 && offset + length <= data.length, 'bounded ICNS chunk')
    assert.equal(images.has(type), false, `duplicate ICNS ${type}`)
    images.set(type, data.subarray(offset + 8, offset + length))
    offset += length
  }
  assert.equal(offset, data.length, 'no trailing ICNS data')
  return images
}

function parseArgb(data, width, name) {
  assert.equal(data.toString('ascii', 0, 4), 'ARGB', `${name}: native ARGB signature`)
  const planeSize = width * width
  const planes = Buffer.alloc(planeSize * 4)
  let offset = 4
  for (let plane = 0; plane < 4; plane += 1) {
    let written = 0
    while (written < planeSize) {
      assert.ok(offset < data.length, `${name}: complete ARGB plane`)
      const control = data[offset++]
      const count = control < 128 ? control + 1 : control - 125
      assert.ok(written + count <= planeSize, `${name}: bounded ARGB plane run`)
      if (control < 128) {
        assert.ok(offset + count <= data.length, `${name}: complete ARGB literal`)
        data.copy(planes, plane * planeSize + written, offset, offset + count)
        offset += count
      } else {
        assert.ok(offset < data.length, `${name}: complete ARGB repeat`)
        planes.fill(data[offset++], plane * planeSize + written, plane * planeSize + written + count)
      }
      written += count
    }
  }
  assert.equal(offset, data.length, `${name}: no trailing ARGB data`)
  const rgba = Buffer.alloc(planeSize * 4)
  for (let index = 0; index < planeSize; index += 1) {
    rgba[index * 4] = planes[planeSize + index]
    rgba[index * 4 + 1] = planes[planeSize * 2 + index]
    rgba[index * 4 + 2] = planes[planeSize * 3 + index]
    rgba[index * 4 + 3] = planes[index]
  }
  const image = { width, height: width, depth: 8, color: 6, channels: 4, nativeArgb: true }
  pixelCache.set(image, rgba)
  return image
}

function iconsetType(file) {
  return iconsetTypes.get(path.relative(path.join(build, 'icon.iconset'), file))
}

function readBrandAsset(file) {
  try {
    return fs.readFileSync(file)
  } catch (error) {
    const type = iconsetType(file)
    if (error.code !== 'ENOENT' || !type || argbSizes.has(type)) throw error
    // The generated iconset is optional in a checkout; large and Retina ICNS entries retain the exact PNGs.
    const image = parseIcns(fs.readFileSync(path.join(build, 'icon.icns'))).get(type)
    assert.ok(image, `${path.basename(file)}: matching ICNS representation`)
    return image
  }
}

function png(file) {
  if (!pngCache.has(file)) {
    let image
    try {
      image = parsePng(readBrandAsset(file), path.relative(root, file))
    } catch (error) {
      const type = iconsetType(file)
      if (error.code !== 'ENOENT' || !argbSizes.has(type)) throw error
      const data = parseIcns(fs.readFileSync(path.join(build, 'icon.icns'))).get(type)
      assert.ok(data, `${path.basename(file)}: matching native ICNS representation`)
      image = parseArgb(data, argbSizes.get(type), `ICNS ${type}`)
    }
    pngCache.set(file, image)
  }
  return pngCache.get(file)
}

function pixels(image) {
  if (pixelCache.has(image)) return pixelCache.get(image)
  const stride = image.width * image.channels
  const decoded = Buffer.alloc(stride * image.height)
  for (let y = 0; y < image.height; y += 1) {
    const filter = image.scanlines[y * (stride + 1)]
    for (let x = 0; x < stride; x += 1) {
      const index = y * stride + x
      const left = x >= image.channels ? decoded[index - image.channels] : 0
      const above = y > 0 ? decoded[index - stride] : 0
      const corner = y > 0 && x >= image.channels ? decoded[index - stride - image.channels] : 0
      let predictor = 0
      if (filter === 1) predictor = left
      if (filter === 2) predictor = above
      if (filter === 3) predictor = Math.floor((left + above) / 2)
      if (filter === 4) {
        const estimate = left + above - corner
        const a = Math.abs(estimate - left), b = Math.abs(estimate - above), c = Math.abs(estimate - corner)
        predictor = a <= b && a <= c ? left : b <= c ? above : corner
      }
      decoded[index] = (image.scanlines[y * (stride + 1) + x + 1] + predictor) & 255
    }
  }
  pixelCache.set(image, decoded)
  return decoded
}

function pixel(image, x, y) {
  const offset = (y * image.width + x) * image.channels
  return Array.from(pixels(image).subarray(offset, offset + image.channels))
}

function transparentCorners(image, name) {
  assert.equal(image.color, 6, `${name}: RGBA canvas`)
  for (const [x, y] of [[0, 0], [image.width - 1, 0], [0, image.height - 1], [image.width - 1, image.height - 1]]) {
    assert.equal(pixel(image, x, y)[3], 0, `${name}: transparent corner ${x},${y}`)
  }
  assert.equal(pixel(image, Math.floor(image.width / 2), Math.floor(image.height / 2))[3], 255, `${name}: opaque centre`)
}

function inkBounds(image, predicate) {
  const data = pixels(image)
  let left = image.width, top = image.height, right = -1, bottom = -1, count = 0
  for (let y = 0; y < image.height; y += 1) {
    for (let x = 0; x < image.width; x += 1) {
      const offset = (y * image.width + x) * image.channels
      if (!predicate(data.subarray(offset, offset + image.channels))) continue
      left = Math.min(left, x); top = Math.min(top, y)
      right = Math.max(right, x); bottom = Math.max(bottom, y)
      count += 1
    }
  }
  assert.ok(count > 0, 'image contains the expected pixels')
  return { left, top, right, bottom, width: right - left + 1, height: bottom - top + 1, count }
}

function roundedCanvas(image, fraction, name) {
  transparentCorners(image, name)
  // Half coverage follows the geometric edge after resampling; shadows peak at alpha 46.
  const body = inkBounds(image, value => value[3] >= 128)
  // A couple of pixels allow for antialiasing and sips resampling at tiny sizes.
  assert.ok(Math.abs(body.width - image.width * fraction) <= 2, `${name}: body width and horizontal padding`)
  assert.ok(Math.abs(body.height - image.height * fraction) <= 2, `${name}: body height and vertical padding`)
  assert.ok(Math.abs(body.left - (image.width - body.width) / 2) <= 1, `${name}: horizontally centred body`)
  assert.ok(Math.abs(body.top - (image.height - body.height) / 2) <= 1, `${name}: vertically centred body`)
  if (image.width >= 128) {
    const inset = Math.round(image.width * (1 - fraction) / 2)
    const corner = pixel(image, inset, inset)
    assert.ok(corner[3] < 100, `${name}: body corner remains rounded`)
    assert.equal(pixel(image, Math.floor(image.width / 2), inset + 2)[3], 255, `${name}: solid top edge`)
  }
}

function blueCoverage(image) {
  const data = pixels(image)
  let coverage = 0
  for (let offset = 0; offset < data.length; offset += image.channels) {
    const [red, green, blue, alpha] = data.subarray(offset, offset + image.channels)
    // Include antialiased blue strokes; neutral backgrounds and shadows do not count.
    if (green - red >= 8 && blue - green >= 15) coverage += Math.min(1, (blue - red) / 198) * alpha / 255
  }
  return coverage / (image.width * image.height)
}

function size(image, width, height = width) {
  assert.equal(image.width, width)
  assert.equal(image.height, height)
}

function density(image, expected) {
  assert.ok(image.dpi, 'PNG includes pHYs density')
  assert.ok(Math.abs(image.dpi.x - expected) < 0.02)
  assert.ok(Math.abs(image.dpi.y - expected) < 0.02)
}

test('the brand manifest lists unique assets with matching hashes and PNG dimensions', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(build, 'brand-icons.json'), 'utf8'))
  const files = new Set()
  for (const entry of manifest) {
    assert.ok(entry.file.startsWith('apps/'), 'manifest paths remain in apps/')
    assert.equal(files.has(entry.file), false, `duplicate ${entry.file}`)
    files.add(entry.file)
    const filename = path.resolve(root, entry.file)
    assert.ok(filename.startsWith(root + path.sep))
    let data
    try {
      data = readBrandAsset(filename)
    } catch (error) {
      if (error.code !== 'ENOENT' || !argbSizes.has(iconsetType(filename))) throw error
      // Native 16/32px ICNS entries retain pixels, not the source PNG's compressed bytes.
    }
    if (data) assert.equal(crypto.createHash('sha256').update(data).digest('hex'), entry.sha256, entry.file)
    if (entry.file.endsWith('.png')) {
      const image = png(filename)
      size(image, entry.width, entry.height)
      assert.equal(image.color === 6, entry.alpha, entry.file)
      if (argbSizes.has(iconsetType(filename))) {
        assert.match(entry.rgba_sha256, /^[a-f0-9]{64}$/, `${entry.file}: raw RGBA checksum`)
        assert.equal(crypto.createHash('sha256').update(pixels(image)).digest('hex'), entry.rgba_sha256, `${entry.file}: pixel checksum`)
      }
    }
  }
  for (const filename of ['icon.png', 'icon-macos-1024.png', 'icon.icns', 'icon.ico', 'trayTemplate.png', 'trayTemplate@2x.png', 'tray.png', 'tray@2x.png',
    ...[16, 24, 32, 48, 64, 128, 256].map(size => `icon-windows-${size}.png`)]) {
    assert.ok(files.has(`apps/desktop/build/${filename}`), filename)
  }
  for (const filename of ['favicon.svg', 'favicon-32.png', 'apple-touch-icon.png']) {
    assert.ok(files.has(`apps/web/public/${filename}`), filename)
  }
})

test('every iOS AppIcon JSON entry matches its pixel dimensions and has no alpha', () => {
  const folder = path.join(assets, 'AppIcon.appiconset')
  const catalog = JSON.parse(fs.readFileSync(path.join(folder, 'Contents.json'), 'utf8'))
  let marketing = 0
  for (const entry of catalog.images) {
    const [width, height] = entry.size.split('x').map(Number)
    const scale = Number(entry.scale.replace('x', ''))
    const image = png(path.join(folder, entry.filename))
    size(image, Math.round(width * scale), Math.round(height * scale))
    assert.equal(image.color, 2, `${entry.filename}: RGB without alpha`)
    if (entry.idiom === 'ios-marketing') { size(image, 1024); marketing += 1 }
  }
  assert.equal(marketing, 1)
})

test('desktop iconset includes every standard scale, plus correctly sized Web and launch images', () => {
  size(png(path.join(build, 'icon.png')), 512)
  for (const points of [16, 32, 128, 256, 512]) {
    for (const scale of [1, 2]) {
      const name = `icon_${points}x${points}${scale === 2 ? '@2x' : ''}.png`
      const image = png(path.join(build, 'icon.iconset', name))
      size(image, points * scale)
      if (!image.nativeArgb) density(image, 72 * scale)
    }
  }
  size(png(path.join(root, 'apps/web/public/favicon-32.png')), 32)
  size(png(path.join(root, 'apps/web/public/apple-touch-icon.png')), 180)
  const launchFolder = path.join(assets, 'LaunchImage.imageset')
  const catalog = JSON.parse(fs.readFileSync(path.join(launchFolder, 'Contents.json'), 'utf8'))
  const base = png(path.join(launchFolder, 'LaunchImage.png'))
  for (const entry of catalog.images) {
    const scale = Number(entry.scale.replace('x', ''))
    const image = png(path.join(launchFolder, entry.filename))
    size(image, base.width * scale, Math.round(base.height * scale))
    assert.equal(image.color, 6, `${entry.filename}: transparent wordmark`)
  }
})

test('macOS app icons have transparent margins, a smooth light squircle and a centred blue wordmark', () => {
  const master = png(path.join(build, 'icon-macos-1024.png'))
  size(master, 1024)
  roundedCanvas(master, 824 / 1024, 'macOS 1024px')
  const desktop = png(path.join(build, 'icon.png'))
  size(desktop, 512)
  roundedCanvas(desktop, 824 / 1024, 'macOS 512px')
  for (const image of [master, desktop]) {
    for (let position = 0; position < image.width; position += 1) {
      assert.equal(pixel(image, position, 0)[3], 0, 'transparent top canvas edge')
      assert.equal(pixel(image, position, image.height - 1)[3], 0, 'transparent bottom canvas edge')
      assert.equal(pixel(image, 0, position)[3], 0, 'transparent left canvas edge')
      assert.equal(pixel(image, image.width - 1, position)[3], 0, 'transparent right canvas edge')
    }
  }
  const top = pixel(master, 512, 180), bottom = pixel(master, 512, 860)
  for (const value of [top, bottom]) {
    assert.equal(value[3], 255)
    assert.ok(value[0] >= 243 && value[1] >= 246 && value[2] >= 251, 'white or very light background')
  }
  for (let channel = 0; channel < 3; channel += 1) assert.ok(top[channel] >= bottom[channel], 'subtle vertical gradient')
  assert.ok(pixel(master, 141, 141)[3] < 100, 'squircle outer diagonal is transparent apart from light shadow')
  assert.equal(pixel(master, 265, 265)[3], 255, 'squircle inner diagonal is opaque')
  const blue = inkBounds(master, value => value[3] === 255 && value[0] === 37 && value[1] === 99 && value[2] === 235)
  assert.ok(Math.abs(blue.width - 824 * 0.72) < 12, 'wordmark is approximately 72% of the body width')
  assert.ok(Math.abs((blue.left + blue.right) / 2 - 511.5) < 3, 'centred wordmark ink horizontally')
  assert.ok(Math.abs((blue.top + blue.bottom) / 2 - 511.5) < 3, 'centred wordmark ink vertically')
  const shadow = pixel(master, 512, 944), fade = pixel(master, 512, 984)
  assert.deepEqual(shadow.slice(0, 3), [0, 0, 0], 'neutral black shadow')
  assert.ok(shadow[3] > 0 && shadow[3] <= 46, 'light shadow stays below 18% opacity')
  assert.ok(fade[3] < shadow[3], 'shadow fades within the transparent canvas')
})

test('macOS tray templates are black with real transparency and correct 1x/2x pixel density', () => {
  for (const scale of [1, 2]) {
    const filename = `trayTemplate${scale === 2 ? '@2x' : ''}.png`
    const image = png(path.join(build, filename))
    size(image, 32 * scale, 16 * scale)
    assert.equal(image.color, 6)
    density(image, 72 * scale)
    const data = pixels(image)
    let visible = 0
    let transparent = 0
    for (let offset = 0; offset < data.length; offset += 4) {
      if (data[offset + 3]) {
        assert.deepEqual(Array.from(data.subarray(offset, offset + 3)), [0, 0, 0], filename)
        visible += 1
      } else transparent += 1
    }
    assert.ok(visible > 16 * scale, `${filename}: visible strokes`)
    assert.ok(transparent > 0, `${filename}: transparent canvas`)
  }
})

test('Windows tray images use the brand blue and transparent 16/32 px representations', () => {
  for (const scale of [1, 2]) {
    const filename = `tray${scale === 2 ? '@2x' : ''}.png`
    const image = png(path.join(build, filename))
    size(image, 16 * scale)
    assert.equal(image.color, 6)
    density(image, 72 * scale)
    const data = pixels(image)
    let visible = 0
    let transparent = 0
    for (let offset = 0; offset < data.length; offset += 4) {
      if (data[offset + 3]) {
        assert.deepEqual(Array.from(data.subarray(offset, offset + 3)), [37, 99, 235], filename)
        visible += 1
      } else transparent += 1
    }
    assert.ok(visible > 0 && transparent > 0)
  }
})

test('Windows ICO has seven non-overlapping RGBA32 PNG entries and correct directory dimensions', () => {
  const data = fs.readFileSync(path.join(build, 'icon.ico'))
  assert.equal(data.readUInt16LE(0), 0)
  assert.equal(data.readUInt16LE(2), 1)
  const sizes = [16, 24, 32, 48, 64, 128, 256]
  assert.equal(data.readUInt16LE(4), sizes.length)
  let expectedOffset = 6 + sizes.length * 16
  for (let index = 0; index < sizes.length; index += 1) {
    const position = 6 + index * 16
    const width = data[position] || 256
    const height = data[position + 1] || 256
    assert.equal(width, sizes[index])
    assert.equal(height, sizes[index])
    assert.equal(data[position + 2], 0)
    assert.equal(data[position + 3], 0)
    assert.equal(data.readUInt16LE(position + 4), 1)
    assert.equal(data.readUInt16LE(position + 6), 32)
    const length = data.readUInt32LE(position + 8)
    const offset = data.readUInt32LE(position + 12)
    assert.equal(offset, expectedOffset)
    assert.ok(offset + length <= data.length)
    const image = parsePng(data.subarray(offset, offset + length), `ICO ${width}px`)
    size(image, width, height)
    assert.equal(image.color, 6, 'PNG ICO payload must be RGBA32')
    roundedCanvas(image, 0.88, `ICO ${width}px`)
    const source = fs.readFileSync(path.join(build, `icon-windows-${width}.png`))
    assert.ok(data.subarray(offset, offset + length).equals(source), `ICO ${width}px: preserves the generated Windows PNG`)
    expectedOffset += length
  }
  assert.equal(expectedOffset, data.length)
})

test('Windows app icons keep small strokes readable without a shadow and use a light shadow at larger sizes', () => {
  const baseline = blueCoverage(png(path.join(build, 'icon-windows-128.png')))
  for (const width of [16, 24, 32, 48, 64, 128, 256]) {
    const image = png(path.join(build, `icon-windows-${width}.png`))
    size(image, width)
    roundedCanvas(image, 0.88, `Windows ${width}px`)
    const data = pixels(image)
    let shadowPixels = 0
    for (let offset = 0; offset < data.length; offset += image.channels) {
      if (data[offset + 3] && data[offset] < 80 && data[offset + 1] < 80 && data[offset + 2] < 80) shadowPixels += 1
    }
    if (width <= 32) {
      assert.equal(shadowPixels, 0, `${width}px: no dark shadow around small icons`)
      assert.ok(blueCoverage(image) > baseline * 1.08, `${width}px: thicker blue strokes than large representations`)
    } else assert.ok(shadowPixels > 0, `${width}px: restrained neutral shadow`)
  }
})

test('ICNS preserves native small ARGB icons and all eight large and Retina PNG representations', () => {
  const images = parseIcns(fs.readFileSync(path.join(build, 'icon.icns')))
  const expected = new Map([
    ['ic04', [16, 1]], ['ic11', [16, 2]], ['ic05', [32, 1]], ['ic12', [32, 2]],
    ['ic07', [128, 1]], ['ic13', [128, 2]], ['ic08', [256, 1]], ['ic14', [256, 2]],
    ['ic09', [512, 1]], ['ic10', [512, 2]],
  ])
  assert.equal(images.has('icp4'), false, '16px 1x uses native ARGB instead of legacy PNG')
  assert.equal(images.has('icp5'), false, '32px 1x uses native ARGB instead of legacy PNG')
  const found = new Set()
  for (const [type, data] of images) {
    if (expected.has(type)) {
      found.add(type)
      const [points, scale] = expected.get(type)
      const native = argbSizes.has(type)
      const image = native ? parseArgb(data, points, `ICNS ${type}`) : parsePng(data, `ICNS ${type}`)
      size(image, points * scale)
      if (!native) density(image, 72 * scale)
      roundedCanvas(image, 824 / 1024, `ICNS ${type}`)
      const filename = `icon_${points}x${points}${scale === 2 ? '@2x' : ''}.png`
      const source = path.join(build, 'icon.iconset', filename)
      if (native) assert.deepEqual(pixels(image), pixels(png(source)), `${type}: preserves straight RGBA source pixels`)
      else assert.ok(data.equals(readBrandAsset(source)), `${type}: preserves the iconset PNG`)
    }
  }
  assert.deepEqual(Array.from(found).sort(), Array.from(expected.keys()).sort())
})

test('native ICNS ARGB decoding handles literal and repeated runs and rejects malformed planes', () => {
  const data = Buffer.concat([Buffer.from('ARGB'), Buffer.from([
    3, 0, 255, 255, 128,
    129, 37,
    1, 99, 100, 1, 101, 102,
    128, 235, 0, 234,
  ])])
  assert.deepEqual(Array.from(pixels(parseArgb(data, 2, 'fixture'))), [
    37, 99, 235, 0, 37, 100, 235, 255,
    37, 101, 235, 255, 37, 102, 234, 128,
  ])
  const repeats = Buffer.concat([Buffer.from('ARGB'), ...[255, 37, 99, 235].map(value => Buffer.from([255, value, 251, value]))])
  assert.deepEqual(pixel(parseArgb(repeats, 16, 'maximum repeat fixture'), 15, 15), [37, 99, 235, 255])
  assert.throws(() => parseArgb(Buffer.from('RGBA'), 2, 'bad signature'), /native ARGB signature/)
  assert.throws(() => parseArgb(Buffer.concat([Buffer.from('ARGB'), Buffer.from([3, 0])]), 2, 'short literal'), /complete ARGB literal/)
  assert.throws(() => parseArgb(Buffer.concat([Buffer.from('ARGB'), Buffer.from([129])]), 2, 'short repeat'), /complete ARGB repeat/)
  assert.throws(() => parseArgb(Buffer.concat([Buffer.from('ARGB'), Buffer.from([4, 1, 2, 3, 4, 5])]), 2, 'long literal'), /bounded ARGB plane run/)
  assert.throws(() => parseArgb(Buffer.concat([Buffer.from('ARGB'), Buffer.from([130, 0])]), 2, 'long repeat'), /bounded ARGB plane run/)
  assert.throws(() => parseArgb(data.subarray(0, data.length - 2), 2, 'short plane'), /complete ARGB plane/)
  assert.throws(() => parseArgb(Buffer.concat([data, Buffer.from([0])]), 2, 'trailing data'), /no trailing ARGB data/)
})

test('ICNS parsing rejects truncated, oversized and duplicate chunks', () => {
  assert.throws(() => parseIcns(Buffer.from('icns')), /complete ICNS header/)
  const header = Buffer.from('69636e7300000010', 'hex')
  const chunk = Buffer.from('6963303400000008', 'hex')
  assert.equal(parseIcns(Buffer.concat([header, chunk])).size, 1)
  assert.throws(() => parseIcns(Buffer.concat([Buffer.from('69636e7300000009', 'hex'), Buffer.from([0])])), /complete ICNS chunk header/)
  assert.throws(() => parseIcns(Buffer.concat([header, Buffer.from('6963303400000009', 'hex')])), /bounded ICNS chunk/)
  assert.throws(() => parseIcns(Buffer.concat([header, Buffer.from('6963303400000007', 'hex')])), /bounded ICNS chunk/)
  assert.throws(() => parseIcns(Buffer.concat([Buffer.from('69636e7300000018', 'hex'), chunk, chunk])), /duplicate ICNS ic04/)
})
