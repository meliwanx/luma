import { readFileSync } from 'node:fs'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

const { version } = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))

function readBrand(mode) {
  const fileEnv = loadEnv(mode, process.cwd(), 'VITE_BRAND_')
  const read = (key, fallback) => {
    const value = String(process.env[key] || fileEnv[key] || '').trim()
    return value || fallback
  }
  const name = read('VITE_BRAND_PRODUCT_NAME', 'Luma')
  const tagline = read('VITE_BRAND_TAGLINE', '个人助理')
  const color = read('VITE_BRAND_PRIMARY_COLOR', '#2563EB')
  return { name, tagline, color, title: tagline ? `${name} · ${tagline}` : name }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]))
}

function faviconHref(name, color) {
  const initial = Array.from(name)[0] || 'A'
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><title>${escapeHtml(name)}</title><rect width="64" height="64" rx="14" fill="${escapeHtml(color)}"/><text x="32" y="42" text-anchor="middle" font-family="sans-serif" font-size="28" font-weight="700" fill="#fff">${escapeHtml(initial)}</text></svg>`
  return `data:image/svg+xml,${encodeURIComponent(svg)}`
}

export default defineConfig(({ command, mode }) => {
  const brand = readBrand(mode)
  return {
    plugins: [react(), {
      name: 'brand-html',
      transformIndexHtml: {
        order: 'post',
        handler: (html) => {
          let next = html.replace(/<title>[\s\S]*?<\/title>/, `<title>${escapeHtml(brand.title)}</title>`)
          if (brand.name.toLowerCase() === 'luma') {
            return command === 'build'
              ? next.replace(/href="\.\/(favicon\.svg|favicon-32\.png|apple-touch-icon\.png)"/g, 'href="/app/$1"')
              : next
          }
          const href = faviconHref(brand.name, brand.color)
          return next.replace(/href="(?:\.\/|\/app\/)?(favicon\.svg|favicon-32\.png|apple-touch-icon\.png)"/g, `href="${href}"`)
        },
      },
    }],
    define: {
      __APP_VERSION__: JSON.stringify(version),
    },
    // Keep the bundle loadable from Electron's file:// URL as well as from the
    // FastAPI static mount. Absolute /assets URLs resolve to the host filesystem
    // under loadFile and leave the desktop window blank.
    base: './',
    server: {
      port: 5173,
      strictPort: true,
    },
    preview: {
      port: 4173,
      strictPort: true,
    },
  }
})
