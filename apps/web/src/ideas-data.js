export const IDEAS_POLL_DELAY_MS = 5000
export const IDEAS_POLL_MAX_RETRIES = 6

export function normalizeIdeas(data) {
  const active = (items) => Array.isArray(items) ? items.filter((idea) => idea && idea.status !== 'dismissed') : []
  return {
    featured: active(data?.featured),
    groups: (Array.isArray(data?.groups) ? data.groups : []).filter(Boolean).map((group) => ({ ...group, items: active(group.items) })),
    generated_at: data?.generated_at || null,
    generating: data?.generating === true,
  }
}

export function updateIdeaStatus(data, id, status) {
  const update = (items) => items.map((idea) => idea.id === id ? { ...idea, status } : idea)
  return normalizeIdeas({ ...data, featured: update(data.featured), groups: data.groups.map((group) => ({ ...group, items: update(group.items) })) })
}

// One initial fetch followed by at most six delayed fetches. A new refresh
// invalidates previous requests; stopping also discards any in-flight response.
export function createIdeasPoller({ load, onData, onError, schedule = setTimeout, cancel = clearTimeout }) {
  let stopped = false
  let timer = null
  let retries = 0
  let revision = 0

  function clearTimer() {
    if (timer !== null) cancel(timer)
    timer = null
  }

  async function fetchIdeas(token) {
    if (stopped || token !== revision) return
    try {
      const data = await load()
      if (stopped || token !== revision) return
      onData(normalizeIdeas(data))
      if (data?.generating === true && retries < IDEAS_POLL_MAX_RETRIES) {
        timer = schedule(() => {
          timer = null
          retries += 1
          return fetchIdeas(token)
        }, IDEAS_POLL_DELAY_MS)
      }
    } catch (error) {
      if (!stopped && token === revision) onError(error)
    }
  }

  return {
    refresh() {
      if (stopped) return Promise.resolve()
      clearTimer()
      retries = 0
      revision += 1
      return fetchIdeas(revision)
    },
    stop() {
      stopped = true
      revision += 1
      clearTimer()
    },
  }
}
