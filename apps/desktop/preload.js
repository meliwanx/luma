const { contextBridge, ipcRenderer } = require('electron')
const commands = new Set(['new-side-chat', 'open-search', 'start-voice'])
let commandListeners = 0

contextBridge.exposeInMainWorld('desktop', {
  platform: process.platform,
  isDesktop: true,
  getAppInfo: () => ipcRenderer.invoke('app:get-info'),
  openExternal: (url) => ipcRenderer.invoke('app:open-external', url),
  onCommand: (callback) => {
    if (typeof callback !== 'function') return () => {}
    const listener = (_event, command) => {
      if (typeof command === 'string' && commands.has(command)) callback(command)
    }
    ipcRenderer.on('app:command', listener)
    commandListeners += 1
    if (commandListeners === 1) ipcRenderer.send('app:commands-ready', true)
    let subscribed = true
    return () => {
      if (!subscribed) return
      subscribed = false
      ipcRenderer.removeListener('app:command', listener)
      commandListeners -= 1
      if (commandListeners === 0) ipcRenderer.send('app:commands-ready', false)
    }
  },
})
