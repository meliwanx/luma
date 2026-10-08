import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { isLumaWordmark } from './brand.js'

const source = await readFile(new URL('./logo.jsx', import.meta.url), 'utf8')
const { code } = await transformWithEsbuild(source.replace(/^import .*\n/gm, '').replace('export default ', ''), 'logo.jsx', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement',
})

function renderLogo(brand, props = {}, { failImage = false } = {}) {
  const states = []
  let cursor = 0
  const context = {
    React,
    isLumaWordmark,
    useBrand: () => brand,
    useState: (initial) => {
      const index = cursor++
      if (!(index in states)) states[index] = initial
      return [states[index], (value) => { states[index] = typeof value === 'function' ? value(states[index]) : value }]
    },
  }
  vm.createContext(context)
  vm.runInContext(code, context)
  const draw = () => { cursor = 0; return context.BrandLogo(props) }
  let element = draw()
  if (failImage) {
    const image = findImage(element)
    assert.ok(image, 'configured logo renders an image before it fails')
    image.props.onError()
    element = draw()
  }
  return renderToStaticMarkup(element)
}

function findImage(element) {
  if (!React.isValidElement(element)) return null
  if (element.type === 'img') return element
  for (const child of React.Children.toArray(element.props.children)) {
    const found = findImage(child)
    if (found) return found
  }
  return null
}

const luma = { product_name: 'Luma', name: 'Luma', tagline: '个人助理', logo_url: '', primary_color: '#2563EB' }

test('wordmark uses the supplied vector path with theme ink and a tight viewBox', () => {
  const html = renderLogo(luma, { label: 'Luma' })
  assert.match(html, /viewBox="60 290 1020 480"/)
  assert.match(html, /stroke="currentColor"/)
  assert.match(html, /stroke-width="51"/)
  assert.match(html, /role="img" aria-label="Luma"/)
  assert.equal((html.match(/<path /g) || []).length, 1)
  assert.doesNotMatch(html, /<image|href=/)
})

test('decorative wordmarks do not repeat nearby accessible labels', () => {
  const html = renderLogo(luma, { className: 'empty-state-logo' })
  assert.match(html, /class="luma-logo brand-logo empty-state-logo"/)
  assert.match(html, /aria-hidden="true"/)
  assert.doesNotMatch(html, /role="img"|aria-label=/)
})

test('a non-Luma name falls back to a bold text mark in the brand color', () => {
  const html = renderLogo({ ...luma, product_name: '测试品牌', name: '测试品牌' }, { labelled: true })
  assert.match(html, /class="brand-logo brand-logo-text"/)
  assert.match(html, /font-weight:700/)
  assert.match(html, /color:var\(--brand-primary\)/)
  assert.match(html, />测试品牌</)
  assert.doesNotMatch(html, /<path |Luma/)
})

test('logo_url renders an image and a load error falls back without the Luma wordmark', () => {
  const brand = { ...luma, product_name: '测试品牌', name: '测试品牌', logo_url: 'https://example.com/logo.png' }
  const image = renderLogo(brand, { labelled: true })
  assert.match(image, /<img [^>]*src="https:\/\/example.com\/logo.png"/)
  assert.match(image, /alt="测试品牌"/)
  assert.doesNotMatch(image, /Luma|<path /)
  const fallback = renderLogo(brand, { labelled: true }, { failImage: true })
  assert.match(fallback, />测试品牌</)
  assert.doesNotMatch(fallback, /<img|Luma|<path /)
})

test('a Luma wordmark returns when its configured image fails', () => {
  const html = renderLogo({ ...luma, logo_url: 'https://example.com/logo.png' }, { label: 'Luma' }, { failImage: true })
  assert.match(html, /viewBox="60 290 1020 480"/)
  assert.match(html, /aria-label="Luma"/)
  assert.doesNotMatch(html, /<img/)
})
