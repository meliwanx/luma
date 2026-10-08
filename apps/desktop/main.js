const { app, BrowserWindow, Menu, Tray, nativeImage, ipcMain, shell, session, globalShortcut } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const { readRuntimeBrand, resolveIconFile } = require('./scripts/apply-brand')

const brand = readRuntimeBrand({
  fs,
  env: process.env,
  dirname: __dirname,
  resourcesPath: process.resourcesPath,
})

app.setName(brand.productName)

function iconFile(name) {
  return resolveIconFile(brand, name, { dirname: __dirname, resourcesPath: process.resourcesPath })
}

const isDev = Boolean(process.env.ELECTRON_RENDERER_URL)

function configServerUrl(filename) {
  try {
    const config = JSON.parse(fs.readFileSync(filename, 'utf8'))
    return typeof config.serverUrl === 'string' ? config.serverUrl.trim() : ''
  } catch {
    return ''
  }
}

const serverUrl = (process.env.LUMA_SERVER_URL || '').trim() ||
  (process.resourcesPath && configServerUrl(path.join(process.resourcesPath, 'config.json'))) ||
  configServerUrl(path.join(__dirname, 'config.local.json')) || 'http://localhost:8000'
function httpOrigin(url) {
  try {
    const parsed = new URL(url)
    if (!['http:', 'https:'].includes(parsed.protocol) || /[,*]/.test(parsed.hostname)) return ''
    return parsed.origin
  } catch {
    return ''
  }
}

const serverOrigin = httpOrigin(serverUrl)
const rendererOrigin = isDev ? httpOrigin(process.env.ELECTRON_RENDERER_URL) : serverOrigin
const desktopCommands = new Set(['new-side-chat', 'open-search', 'start-voice'])
if (serverOrigin) {
  // Remote HTTP pages need a secure context for getUserMedia; trust only this origin.
  app.commandLine.appendSwitch('unsafely-treat-insecure-origin-as-secure', serverOrigin)
}
let mainWindow
let tray
let isQuitting = false
let commandsReady = false
let pendingCommands = []

function trayIcon() {
  // Electron also loads the adjacent @2x representation for Retina displays.
  const filename = process.platform === 'darwin' ? 'trayTemplate.png' : 'tray.png'
  const image = nativeImage.createFromPath(iconFile(filename))
  if (process.platform === 'darwin') image.setTemplateImage(true)
  return image
}

function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) createWindow()
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
}

function flushCommands() {
  if (!commandsReady || !mainWindow || mainWindow.isDestroyed()) return
  const contents = mainWindow.webContents
  if (contents.isLoadingMainFrame() || httpOrigin(contents.getURL()) !== rendererOrigin) return
  for (const command of pendingCommands.splice(0)) contents.send('app:command', command)
}

function sendCommand(command) {
  if (!desktopCommands.has(command)) return
  showWindow()
  // Wait for React's subscription during loading, but don't save actions from
  // a login page for an unrelated workspace opened later.
  if (commandsReady || mainWindow.webContents.isLoadingMainFrame()) {
    pendingCommands.push(command)
    flushCommands()
  }
}

function toggleWindow() {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() && !mainWindow.isMinimized()) mainWindow.hide()
  else showWindow()
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 900,
    minHeight: 640,
    title: brand.productName,
    icon: iconFile(process.platform === 'win32' ? 'icon.ico' : 'icon.png'),
    backgroundColor: '#0f1110',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  commandsReady = false
  const window = mainWindow
  window.webContents.on('did-start-navigation', (_event, _url, isInPlace, isMainFrame) => {
    if (isMainFrame && !isInPlace) {
      commandsReady = false
      pendingCommands = []
    }
  })
  window.webContents.on('did-finish-load', flushCommands)
  window.webContents.on('render-process-gone', () => { commandsReady = false; pendingCommands = [] })
  window.once('ready-to-show', () => { if (!isQuitting) window.show() })
  window.on('close', (event) => {
    if (isQuitting) return
    event.preventDefault()
    window.hide()
  })
  window.on('closed', () => { mainWindow = null; commandsReady = false; pendingCommands = [] })

  if (isDev) {
    // Desktop clients always open the workspace. The marketing site remains
    // a browser-only entry point, even when Electron uses the Vite renderer.
    const rendererUrl = new URL(process.env.ELECTRON_RENDERER_URL)
    if (!rendererUrl.pathname || rendererUrl.pathname === '/') rendererUrl.pathname = '/app'
    rendererUrl.searchParams.set('client', '1')
    mainWindow.loadURL(rendererUrl.toString())
  } else {
    // Load the deployed workspace and its account API from the same origin.
    const workspaceUrl = new URL(serverUrl)
    if (!workspaceUrl.pathname || workspaceUrl.pathname === '/') workspaceUrl.pathname = '/app'
    workspaceUrl.searchParams.set('client', '1')
    mainWindow.loadURL(workspaceUrl.toString())
  }
}

function trayMenu() {
  const supportsLoginItems = ['darwin', 'win32'].includes(process.platform)
  return Menu.buildFromTemplate([
    { label: `打开 ${brand.productName}`, click: showWindow },
    { label: '新建旁聊', click: () => sendCommand('new-side-chat') },
    { label: '搜索…', click: () => sendCommand('open-search') },
    { label: '语音输入', click: () => sendCommand('start-voice') },
    { type: 'separator' },
    {
      label: '开机启动',
      type: 'checkbox',
      enabled: supportsLoginItems,
      checked: supportsLoginItems && app.getLoginItemSettings().openAtLogin,
      click: (item) => {
        app.setLoginItemSettings({ openAtLogin: item.checked })
        if (process.platform !== 'darwin') tray.setContextMenu(trayMenu())
      },
    },
    { label: '退出', click: () => app.quit() },
  ])
}

function createTray() {
  tray = new Tray(trayIcon())
  tray.setToolTip(brand.trayTooltip)
  if (process.platform === 'darwin') {
    // Supplying a context menu would let macOS open it automatically as well.
    tray.setIgnoreDoubleClickEvents(true)
    tray.on('click', () => tray.popUpContextMenu(trayMenu()))
    tray.on('right-click', () => tray.popUpContextMenu(trayMenu()))
  } else {
    tray.setContextMenu(trayMenu())
    tray.on('click', showWindow)
  }
}

app.whenReady().then(() => {
  // Use the requesting frame's security origin, including opaque sandboxed frames.
  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback, details = {}) => {
    const origin = details.securityOrigin === undefined ? details.requestingUrl : details.securityOrigin
    callback(Boolean(serverOrigin) && permission === 'media' &&
      httpOrigin(origin) === serverOrigin &&
      Array.isArray(details.mediaTypes) && details.mediaTypes.length > 0 &&
      details.mediaTypes.every(type => type === 'audio'))
  })
  session.defaultSession.setPermissionCheckHandler((_webContents, permission, requestingOrigin, details = {}) => {
    const origin = details.securityOrigin === undefined ? requestingOrigin : details.securityOrigin
    return Boolean(serverOrigin) && permission === 'media' && details.mediaType === 'audio' &&
      httpOrigin(origin) === serverOrigin
  })
  ipcMain.handle('app:get-info', () => ({ name: app.getName(), version: app.getVersion(), platform: process.platform }))
  ipcMain.handle('app:open-external', (_event, url) => {
    if (typeof url !== 'string' || !/^https?:\/\//.test(url)) return false
    shell.openExternal(url)
    return true
  })
  ipcMain.on('app:commands-ready', (event, ready) => {
    if (!mainWindow || event.sender !== mainWindow.webContents ||
      event.senderFrame !== mainWindow.webContents.mainFrame ||
      httpOrigin(event.senderFrame.url) !== rendererOrigin || typeof ready !== 'boolean') return
    commandsReady = ready
    if (ready) flushCommands()
    else pendingCommands = []
  })
  if (process.platform === 'darwin') {
    app.dock.setIcon(iconFile('icon.png'))
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      {
        label: brand.productName,
        submenu: [
          { role: 'about', label: `关于 ${brand.productName}` },
          { type: 'separator' },
          { role: 'services' },
          { type: 'separator' },
          { role: 'hide', label: `隐藏 ${brand.productName}` },
          { role: 'hideOthers' },
          { role: 'unhide' },
          { type: 'separator' },
          { role: 'quit', label: `退出 ${brand.productName}` },
        ],
      },
      { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
    ]))
  }
  createWindow()
  createTray()
  try { globalShortcut.register('CommandOrControl+Shift+L', toggleWindow) } catch {}
  app.on('activate', showWindow)
})

app.on('before-quit', () => {
  isQuitting = true
  globalShortcut.unregisterAll()
  if (tray) tray.destroy()
})
