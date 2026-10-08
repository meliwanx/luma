import React, { useState } from 'react'
import { isLumaWordmark, useBrand } from './brand.js'

const WORDMARK_PATH = 'M1049.5 648C1030.5 676 987.5 731 955 724C897.909 711.704 980.5 600.5 896 579C860.193 569.89 831.748 590.946 807.811 619C775.262 657.15 751.05 708.238 728 714C688 724 705 581.5 669 579C633 576.5 595.5 704 585.5 704C575.5 704 597 593 551 593C505 593 489.5 719.5 455 724C427.4 727.6 423 655 430.5 600.5C430.5 587 377.5 745 343 740C308.5 735 343 593 324 593C312.5 593 262.5 760.75 205 740C90 698.5 273.5 233 360 342C405.5 411.5 186.333 599.667 93 648M896 579C977 609 891.5 740 827 740C784.079 740 751.261 700.6 807.811 619'

function classes(...parts) {
  return parts.filter(Boolean).join(' ')
}

export default function BrandLogo({ className = '', label = '', labelled = false }) {
  const brand = useBrand()
  const name = brand.product_name || 'Luma'
  const accessible = label || (labelled ? name : '')
  const [imageFailed, setImageFailed] = useState(false)
  const logoUrl = !imageFailed && brand.logo_url ? brand.logo_url : ''
  if (logoUrl) {
    return <img className={classes('brand-logo', 'brand-logo-image', className)} src={logoUrl} alt={accessible} onError={() => setImageFailed(true)} />
  }
  if (!isLumaWordmark(name)) {
    return <span className={classes('brand-logo', 'brand-logo-text', className)} role={accessible ? 'img' : undefined} aria-label={accessible || undefined} aria-hidden={accessible ? undefined : true} style={{ color: 'var(--brand-primary)', fontWeight: 700 }}>{name}</span>
  }
  return <svg className={classes('luma-logo', 'brand-logo', className)} viewBox="60 290 1020 480" fill="none" xmlns="http://www.w3.org/2000/svg" role={accessible ? 'img' : undefined} aria-label={accessible || undefined} aria-hidden={accessible ? undefined : true}>
    <path d={WORDMARK_PATH} stroke="currentColor" strokeWidth="51" strokeLinecap="round" />
  </svg>
}
