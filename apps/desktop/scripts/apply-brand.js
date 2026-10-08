const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const DESKTOP_DIR = path.resolve(__dirname, '..')

const DEFAULT_BRAND = {
  productName: 'Luma',
  appId: 'app.luma.desktop',
  iconDir: 'build',
  trayTooltip: 'Luma',
  copyright: '',
  macIdentity: null,
}

const REQUIRED_ICONS = [
  'icon.icns',
  'icon.ico',
  'icon.png',
  'trayTemplate.png',
  'trayTemplate@2x.png',
  'tray.png',
  'tray@2x.png',
]

// icon.icns is consumed by electron-builder at pack time, not from the ASAR.
const PACKAGED_ICONS = [
  'icon.png',
  'icon.ico',
  'trayTemplate.png',
  'trayTemplate@2x.png',
  'tray.png',
  'tray@2x.png',
]

function resolveBrandFile(file, env = process.env) {
  if (file) return path.resolve(file)
  const fromEnv = env && typeof env.BRAND_FILE === 'string' ? env.BRAND_FILE.trim() : ''
  if (fromEnv) return path.resolve(fromEnv)
  return path.join(DESKTOP_DIR, 'brand.json')
}

function requireText(value, field, filename) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`Brand file ${filename} field ${field} must be a non-empty string`)
  }
  return value.trim()
}

function parseBrand(raw, filename) {
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch (error) {
    throw new Error(`Brand file ${filename} is not valid JSON: ${error.message}`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`Brand file ${filename} must be a JSON object`)
  }
  const brand = {
    productName: parsed.productName === undefined
      ? DEFAULT_BRAND.productName
      : requireText(parsed.productName, 'productName', filename),
    appId: parsed.appId === undefined
      ? DEFAULT_BRAND.appId
      : requireText(parsed.appId, 'appId', filename),
    iconDir: parsed.iconDir === undefined
      ? DEFAULT_BRAND.iconDir
      : requireText(parsed.iconDir, 'iconDir', filename),
    trayTooltip: parsed.trayTooltip === undefined
      ? DEFAULT_BRAND.trayTooltip
      : requireText(parsed.trayTooltip, 'trayTooltip', filename),
    copyright: parsed.copyright === undefined ? DEFAULT_BRAND.copyright : parsed.copyright,
    macIdentity: parsed.macIdentity === undefined ? DEFAULT_BRAND.macIdentity : parsed.macIdentity,
    brandFile: filename,
  }
  if (typeof brand.copyright !== 'string') {
    throw new Error(`Brand file ${filename} field copyright must be a string`)
  }
  if (brand.macIdentity !== null && (typeof brand.macIdentity !== 'string' || !brand.macIdentity.trim())) {
    throw new Error(`Brand file ${filename} field macIdentity must be a non-empty string or null`)
  }
  if (typeof brand.macIdentity === 'string') brand.macIdentity = brand.macIdentity.trim()
  return brand
}

function loadBrand(file, env = process.env) {
  const filename = resolveBrandFile(file, env)
  let raw
  try {
    raw = fs.readFileSync(filename, 'utf8')
  } catch (error) {
    if (error && error.code === 'ENOENT') {
      throw new Error(`Cannot read brand file ${filename}: file not found. Set BRAND_FILE or add brand.json next to the desktop app.`)
    }
    throw new Error(`Cannot read brand file ${filename}: ${error.message}`)
  }
  return parseBrand(raw, filename)
}

function readRuntimeBrand({ fs: fsImpl, env, dirname, resourcesPath }) {
  const candidates = []
  const fromEnv = env && typeof env.BRAND_FILE === 'string' ? env.BRAND_FILE.trim() : ''
  if (fromEnv) candidates.push(path.resolve(fromEnv))
  else {
    if (resourcesPath) candidates.push(path.join(resourcesPath, 'brand.json'))
    candidates.push(path.join(dirname, 'brand.json'))
  }
  const errors = []
  for (const filename of candidates) {
    let raw
    try {
      raw = fsImpl.readFileSync(filename, 'utf8')
    } catch (error) {
      errors.push(`${filename}: ${error.message}`)
      continue
    }
    return parseBrand(raw, filename)
  }
  throw new Error(`Cannot read desktop brand config. Tried ${errors.join('; ')}`)
}

function resolveIconDir(brand) {
  if (path.isAbsolute(brand.iconDir)) return path.resolve(brand.iconDir)
  const base = brand.brandFile ? path.dirname(path.resolve(brand.brandFile)) : DESKTOP_DIR
  return path.resolve(base, brand.iconDir)
}

function resolveIconFile(brand, name, locations) {
  if (path.isAbsolute(brand.iconDir)) return path.join(brand.iconDir, name)
  const dirname = locations && locations.dirname
  const resourcesPath = locations && locations.resourcesPath
  const brandFile = brand.brandFile ? path.resolve(brand.brandFile) : ''
  const packaged = Boolean(resourcesPath) && brandFile === path.resolve(resourcesPath, 'brand.json')
  const besideApp = Boolean(dirname) && brandFile === path.resolve(dirname, 'brand.json')
  if (packaged || besideApp || !brandFile) return path.join(dirname, brand.iconDir, name)
  return path.join(path.dirname(brandFile), brand.iconDir, name)
}

function assertIcons(iconDir) {
  const expected = REQUIRED_ICONS.join(', ')
  let stats
  try {
    stats = fs.statSync(iconDir)
  } catch {
    throw new Error(`Icon directory not found: ${iconDir}. Brand iconDir must point at a directory containing ${expected}.`)
  }
  if (!stats.isDirectory()) {
    throw new Error(`Icon path is not a directory: ${iconDir}. Brand iconDir must point at a directory containing ${expected}.`)
  }
  const missing = REQUIRED_ICONS.filter((name) => {
    try {
      return !fs.statSync(path.join(iconDir, name)).isFile()
    } catch {
      return true
    }
  })
  if (missing.length) {
    throw new Error(`Icon directory ${iconDir} is missing ${missing.join(', ')}. Expected ${expected}.`)
  }
}

function projectRelative(target) {
  const relative = path.relative(DESKTOP_DIR, target)
  if (!relative) return null
  const parts = relative.split(path.sep)
  if (parts.includes('..')) return null
  return parts.join('/')
}

function writePackagedBrand(brand, runtimeIconDir) {
  const payload = {
    productName: brand.productName,
    appId: brand.appId,
    iconDir: runtimeIconDir,
    trayTooltip: brand.trayTooltip,
    copyright: brand.copyright,
    macIdentity: brand.macIdentity,
  }
  const defaultFile = path.join(DESKTOP_DIR, 'brand.json')
  if (path.resolve(brand.brandFile) === defaultFile && brand.iconDir === runtimeIconDir) return 'brand.json'
  const destination = path.join(os.tmpdir(), `oc-desktop-brand-${crypto.randomBytes(8).toString('hex')}.json`)
  fs.writeFileSync(destination, `${JSON.stringify(payload, null, 2)}\n`)
  return destination
}

function electronBuilderConfig(brand = loadBrand()) {
  const resolved = {
    ...DEFAULT_BRAND,
    ...brand,
    brandFile: brand.brandFile || path.join(DESKTOP_DIR, 'brand.json'),
  }
  const iconDirAbs = resolveIconDir(resolved)
  assertIcons(iconDirAbs)
  const relative = projectRelative(iconDirAbs)
  const runtimeIconDir = relative || 'build'
  const iconPath = (name) => (relative ? `${relative}/${name}` : path.join(iconDirAbs, name))
  const mac = {
    target: [
      { target: 'dmg', arch: ['arm64', 'x64'] },
      { target: 'zip', arch: ['arm64', 'x64'] },
    ],
    artifactName: `${resolved.productName}-\${version}-\${arch}.\${ext}`,
    icon: iconPath('icon.icns'),
    category: 'public.app-category.productivity',
    forceCodeSigning: true,
    hardenedRuntime: true,
    entitlements: 'build/entitlements.mac.plist',
    entitlementsInherit: 'build/entitlements.mac.plist',
    notarize: false,
    extendInfo: {
      NSMicrophoneUsageDescription: '用于语音输入，录音只用于转写成文字',
    },
  }
  // Null omits mac.identity. electron-builder treats an explicit null as "do not sign",
  // which would ignore CSC_NAME / CSC_LINK. A string pins the certificate name.
  if (resolved.macIdentity) mac.identity = resolved.macIdentity
  return {
    appId: resolved.appId,
    productName: resolved.productName,
    copyright: resolved.copyright,
    extraMetadata: { productName: resolved.productName },
    directories: { buildResources: 'build', output: 'dist' },
    files: [
      'main.js',
      'preload.js',
      'config.example.json',
      'brand.json',
      ...(relative
        ? PACKAGED_ICONS.map((name) => `${relative}/${name}`)
        : [{ from: iconDirAbs, to: runtimeIconDir, filter: PACKAGED_ICONS.slice() }]),
    ],
    extraResources: [
      { from: 'config.local.json', to: 'config.json' },
      { from: writePackagedBrand(resolved, runtimeIconDir), to: 'brand.json' },
    ],
    mac,
    dmg: { artifactName: `${resolved.productName}-\${version}-\${arch}.dmg` },
    win: {
      target: [{ target: 'nsis', arch: ['x64'] }],
      icon: iconPath('icon.ico'),
      artifactName: `${resolved.productName}-Setup-\${version}.exe`,
    },
    nsis: {
      oneClick: false,
      allowToChangeInstallationDirectory: true,
      createDesktopShortcut: true,
    },
  }
}

module.exports = {
  DEFAULT_BRAND,
  DESKTOP_DIR,
  PACKAGED_ICONS,
  REQUIRED_ICONS,
  assertIcons,
  electronBuilderConfig,
  loadBrand,
  parseBrand,
  readRuntimeBrand,
  resolveBrandFile,
  resolveIconDir,
  resolveIconFile,
}
