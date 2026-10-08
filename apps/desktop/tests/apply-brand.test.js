const assert = require('node:assert/strict')
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const desktopDir = path.resolve(__dirname, '..')
const {
  DEFAULT_BRAND, PACKAGED_ICONS, REQUIRED_ICONS, electronBuilderConfig, loadBrand, readRuntimeBrand, resolveBrandFile, resolveIconFile,
} = require('../scripts/apply-brand')

function temporaryDirectory() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'oc-brand-'))
}

function writeIcons(directory) {
  for (const name of REQUIRED_ICONS) fs.writeFileSync(path.join(directory, name), name)
}

test('the default brand file supplies the open-source Luma values', () => {
  const brand = loadBrand(path.join(desktopDir, 'brand.json'), {})
  assert.equal(brand.productName, 'Luma')
  assert.equal(brand.appId, 'app.luma.desktop')
  assert.equal(brand.iconDir, 'build')
  assert.equal(brand.trayTooltip, 'Luma')
  assert.equal(brand.copyright, '')
  assert.equal(brand.macIdentity, null)
  assert.deepEqual(
    { ...DEFAULT_BRAND, brandFile: brand.brandFile },
    { ...brand },
  )
  const config = electronBuilderConfig(brand)
  assert.equal(config.productName, brand.productName)
  assert.equal(config.appId, brand.appId)
  assert.equal(config.extraMetadata.productName, brand.productName)
  assert.equal(config.copyright, '')
  assert.equal(Object.hasOwn(config.mac, 'identity'), false)
  assert.equal(config.mac.artifactName, 'Luma-${version}-${arch}.${ext}')
  assert.equal(config.dmg.artifactName, 'Luma-${version}-${arch}.dmg')
  assert.equal(config.win.artifactName, 'Luma-Setup-${version}.exe')
  assert.equal(config.mac.icon, 'build/icon.icns')
  assert.equal(config.win.icon, 'build/icon.ico')
})

test('omitted brand fields keep the defaults and an explicit file wins over BRAND_FILE', () => {
  const directory = temporaryDirectory()
  try {
    const partial = path.join(directory, 'partial.json')
    const explicit = path.join(directory, 'explicit.json')
    fs.writeFileSync(partial, JSON.stringify({ productName: 'Custom' }))
    fs.writeFileSync(explicit, JSON.stringify({ productName: 'Explicit', appId: 'app.explicit.desktop' }))
    const brand = loadBrand(partial, { BRAND_FILE: explicit })
    assert.equal(brand.productName, 'Custom')
    assert.equal(brand.appId, DEFAULT_BRAND.appId)
    assert.equal(brand.iconDir, DEFAULT_BRAND.iconDir)
    assert.equal(brand.trayTooltip, DEFAULT_BRAND.trayTooltip)
    assert.equal(brand.copyright, '')
    assert.equal(brand.macIdentity, null)
    assert.equal(resolveBrandFile(undefined, { BRAND_FILE: `  ${explicit}  ` }), explicit)
    assert.equal(resolveBrandFile(undefined, { BRAND_FILE: '   ' }), path.join(desktopDir, 'brand.json'))
    assert.equal(loadBrand(undefined, { BRAND_FILE: explicit }).productName, 'Explicit')
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

test('a brand override changes productName, appId, artifact names, identity and icons', () => {
  const directory = temporaryDirectory()
  const outside = temporaryDirectory()
  const created = []
  try {
    writeIcons(outside)
    const brandFile = path.join(directory, 'brand.json')
    fs.writeFileSync(brandFile, JSON.stringify({
      productName: 'Northstar',
      appId: 'app.northstar.desktop',
      iconDir: outside,
      trayTooltip: 'Northstar ready',
      copyright: 'Copyright Northstar',
      macIdentity: 'Northstar Cert',
    }))
    const config = electronBuilderConfig(loadBrand(brandFile, {}))
    assert.equal(config.productName, 'Northstar')
    assert.equal(config.appId, 'app.northstar.desktop')
    assert.equal(config.extraMetadata.productName, 'Northstar')
    assert.equal(config.copyright, 'Copyright Northstar')
    assert.equal(config.mac.identity, 'Northstar Cert')
    assert.equal(config.win.artifactName, 'Northstar-Setup-${version}.exe')
    assert.equal(config.dmg.artifactName, 'Northstar-${version}-${arch}.dmg')
    assert.equal(config.mac.artifactName, 'Northstar-${version}-${arch}.${ext}')
    assert.equal(config.mac.icon, path.join(outside, 'icon.icns'))
    assert.equal(config.win.icon, path.join(outside, 'icon.ico'))
    assert.deepEqual(config.files[config.files.length - 1], { from: outside, to: 'build', filter: PACKAGED_ICONS })
    const packaged = config.extraResources.find((item) => item.to === 'brand.json')
    created.push(packaged.from)
    const packagedBrand = JSON.parse(fs.readFileSync(packaged.from, 'utf8'))
    assert.equal(packagedBrand.productName, 'Northstar')
    assert.equal(packagedBrand.appId, 'app.northstar.desktop')
    assert.equal(packagedBrand.iconDir, 'build')
    assert.equal(packagedBrand.trayTooltip, 'Northstar ready')
    assert.equal(packagedBrand.macIdentity, 'Northstar Cert')
  } finally {
    for (const filename of created) fs.rmSync(filename, { force: true })
    fs.rmSync(directory, { recursive: true, force: true })
    fs.rmSync(outside, { recursive: true, force: true })
  }
})

test('a missing icon directory or missing icon file names the path and the required files', () => {
  const missing = path.join(os.tmpdir(), 'oc-brand-missing-icons')
  assert.throws(
    () => electronBuilderConfig({ ...DEFAULT_BRAND, brandFile: path.join(desktopDir, 'brand.json'), iconDir: missing }),
    (error) => {
      assert.ok(error.message.includes(`Icon directory not found: ${missing}`), error.message)
      for (const name of REQUIRED_ICONS) assert.ok(error.message.includes(name), error.message)
      return true
    },
  )
  const directory = temporaryDirectory()
  try {
    const file = path.join(directory, 'not-a-directory')
    fs.writeFileSync(file, '')
    assert.throws(
      () => electronBuilderConfig({ ...DEFAULT_BRAND, brandFile: directory, iconDir: file }),
      /Icon path is not a directory/,
    )
    fs.writeFileSync(path.join(directory, 'icon.png'), '')
    assert.throws(
      () => electronBuilderConfig({ ...DEFAULT_BRAND, brandFile: path.join(directory, 'brand.json'), iconDir: directory }),
      (error) => {
        assert.match(error.message, /is missing/)
        assert.ok(error.message.includes(directory), error.message)
        assert.match(error.message, /icon\.icns/)
        assert.match(error.message, /trayTemplate@2x\.png/)
        return true
      },
    )
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

test('runtime brand lookup prefers BRAND_FILE, then resources, then the app directory', () => {
  const files = new Map()
  const fakeFs = {
    readFileSync(filename) {
      if (!files.has(filename)) {
        const error = new Error('missing')
        error.code = 'ENOENT'
        throw error
      }
      return files.get(filename)
    },
  }
  const dirname = path.join(desktopDir, 'app')
  const resourcesPath = path.join(desktopDir, 'resources')
  const external = path.join(desktopDir, 'external', 'brand.json')
  files.set(external, JSON.stringify({ productName: 'External', iconDir: 'icons' }))
  files.set(path.join(resourcesPath, 'brand.json'), JSON.stringify({ productName: 'Packaged', trayTooltip: 'Packed' }))
  files.set(path.join(dirname, 'brand.json'), JSON.stringify({ productName: 'Local' }))

  const fromEnv = readRuntimeBrand({ fs: fakeFs, env: { BRAND_FILE: external }, dirname, resourcesPath })
  assert.equal(fromEnv.productName, 'External')
  assert.equal(fromEnv.appId, DEFAULT_BRAND.appId)
  assert.equal(resolveIconFile(fromEnv, 'icon.png', { dirname, resourcesPath }), path.join(desktopDir, 'external', 'icons', 'icon.png'))

  const packaged = readRuntimeBrand({ fs: fakeFs, env: {}, dirname, resourcesPath })
  assert.equal(packaged.productName, 'Packaged')
  assert.equal(packaged.trayTooltip, 'Packed')
  assert.equal(resolveIconFile(packaged, 'icon.png', { dirname, resourcesPath }), path.join(dirname, 'build', 'icon.png'))

  files.delete(path.join(resourcesPath, 'brand.json'))
  const local = readRuntimeBrand({ fs: fakeFs, env: {}, dirname, resourcesPath })
  assert.equal(local.productName, 'Local')
  assert.equal(local.trayTooltip, 'Luma')
  assert.equal(resolveIconFile(local, 'tray.png', { dirname, resourcesPath }), path.join(dirname, 'build', 'tray.png'))

  files.delete(path.join(dirname, 'brand.json'))
  assert.throws(() => readRuntimeBrand({ fs: fakeFs, env: {}, dirname, resourcesPath }), /Cannot read desktop brand config/)
  files.set(external, '{')
  assert.throws(() => readRuntimeBrand({ fs: fakeFs, env: { BRAND_FILE: external }, dirname, resourcesPath }), /not valid JSON/)
})

test('invalid brand files report the field or file that failed', () => {
  const directory = temporaryDirectory()
  try {
    const filename = path.join(directory, 'brand.json')
    fs.writeFileSync(filename, '{')
    assert.throws(() => loadBrand(filename, {}), /not valid JSON/)
    fs.writeFileSync(filename, '[]')
    assert.throws(() => loadBrand(filename, {}), /must be a JSON object/)
    fs.writeFileSync(filename, JSON.stringify({ productName: '  ' }))
    assert.throws(() => loadBrand(filename, {}), /productName must be a non-empty string/)
    fs.writeFileSync(filename, JSON.stringify({ macIdentity: '' }))
    assert.throws(() => loadBrand(filename, {}), /macIdentity must be a non-empty string or null/)
    assert.throws(() => loadBrand(path.join(directory, 'missing.json'), {}), /file not found/)
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

test('electron-builder.config.js reads the default brand and BRAND_FILE', () => {
  const configModule = path.join(desktopDir, 'electron-builder.config.js')
  const directory = temporaryDirectory()
  const outside = temporaryDirectory()
  try {
    writeIcons(outside)
    const brandFile = path.join(directory, 'brand.json')
    fs.writeFileSync(brandFile, JSON.stringify({
      productName: 'Northstar',
      appId: 'app.northstar.desktop',
      iconDir: outside,
      trayTooltip: 'Northstar',
      copyright: 'Copyright Northstar',
      macIdentity: 'Northstar Cert',
    }))
    const run = (env) => spawnSync(process.execPath, ['-e', `
      const config = require(${JSON.stringify(configModule)})
      const packaged = config.extraResources.find((item) => item.to === 'brand.json')
      console.log(JSON.stringify({
        productName: config.productName,
        appId: config.appId,
        identity: Object.hasOwn(config.mac, 'identity') ? config.mac.identity : null,
        win: config.win.artifactName,
        dmg: config.dmg.artifactName,
        extra: config.extraMetadata.productName,
        brandFrom: packaged.from,
      }))
    `], { encoding: 'utf8', env: { ...process.env, ...env } })
    const defaults = run({ BRAND_FILE: '' })
    assert.equal(defaults.status, 0, defaults.stderr)
    assert.deepEqual(JSON.parse(defaults.stdout), {
      productName: 'Luma',
      appId: 'app.luma.desktop',
      identity: null,
      win: 'Luma-Setup-${version}.exe',
      dmg: 'Luma-${version}-${arch}.dmg',
      extra: 'Luma',
      brandFrom: 'brand.json',
    })
    const override = run({ BRAND_FILE: brandFile })
    assert.equal(override.status, 0, override.stderr)
    const parsed = JSON.parse(override.stdout)
    assert.equal(parsed.productName, 'Northstar')
    assert.equal(parsed.appId, 'app.northstar.desktop')
    assert.equal(parsed.identity, 'Northstar Cert')
    assert.equal(parsed.win, 'Northstar-Setup-${version}.exe')
    assert.equal(parsed.dmg, 'Northstar-${version}-${arch}.dmg')
    assert.equal(parsed.extra, 'Northstar')
    if (parsed.brandFrom !== 'brand.json') fs.rmSync(parsed.brandFrom, { force: true })
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
    fs.rmSync(outside, { recursive: true, force: true })
  }
})
