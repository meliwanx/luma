import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const { version } = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))

export default defineConfig(({ command }) => ({
  plugins: [react(), {
    name: 'luma-brand-icons',
    transformIndexHtml: {
      order: 'post',
      handler: (html) => command === 'build'
        // FastAPI serves public assets under /app even for the public SPA routes.
        ? html.replace(/href="\.\/(favicon\.svg|favicon-32\.png|apple-touch-icon\.png)"/g, 'href="/app/$1"')
        : html,
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
}))
