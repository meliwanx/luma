export function nearBottom(element, threshold = 120) {
  return !element || element.scrollHeight - element.scrollTop - element.clientHeight < threshold
}

// Save the reader's position before content or viewport sizes change. Checking
// the distance only after a large delta/keyboard resize loses that intention.
export function createBottomFollower(getElement, { requestFrame, cancelFrame, setDelay, clearDelay }) {
  let pinned = true
  let frame = null
  let settleTimer = null
  let disposed = false

  function align() {
    const element = getElement()
    if (!disposed && pinned && element) element.scrollTop = element.scrollHeight
  }

  function schedule(force = false) {
    if (disposed) return
    if (force) pinned = true
    if (!pinned) return
    if (frame === null) frame = requestFrame(() => { frame = null; align() })
    if (settleTimer !== null) clearDelay(settleTimer)
    // Keyboard and panel animations can resize the list after the first frame.
    settleTimer = setDelay(() => {
      settleTimer = null
      if (!disposed && frame === null) frame = requestFrame(() => { frame = null; align() })
    }, 300)
  }

  return {
    schedule,
    updatePosition: () => { pinned = nearBottom(getElement()) },
    reset: () => { pinned = true },
    dispose: () => {
      disposed = true
      if (frame !== null) cancelFrame(frame)
      if (settleTimer !== null) clearDelay(settleTimer)
    },
  }
}
