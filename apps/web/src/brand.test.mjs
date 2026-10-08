import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import {
  applyBrandToDocument,
  brandFromEnv,
  brandTitle,
  buildBrand,
  mergeBrand,
  resolveBrand,
} from './brand.js'

const base = brandFromEnv({})

test('build defaults come from empty env and explicit Vite values override them', () => {
  assert.equal(base.product_name, 'Luma')
  assert.equal(base.name, 'Luma')
  assert.equal(base.tagline, '你的个人 AI 助理')
  assert.equal(base.primary_color, '#2563EB')
  assert.equal(base.logo_url, '')
  assert.equal(buildBrand().product_name, 'Luma')
  const custom = brandFromEnv({
    VITE_BRAND_PRODUCT_NAME: '  Northstar  ',
    VITE_BRAND_TAGLINE: 'stay with it',
    VITE_BRAND_PRIMARY_COLOR: '#abc',
    VITE_BRAND_LOGO_URL: ' https://cdn.example/logo.png ',
  })
  assert.equal(custom.product_name, 'Northstar')
  assert.equal(custom.tagline, 'stay with it')
  assert.equal(custom.primary_color, '#abc')
  assert.equal(custom.logo_url, 'https://cdn.example/logo.png')
  assert.equal(brandFromEnv({ VITE_BRAND_PRIMARY_COLOR: 'red' }).primary_color, '#2563EB')
})

test('runtime brand fields override build defaults and a failed request keeps them', async () => {
  const remote = mergeBrand(base, {
    name: '测试品牌',
    tagline: '一句介绍',
    logo_url: 'https://cdn.example/logo.png',
    primary_color: '#112233',
  })
  assert.equal(remote.product_name, '测试品牌')
  assert.equal(remote.name, '测试品牌')
  assert.equal(remote.tagline, '一句介绍')
  assert.equal(remote.logo_url, 'https://cdn.example/logo.png')
  assert.equal(remote.primary_color, '#112233')
  assert.equal(mergeBrand(base, { primaryColor: '#445566' }).primary_color, '#445566')
  assert.equal(mergeBrand(base, { primary_color: 'javascript:alert(1)' }).primary_color, '#2563EB')
  assert.equal(mergeBrand(base, { tagline: '' }).tagline, '')
  assert.equal(mergeBrand(base, { logo_url: null }).logo_url, '')
  assert.deepEqual(mergeBrand(base, null), { ...base })

  let requested = ''
  const failed = await resolveBrand(base, async (url) => { requested = url; throw new Error('offline') }, '/api/v1/brand')
  assert.equal(requested, '/api/v1/brand')
  assert.equal(failed.product_name, 'Luma')
  const httpError = await resolveBrand(base, async () => ({ ok: false, status: 404, json: async () => ({ name: '测试品牌' }) }), '/api/v1/brand')
  assert.equal(httpError.product_name, 'Luma')
  const badJson = await resolveBrand(base, async () => ({ ok: true, json: async () => { throw new Error('not json') } }), '/api/v1/brand')
  assert.equal(badJson.product_name, 'Luma')
  const applied = await resolveBrand(base, async () => ({ ok: true, json: async () => ({ name: '测试品牌', primary_color: '#abcdef' }) }), '/api/v1/brand')
  assert.equal(applied.product_name, '测试品牌')
  assert.equal(applied.tagline, '你的个人 AI 助理')
  assert.equal(applied.primary_color, '#abcdef')
})

test('document title follows the brand and omits Luma for another product', () => {
  const brand = mergeBrand(base, { name: '测试品牌' })
  assert.equal(brandTitle(brand), '测试品牌 · 你的个人 AI 助理')
  assert.equal(brandTitle(brand).includes('Luma'), false)
  const target = { title: '', documentElement: { style: { props: {}, setProperty(name, value) { this.props[name] = value } } } }
  applyBrandToDocument(brand, target)
  assert.equal(target.title, '测试品牌 · 你的个人 AI 助理')
  assert.equal(target.documentElement.style.props['--brand-primary'], '#2563EB')
})

const source = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
const loginFragment = source.slice(source.indexOf('function LoginPage('), source.indexOf('function RouteLoading('))
const landingFragment = `function LandingTitle(){ const productName = useBrand().product_name; const brandMark = productName.toUpperCase(); return React.createElement('h1', null, productName, brandMark) }\n${source.slice(source.indexOf('function LandingPage('), source.indexOf('function FeatureCard('))}`
const { code: loginCode } = await transformWithEsbuild(loginFragment, 'login-page.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })
const { code: landingCode } = await transformWithEsbuild(landingFragment, 'landing-page.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })

function renderPage(code, brand) {
  const states = []
  let stateIndex = 0
  const refs = []
  let refIndex = 0
  const context = {
    React,
    API_URL: '/api/v1',
    ACCESS_TOKEN_STORAGE_KEY: 'luma_access_token',
    useBrand: () => brand,
    BrandLogo: () => React.createElement('svg', { 'data-logo': 'brand' }),
    useState: (initial) => {
      const index = stateIndex++
      if (!(index in states)) states[index] = initial
      return [states[index], (value) => { states[index] = typeof value === 'function' ? value(states[index]) : value }]
    },
    useRef: (initial) => { const index = refIndex++; return refs[index] ||= { current: initial } },
    useEffect: () => {},
    loadAuthConfig: async () => ({ registration_open: true, requires_invite: false }),
    loginWithPassword: async () => ({}),
    registerWithPassword: async () => ({}),
    navigate: () => {},
  }
  vm.createContext(context)
  vm.runInContext(code, context)
  stateIndex = 0
  refIndex = 0
  const page = context.LoginPage ? context.LoginPage({ onAuthenticated: () => {} }) : context.LandingPage()
  return renderToStaticMarkup(page)
}

test('a renamed brand does not render Luma in the title or the login page', () => {
  const brand = { product_name: '测试品牌', name: '测试品牌', tagline: '一句介绍', logo_url: '', primary_color: '#336699' }
  assert.equal(brandTitle(brand).includes('Luma'), false)
  const login = renderPage(loginCode, brand)
  const landing = renderPage(landingCode, brand)
  assert.match(login, /登录 测试品牌/)
  assert.match(login, /了解 测试品牌/)
  assert.doesNotMatch(login, /Luma/)
  assert.match(landing, /测试品牌/)
  assert.doesNotMatch(landing, /Luma/)
  assert.match(login, /name="login"/)
  assert.match(login, /name="password"/)
})
