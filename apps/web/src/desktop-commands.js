const COMMANDS = new Set(['new-side-chat', 'open-search', 'start-voice'])

export function subscribeDesktopCommands(desktop, getHandlers) {
  if (typeof desktop?.onCommand !== 'function') return undefined
  let active = true
  const unsubscribe = desktop.onCommand((command) => {
    if (!active || !COMMANDS.has(command)) return
    const handler = getHandlers()?.[command]
    if (typeof handler === 'function') handler()
  })
  return () => {
    if (!active) return
    active = false
    if (typeof unsubscribe === 'function') unsubscribe()
  }
}
