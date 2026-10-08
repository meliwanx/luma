import React, { createContext, useContext, useEffect, useState } from 'react'
import { loadBrowserLiveHostSuffixes } from './browser-tools.js'

export const DEFAULT_PRODUCT_NAME = 'Luma'
export const DEFAULT_TAGLINE = '个人助理'
export const DEFAULT_PRIMARY_COLOR = '#2563EB'

const COLOR_PATTERN = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/
const BrandContext = createContext(null)

function viteEnv() {
  try {
    const env = import.meta.env
    return env && typeof env === 'object' ? env : {}
  } catch {
    return {}
  }
}

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : ''
}

function firstText(...values) {
  for (const value of values) {
    const text = cleanText(value)
    if (text) return text
  }
  return ''
}

export function normalizeColor(value, fallback = DEFAULT_PRIMARY_COLOR) {
  const text = cleanText(value)
  return COLOR_PATTERN.test(text) ? text : fallback
}

export function isLumaWordmark(name) {
  return cleanText(name).toLowerCase() === 'luma'
}

export function brandFromEnv(env = {}) {
  const productName = firstText(env.VITE_BRAND_PRODUCT_NAME) || DEFAULT_PRODUCT_NAME
  const tagline = firstText(env.VITE_BRAND_TAGLINE) || DEFAULT_TAGLINE
  return {
    product_name: productName,
    name: productName,
    tagline,
    logo_url: '',
    primary_color: normalizeColor(env.VITE_BRAND_PRIMARY_COLOR),
  }
}

export function buildBrand() {
  return brandFromEnv(viteEnv())
}

export function mergeBrand(base, remote) {
  const next = { ...base, name: base.product_name, product_name: base.product_name }
  if (!remote || typeof remote !== 'object' || Array.isArray(remote)) return next
  const productName = firstText(remote.product_name, remote.name, remote.productName)
  if (productName) {
    next.product_name = productName
    next.name = productName
  }
  if (typeof remote.tagline === 'string') next.tagline = remote.tagline.trim()
  const color = firstText(remote.primary_color, remote.primaryColor)
  if (color && COLOR_PATTERN.test(color)) next.primary_color = color
  if (Object.prototype.hasOwnProperty.call(remote, 'logo_url') || Object.prototype.hasOwnProperty.call(remote, 'logoUrl')) {
    const logo = remote.logo_url ?? remote.logoUrl
    next.logo_url = typeof logo === 'string' ? logo.trim() : ''
  }
  return next
}

export function brandTitle(brand) {
  const name = cleanText(brand?.product_name) || DEFAULT_PRODUCT_NAME
  const tagline = cleanText(brand?.tagline)
  return tagline ? `${name} · ${tagline}` : name
}

export function applyBrandToDocument(brand, target = globalThis.document) {
  if (!target) return
  target.title = brandTitle(brand)
  const color = normalizeColor(brand?.primary_color)
  target.documentElement?.style?.setProperty('--brand-primary', color)
}

export async function resolveBrand(base, fetcher, url) {
  try {
    const response = await fetcher(url)
    if (!response?.ok) return base
    return mergeBrand(base, await response.json())
  } catch {
    return base
  }
}

async function defaultFetcher(url) {
  return fetch(url, { headers: { Accept: 'application/json' }, credentials: 'include' })
}

export function useBrand() {
  return useContext(BrandContext) || buildBrand()
}

export function BrandProvider({ children, apiBase = '', fetcher }) {
  const [brand, setBrand] = useState(() => buildBrand())
  useEffect(() => {
    let active = true
    const base = buildBrand()
    applyBrandToDocument(base)
    const root = String(apiBase || '').replace(/\/$/, '')
    if (!root) return undefined
    const load = fetcher || defaultFetcher
    loadBrowserLiveHostSuffixes(root, load)
    resolveBrand(base, load, `${root}/brand`).then((next) => {
      if (!active) return
      setBrand(next)
      applyBrandToDocument(next)
    })
    return () => { active = false }
  }, [apiBase, fetcher])
  return React.createElement(BrandContext.Provider, { value: brand }, children)
}
