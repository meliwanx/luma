const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const desktopDir = path.resolve(__dirname, '..')
const resourcesDir = path.join(desktopDir, 'test-resources')
const brandResource = path.join(resourcesDir, 'brand.json')
const brandLocal = path.join(desktopDir, 'brand.json')

async function loadMain(platform = 'darwin', options = {}) {
  const windows = []
  const trays = []
  const images = []
  const handlers = new Map()
  const permissions = {}
  const shortcut = { registerCalls: [], unregisterCalls: 0 }
  const startupCalls = []
  const configReads = []
  const app = new EventEmitter()
  Object.assign(app, {
    commandLine: { appendSwitch() {} },
    name: 'Electron',
    setName: (name) => { app.name = name; startupCalls.push('setName') },
    whenReady: () => { startupCalls.push('whenReady'); return Promise.resolve() },
    getName: () => app.name,
    getVersion: () => '0.1.0',
    getLoginItemSettings: () => ({ openAtLogin: Boolean(app.openAtLogin) }),
    setLoginItemSettings: (settings) => { app.openAtLogin = settings.openAtLogin },
    quit: () => {
      app.quitCalls = (app.quitCalls || 0) + 1
      app.emit('before-quit')
      for (const window of windows) if (!window.destroyed) window.close()
    },
  })
  if (platform === 'darwin') {
    app.dock = { setIcon: (filename) => { app.dockIcon = filename; startupCalls.push('dockIcon') } }
  }
  class BrowserWindow extends EventEmitter {
    constructor(config) {
      super()
      this.config = config
      this.visible = false
      this.minimized = false
      this.destroyed = false
      this.webContents = new EventEmitter()
      Object.assign(this.webContents, {
        mainFrame: { url: '' },
        loading: true,
        sent: [],
        isLoadingMainFrame: () => this.webContents.loading,
        getURL: () => this.webContents.mainFrame.url,
        send: (...args) => this.webContents.sent.push(args),
      })
      windows.push(this)
    }
    loadURL(url) {
      this.url = url
      this.webContents.mainFrame.url = url
      this.webContents.loading = true
      this.webContents.emit('did-start-navigation', {}, url, false, true)
    }
    finishLoad() {
      this.webContents.loading = false
      this.webContents.emit('did-finish-load')
    }
    show() { this.visible = true }
    hide() { this.visible = false }
    focus() { this.focused = true }
    restore() { this.minimized = false }
    isVisible() { return this.visible }
    isMinimized() { return this.minimized }
    isDestroyed() { return this.destroyed }
    close() {
      let prevented = false
      this.emit('close', { preventDefault: () => { prevented = true } })
      if (!prevented) {
        this.destroyed = true
        this.emit('closed')
        app.emit('window-all-closed')
      }
      return prevented
    }
  }
  class Tray extends EventEmitter {
    constructor(image) { super(); this.image = image; this.popupMenus = []; trays.push(this) }
    setToolTip(value) { this.tooltip = value }
    setContextMenu(menu) { this.menu = menu }
    setIgnoreDoubleClickEvents(value) { this.ignoreDoubleClicks = value }
    popUpContextMenu(menu) { this.popupMenus.push(menu) }
    destroy() { this.destroyed = true }
  }
  const Menu = {
    buildFromTemplate: (template) => ({ template }),
    setApplicationMenu: (menu) => { Menu.applicationMenu = menu },
  }
  const ipcMain = new EventEmitter()
  ipcMain.handle = (channel, callback) => handlers.set(channel, callback)
  const externalUrls = []
  const electron = {
    app, BrowserWindow, Menu, Tray, ipcMain,
    nativeImage: {
      createFromPath: (filename) => {
        const image = { filename, setTemplateImage(value) { this.template = value } }
        images.push(image)
        return image
      },
    },
    shell: { openExternal: (url) => externalUrls.push(url) },
    session: { defaultSession: {
      setPermissionRequestHandler: (callback) => { permissions.request = callback },
      setPermissionCheckHandler: (callback) => { permissions.check = callback },
    } },
    globalShortcut: {
      register: (accelerator, callback) => {
        shortcut.registerCalls.push(accelerator)
        if (options.shortcutThrows) throw new Error('Unavailable')
        shortcut.callback = callback
        return options.shortcutRegistered !== false
      },
      unregisterAll: () => { shortcut.unregisterCalls += 1 },
    },
  }
  vm.runInNewContext(fs.readFileSync(path.join(desktopDir, 'main.js'), 'utf8'), {
    require: (name) => {
      if (name === 'electron') return electron
      if (name === 'node:fs') return { readFileSync(filename) {
        configReads.push(filename)
        const configs = options.configs || {}
        if (Object.prototype.hasOwnProperty.call(configs, filename)) return configs[filename]
        if (filename === brandLocal) return fs.readFileSync(filename)
        throw new Error('No config')
      } }
      if (name.startsWith('.')) return require(path.resolve(desktopDir, name))
      return require(name)
    },
    process: {
      platform,
      resourcesPath: options.resourcesPath === undefined ? resourcesDir : options.resourcesPath,
      env: { LUMA_SERVER_URL: 'https://luma.example.com/app', ...options.env },
    },
    URL, __dirname: desktopDir,
  })
  await Promise.resolve()
  const ready = (value = true, event = {}) => {
    const contents = windows[windows.length - 1].webContents
    ipcMain.emit('app:commands-ready', { sender: contents, senderFrame: contents.mainFrame, ...event }, value)
  }
  const menu = () => {
    const tray = trays[0]
    if (platform === 'darwin') { tray.emit('click'); return tray.popupMenus.at(-1).template }
    return tray.menu.template
  }
  return { app, windows, trays, images, handlers, permissions, shortcut, ready, menu, ipcMain, Menu, externalUrls, startupCalls, configReads }
}

function loadPreload() {
  let desktop
  const ipcRenderer = new EventEmitter()
  ipcRenderer.sent = []
  ipcRenderer.send = (...args) => ipcRenderer.sent.push(args)
  ipcRenderer.invoke = (...args) => Promise.resolve(args)
  vm.runInNewContext(fs.readFileSync(path.join(desktopDir, 'preload.js'), 'utf8'), {
    require: () => ({ ipcRenderer, contextBridge: { exposeInMainWorld: (_name, api) => { desktop = api } } }),
    process: { platform: 'darwin' },
  })
  return { desktop, ipcRenderer }
}

test('macOS uses branded template tray, a single popup per click and a native quit menu', async () => {
  const state = await loadMain()
  const window = state.windows[0]
  assert.equal(path.basename(state.images[0].filename), 'trayTemplate.png')
  assert.equal(state.images[0].template, true)
  assert.equal(window.config.title, 'Luma')
  assert.equal(state.trays[0].tooltip, 'Luma')
  assert.equal(path.basename(window.config.icon), 'icon.png')
  assert.equal(window.config.webPreferences.contextIsolation, true)
  assert.equal(window.config.webPreferences.nodeIntegration, false)
  assert.equal(window.config.webPreferences.sandbox, true)
  assert.equal(new URL(window.url).searchParams.get('client'), '1')
  assert.equal(state.trays[0].menu, undefined)
  assert.equal(state.trays[0].ignoreDoubleClicks, true)
  const labels = state.menu().map((item) => item.label || item.type)
  assert.deepEqual(Array.from(labels), ['打开 Luma', '新建旁聊', '搜索…', '语音输入', 'separator', '开机启动', '退出'])
  assert.equal(state.trays[0].popupMenus.length, 1)
  state.trays[0].emit('right-click')
  assert.equal(state.trays[0].popupMenus.length, 2)
  const appMenu = state.Menu.applicationMenu.template[0]
  assert.equal(appMenu.label, 'Luma')
  for (const [role, label] of [['about', '关于 Luma'], ['hide', '隐藏 Luma'], ['quit', '退出 Luma']]) {
    assert.equal(appMenu.submenu.find((item) => item.role === role).label, label)
  }
  assert.deepEqual(Array.from(appMenu.submenu.filter((item) => item.role).map((item) => item.role)),
    ['about', 'services', 'hide', 'hideOthers', 'unhide', 'quit'])
})

test('Luma is named before ready on both platforms and sets only the macOS Dock icon', async () => {
  for (const platform of ['darwin', 'win32']) {
    const state = await loadMain(platform)
    assert.equal(state.app.getName(), 'Luma')
    assert.equal(state.windows[0].config.title, 'Luma')
    assert.equal(state.trays[0].tooltip, 'Luma')
    assert.equal(state.handlers.get('app:get-info')().name, 'Luma')
    assert.deepEqual(state.startupCalls, platform === 'darwin'
      ? ['setName', 'whenReady', 'dockIcon'] : ['setName', 'whenReady'])
    assert.equal(state.app.dockIcon, platform === 'darwin' ? path.join(desktopDir, 'build', 'icon.png') : undefined)
  }
})

test('server configuration prefers the environment, then packaged resources, then the local file', async () => {
  const resourceFile = path.join(resourcesDir, 'config.json')
  const localFile = path.join(desktopDir, 'config.local.json')
  const configs = {
    [resourceFile]: JSON.stringify({ serverUrl: ' https://packaged.example.com/app ' }),
    [localFile]: JSON.stringify({ serverUrl: 'https://local.example.com/app' }),
  }
  const environment = await loadMain('darwin', { configs, env: { LUMA_SERVER_URL: ' https://env.example.com/app ' } })
  assert.equal(new URL(environment.windows[0].url).origin, 'https://env.example.com')
  assert.deepEqual(environment.configReads, [brandResource, brandLocal])
  const packaged = await loadMain('darwin', { configs, env: { LUMA_SERVER_URL: undefined } })
  assert.equal(new URL(packaged.windows[0].url).origin, 'https://packaged.example.com')
  assert.deepEqual(packaged.configReads, [brandResource, brandLocal, resourceFile])
  assert.equal(packaged.permissions.check(null, 'media', 'https://packaged.example.com', { mediaType: 'audio' }), true)
  assert.equal(packaged.permissions.check(null, 'media', 'https://local.example.com', { mediaType: 'audio' }), false)
  const local = await loadMain('win32', { configs: { [localFile]: configs[localFile] }, env: { LUMA_SERVER_URL: '' } })
  assert.equal(new URL(local.windows[0].url).origin, 'https://local.example.com')
  assert.deepEqual(local.configReads, [brandResource, brandLocal, resourceFile, localFile])
})

test('missing, malformed or empty packaged config falls back without changing the local default', async () => {
  const resourceFile = path.join(resourcesDir, 'config.json')
  const localFile = path.join(desktopDir, 'config.local.json')
  for (const resource of ['{invalid JSON', 'null', '{"serverUrl":42}', '{"serverUrl":"  "}']) {
    const state = await loadMain('darwin', {
      env: { LUMA_SERVER_URL: ' ' },
      configs: { [resourceFile]: resource, [localFile]: '{"serverUrl":" https://local.example.com "}' },
    })
    assert.equal(new URL(state.windows[0].url).origin, 'https://local.example.com')
    assert.equal(new URL(state.windows[0].url).pathname, '/app')
    assert.deepEqual(state.configReads, [brandResource, brandLocal, resourceFile, localFile])
  }
  const state = await loadMain('win32', { env: { LUMA_SERVER_URL: undefined } })
  assert.equal(state.windows[0].url, 'http://localhost:8000/app?client=1')
  const noResources = await loadMain('darwin', { env: { LUMA_SERVER_URL: undefined }, resourcesPath: null })
  assert.equal(noResources.windows[0].url, 'http://localhost:8000/app?client=1')
  assert.deepEqual(noResources.configReads, [brandLocal, localFile])
})

test('Windows uses the application ICO and single tray click restores and focuses the window', async () => {
  const state = await loadMain('win32')
  const window = state.windows[0]
  assert.equal(path.basename(window.config.icon), 'icon.ico')
  assert.equal(path.basename(state.images[0].filename), 'tray.png')
  assert.equal(state.images[0].template, undefined)
  assert.ok(state.trays[0].menu)
  window.minimized = true
  state.trays[0].emit('click')
  assert.equal(window.visible, true)
  assert.equal(window.minimized, false)
  assert.equal(window.focused, true)
})

test('commands wait for both renderer subscription and completed initial loading', async () => {
  const state = await loadMain()
  const window = state.windows[0]
  const menu = state.menu()
  menu[1].click()
  menu[2].click()
  menu[3].click()
  assert.equal(window.visible, true)
  assert.equal(window.webContents.sent.length, 0)
  state.ready()
  assert.equal(window.webContents.sent.length, 0)
  window.finishLoad()
  assert.deepEqual(window.webContents.sent, [
    ['app:command', 'new-side-chat'], ['app:command', 'open-search'], ['app:command', 'start-voice'],
  ])
  state.ready()
  assert.equal(window.webContents.sent.length, 3)
})

test('loaded pages without a workspace listener do not replay commands after login', async () => {
  const state = await loadMain()
  state.windows[0].finishLoad()
  state.menu()[3].click()
  state.ready()
  assert.equal(state.windows[0].webContents.sent.length, 0)
})

test('navigation and unsubscription discard pending commands, in-page navigation preserves readiness', async () => {
  const state = await loadMain()
  const window = state.windows[0]
  state.menu()[1].click()
  window.webContents.emit('did-start-navigation', {}, window.url, false, true)
  state.ready()
  window.finishLoad()
  assert.equal(window.webContents.sent.length, 0)
  window.webContents.emit('did-start-navigation', {}, window.url, true, true)
  state.menu()[2].click()
  assert.equal(window.webContents.sent[0][1], 'open-search')
  window.webContents.loading = true
  state.menu()[3].click()
  state.ready(false)
  window.finishLoad()
  state.ready()
  assert.equal(window.webContents.sent.length, 1)
})

test('readiness IPC rejects other windows, subframes, origins and non-boolean values', async () => {
  const state = await loadMain()
  const window = state.windows[0]
  state.menu()[1].click()
  window.finishLoad()
  state.ready(true, { sender: new EventEmitter() })
  state.ready(true, { senderFrame: { url: window.url } })
  state.ready('true')
  window.webContents.mainFrame.url = 'https://untrusted.example.com/app'
  state.ready()
  assert.equal(window.webContents.sent.length, 0)
  window.webContents.mainFrame.url = window.url
  state.ready()
  assert.equal(window.webContents.sent.length, 1)
})

test('macOS and Windows close to tray; explicit quit closes and releases all shortcuts', async () => {
  for (const platform of ['darwin', 'win32']) {
    const state = await loadMain(platform)
    const window = state.windows[0]
    window.show()
    assert.equal(window.close(), true)
    assert.equal(window.visible, false)
    assert.equal(window.destroyed, false)
    state.app.emit('window-all-closed')
    assert.equal(state.app.quitCalls, undefined)
    state.menu().find((item) => item.label === '退出').click()
    assert.equal(state.app.quitCalls, 1)
    assert.equal(window.destroyed, true)
    assert.equal(state.trays[0].destroyed, true)
    assert.equal(state.shortcut.unregisterCalls, 1)
  }
})

test('the global shortcut toggles visibility and registration failures are silent', async () => {
  const state = await loadMain()
  const window = state.windows[0]
  assert.deepEqual(state.shortcut.registerCalls, ['CommandOrControl+Shift+L'])
  state.shortcut.callback()
  assert.equal(window.visible, true)
  state.shortcut.callback()
  assert.equal(window.visible, false)
  window.show()
  window.minimized = true
  state.shortcut.callback()
  assert.equal(window.minimized, false)
  assert.equal(window.visible, true)
  await loadMain('darwin', { shortcutThrows: true })
  await loadMain('win32', { shortcutRegistered: false })
})

test('the startup checkbox reads OS settings and persists both enable and disable', async () => {
  for (const platform of ['darwin', 'win32']) {
    const state = await loadMain(platform)
    assert.equal(state.menu()[5].checked, false)
    state.menu()[5].click({ checked: true })
    assert.equal(state.app.openAtLogin, true)
    assert.equal(state.menu()[5].checked, true)
    state.menu()[5].click({ checked: false })
    assert.equal(state.app.openAtLogin, false)
    assert.equal(state.menu()[5].checked, false)
  }
})

test('microphone permission remains restricted to audio and the configured server origin', async () => {
  const state = await loadMain()
  const request = (details, permission = 'media') => {
    let accepted
    state.permissions.request(null, permission, (value) => { accepted = value }, details)
    return accepted
  }
  assert.equal(request({ securityOrigin: 'https://luma.example.com', mediaTypes: ['audio'] }), true)
  assert.equal(request({ securityOrigin: 'https://other.example.com', mediaTypes: ['audio'] }), false)
  assert.equal(request({ securityOrigin: 'https://luma.example.com', mediaTypes: ['audio', 'video'] }), false)
  assert.equal(request({ securityOrigin: 'https://luma.example.com', mediaTypes: [] }), false)
  assert.equal(request({ securityOrigin: 'null', requestingUrl: 'https://luma.example.com', mediaTypes: ['audio'] }), false)
  assert.equal(state.permissions.check(null, 'media', 'https://luma.example.com', { mediaType: 'audio' }), true)
  assert.equal(state.permissions.check(null, 'media', 'https://luma.example.com', { mediaType: 'video' }), false)
  assert.equal(state.handlers.get('app:open-external')(null, 'file:///etc/passwd'), false)
  assert.equal(state.handlers.get('app:open-external')(null, 'https://luma.example.com/help'), true)
  assert.deepEqual(state.externalUrls, ['https://luma.example.com/help'])
})

test('preload only forwards the command whitelist and never exposes Electron event objects', () => {
  const { desktop, ipcRenderer } = loadPreload()
  const received = []
  const unsubscribe = desktop.onCommand((...args) => received.push(args))
  for (const command of ['new-side-chat', 'open-search', 'start-voice', 'run-shell', { command: 'start-voice' }, null]) {
    ipcRenderer.emit('app:command', { sender: 'private Electron event' }, command)
  }
  assert.deepEqual(received, [['new-side-chat'], ['open-search'], ['start-voice']])
  unsubscribe()
  unsubscribe()
  ipcRenderer.emit('app:command', {}, 'start-voice')
  assert.equal(received.length, 3)
  assert.deepEqual(ipcRenderer.sent, [['app:commands-ready', true], ['app:commands-ready', false]])
  assert.equal(ipcRenderer.listenerCount('app:command'), 0)
})

test('window title, tray tooltip and menus follow the brand file', async () => {
  const state = await loadMain('darwin', {
    configs: {
      [brandLocal]: JSON.stringify({
        productName: 'Northstar',
        appId: 'app.northstar.desktop',
        iconDir: 'custom-icons',
        trayTooltip: 'Northstar tray',
        copyright: '',
        macIdentity: null,
      }),
    },
  })
  assert.equal(state.app.getName(), 'Northstar')
  assert.equal(state.handlers.get('app:get-info')().name, 'Northstar')
  assert.equal(state.windows[0].config.title, 'Northstar')
  assert.equal(state.trays[0].tooltip, 'Northstar tray')
  assert.equal(path.dirname(state.windows[0].config.icon), path.join(desktopDir, 'custom-icons'))
  assert.equal(path.basename(state.windows[0].config.icon), 'icon.png')
  assert.equal(path.basename(state.images[0].filename), 'trayTemplate.png')
  assert.equal(state.menu()[0].label, '打开 Northstar')
  const appMenu = state.Menu.applicationMenu.template[0]
  assert.equal(appMenu.label, 'Northstar')
  assert.equal(appMenu.submenu.find((item) => item.role === 'about').label, '关于 Northstar')
  assert.equal(appMenu.submenu.find((item) => item.role === 'hide').label, '隐藏 Northstar')
  assert.equal(appMenu.submenu.find((item) => item.role === 'quit').label, '退出 Northstar')
  assert.deepEqual(state.configReads, [brandResource, brandLocal])
})

test('BRAND_FILE selects another brand file and resolves relative icons beside it', async () => {
  const brandFile = path.join(resourcesDir, 'custom-brand.json')
  const state = await loadMain('win32', {
    env: { BRAND_FILE: brandFile },
    configs: {
      [brandFile]: JSON.stringify({
        productName: 'Northstar',
        trayTooltip: 'Northstar tray',
        iconDir: 'icons',
      }),
    },
  })
  assert.equal(state.app.getName(), 'Northstar')
  assert.equal(state.windows[0].config.title, 'Northstar')
  assert.equal(state.trays[0].tooltip, 'Northstar tray')
  assert.equal(state.windows[0].config.icon, path.join(resourcesDir, 'icons', 'icon.ico'))
  assert.equal(state.images[0].filename, path.join(resourcesDir, 'icons', 'tray.png'))
  assert.deepEqual(state.configReads, [brandFile])
})

test('a packaged brand file supplies the name while icons stay in the app directory', async () => {
  const state = await loadMain('darwin', {
    configs: {
      [brandResource]: JSON.stringify({
        productName: 'Packaged',
        trayTooltip: 'Packaged tip',
        iconDir: 'build',
      }),
    },
  })
  assert.equal(state.app.getName(), 'Packaged')
  assert.equal(state.windows[0].config.title, 'Packaged')
  assert.equal(state.trays[0].tooltip, 'Packaged tip')
  assert.equal(state.app.dockIcon, path.join(desktopDir, 'build', 'icon.png'))
  assert.equal(state.windows[0].config.icon, path.join(desktopDir, 'build', 'icon.png'))
  assert.deepEqual(state.configReads, [brandResource])
})

test('multiple preload subscribers stay ready until the last idempotent unsubscribe', () => {
  const { desktop, ipcRenderer } = loadPreload()
  desktop.onCommand(null)()
  assert.equal(ipcRenderer.sent.length, 0)
  const first = desktop.onCommand(() => {})
  const second = desktop.onCommand(() => {})
  first()
  first()
  assert.deepEqual(ipcRenderer.sent, [['app:commands-ready', true]])
  second()
  assert.deepEqual(ipcRenderer.sent, [['app:commands-ready', true], ['app:commands-ready', false]])
})
