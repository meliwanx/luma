import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

function run(command, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: webRoot, env, stdio: ['ignore', 'pipe', 'pipe'] })
    let stderr = ''
    child.stderr.on('data', (chunk) => { stderr += chunk })
    child.on('error', reject)
    child.on('close', (code) => {
      if (code === 0) resolve()
      else reject(new Error(stderr || `${command} exited ${code}`))
    })
  })
}

test('聚光测试 build writes the brand color and no Luma into index.html', async () => {
  const out = await mkdtemp(path.join(tmpdir(), 'oc-brand-web-'))
  try {
    await run('npx', ['vite', 'build', '--outDir', out, '--emptyOutDir'], {
      ...process.env,
      VITE_BRAND_PRODUCT_NAME: '聚光测试',
      VITE_BRAND_TAGLINE: '你的个人 AI 助理',
      VITE_BRAND_PRIMARY_COLOR: '#1E66F5',
      VITE_BRAND_LOGO_URL: 'https://example.com/juguang-logo.png',
    })
    const html = await readFile(path.join(out, 'index.html'), 'utf8')
    assert.equal(html.includes('Luma'), false)
    assert.match(html, /--brand-primary:#1E66F5/)
    assert.match(html, /--mc-user:var\(--brand-primary\)/)
    assert.match(html, /name="brand-logo-url" content="https:\/\/example.com\/juguang-logo.png"/)
    assert.match(html, /聚光测试/)
  } finally {
    await rm(out, { recursive: true, force: true })
  }
})
