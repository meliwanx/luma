const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')

const { electronBuilderConfig, loadBrand } = require('../scripts/apply-brand')

const desktopDir = path.resolve(__dirname, '..')
const pkg = JSON.parse(fs.readFileSync(path.join(desktopDir, 'package.json'), 'utf8'))
const build = electronBuilderConfig(loadBrand(path.join(desktopDir, 'brand.json'), {}))

test('packaged app includes every local runtime asset and keeps server configuration outside ASAR', () => {
  for (const filename of [
    'main.js', 'preload.js', 'config.example.json', 'brand.json', 'build/icon.png', 'build/icon.ico',
    'build/trayTemplate.png', 'build/trayTemplate@2x.png', 'build/tray.png', 'build/tray@2x.png',
  ]) {
    assert.ok(build.files.includes(filename), `${filename}: packaged runtime asset`)
    assert.ok(fs.statSync(path.join(desktopDir, filename)).isFile(), `${filename}: asset exists`)
  }
  assert.equal(build.files.includes('config.local.json'), false)
  assert.deepEqual(build.extraResources, [
    { from: 'config.local.json', to: 'config.json' },
    { from: 'brand.json', to: 'brand.json' },
  ])
  assert.equal(Object.hasOwn(pkg, 'build'), false)
  assert.equal(pkg.scripts['dist:mac'], 'electron-builder --config electron-builder.config.js --mac --publish never')
  assert.equal(pkg.scripts['dist:win'], 'electron-builder --config electron-builder.config.js --win --publish never')
  const example = JSON.parse(fs.readFileSync(path.join(desktopDir, 'config.example.json'), 'utf8'))
  assert.deepEqual(Object.keys(example), ['serverUrl'])
  const server = new URL(example.serverUrl)
  assert.equal(example.serverUrl, 'http://localhost:8000')
  assert.equal(server.protocol, 'http:')
  assert.equal(server.username, '')
  assert.equal(server.password, '')
})

test('macOS distributions keep architectures distinct and sign app and helpers with microphone and JIT permissions', () => {
  assert.equal(build.appId, 'app.luma.desktop')
  assert.equal(build.productName, 'Luma')
  assert.equal(build.extraMetadata.productName, 'Luma')
  assert.equal(build.dmg.artifactName, 'Luma-${version}-${arch}.dmg')
  assert.deepEqual(build.mac.target.map(({ target }) => target).sort(), ['dmg', 'zip'])
  for (const { arch } of build.mac.target) assert.deepEqual([...arch].sort(), ['arm64', 'x64'])
  assert.ok(build.mac.artifactName.includes('${arch}'), 'architecture-specific artifacts cannot overwrite each other')
  assert.equal(build.copyright, '')
  assert.equal(Object.hasOwn(build.mac, 'identity'), false)
  assert.equal(build.mac.forceCodeSigning, true)
  assert.equal(build.mac.hardenedRuntime, true)
  assert.equal(build.mac.notarize, false)
  assert.equal(build.mac.extendInfo.NSMicrophoneUsageDescription, '用于语音输入，录音只用于转写成文字')
  for (const filename of [build.mac.entitlements, build.mac.entitlementsInherit]) {
    const entitlements = fs.readFileSync(path.join(desktopDir, filename), 'utf8')
    assert.match(entitlements, /<key>com\.apple\.security\.device\.audio-input<\/key>\s*<true\s*\/>/)
    assert.match(entitlements, /<key>com\.apple\.security\.cs\.allow-jit<\/key>\s*<true\s*\/>/)
    assert.doesNotMatch(entitlements, /disable-library-validation|allow-dyld-environment-variables|app-sandbox/)
  }
  assert.ok(fs.statSync(path.join(desktopDir, build.mac.icon)).isFile())
})

test('Windows installer offers an installation directory and creates a branded desktop shortcut', () => {
  assert.equal(build.win.target[0].target, 'nsis')
  assert.deepEqual(build.win.target[0].arch, ['x64'])
  assert.equal(build.win.artifactName, 'Luma-Setup-${version}.exe')
  assert.equal(build.nsis.oneClick, false)
  assert.equal(build.nsis.allowToChangeInstallationDirectory, true)
  assert.equal(build.nsis.createDesktopShortcut, true)
  assert.ok(fs.statSync(path.join(desktopDir, build.win.icon)).isFile())
})
