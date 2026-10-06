import React from 'react';

// A 24 px grid, 1.65 px rounded strokes and optically balanced 4–20 px drawings.
// Filled details are reserved for dots and the four-point Luma mark.
const drawings = {
  chart: <><path d="M5 4v16h15M9 16v-5M13 16V7M17 16V4" /></>,
  doc: <><path d="M14 3.5H6.5v17h11V7Zm0 0V7h3.5M9 11h6M9 15h6" /></>,
  calendar: <><rect x="4" y="5.5" width="16" height="15" rx="2" /><path d="M8 3.5v4M16 3.5v4M4 10h16M8 14h2M14 14h2M8 17h2" /></>,
  code: <><path d="m8 7-5 5 5 5m8-10 5 5-5 5M14 4l-4 16" /></>,
  mail: <><rect x="3.5" y="5.5" width="17" height="13" rx="2" /><path d="m4.5 7 7.5 6 7.5-6" /></>,
  book: <><path d="M12 6c-2.5-2-5.5-2-9-1v14c3.5-1 6.5-1 9 1 2.5-2 5.5-2 9-1V5c-3.5-1-6.5-1-9 1Zm0 0v14" /></>,
  money: <><circle cx="12" cy="12" r="8.5" /><path d="m8.5 7 3.5 4 3.5-4M8.5 12h7M8.5 15h7M12 11v7" /></>,
  heart: <path d="M12 20 4.5 12.5a4.7 4.7 0 0 1 6.6-6.7l.9.9.9-.9a4.7 4.7 0 0 1 6.6 6.7Z" />,
  globe: <><circle cx="12" cy="12" r="8.5" /><ellipse cx="12" cy="12" rx="3.5" ry="8.5" /><path d="M4 9h16M4 15h16" /></>,
  list: <><path d="M9 6h11M9 12h11M9 18h11M4 6h.1M4 12h.1M4 18h.1" /></>,
  image: <><rect x="3.5" y="4.5" width="17" height="15" rx="2" /><circle cx="9" cy="9" r="1.5" /><path d="m4 17 5-5 4 4 3-3 4 4" /></>,
  archive: <><rect x="5" y="3.5" width="14" height="17" rx="2" /><path d="M11 4v2h2v2h-2v2h2v2M11 15h2v3h-2Z" /></>,
  pin: <><path d="m9 3 6 2-1 5 3 4-5-1-4 3-1-5Zm3 10-5 8" /></>,
  refresh: <><path d="M19.5 9a8 8 0 0 0-13-3L3.5 9M3.5 4.5V9H8M4.5 15a8 8 0 0 0 13 3l3-3M16 15h4.5v4.5" /></>,
  sliders: <><path d="M4 7h7M15 7h5M4 17h3M11 17h9" /><circle cx="13" cy="7" r="2" /><circle cx="9" cy="17" r="2" /></>,
  chat: <>
    <path d="M8 19.2 4.6 20l.8-3.4A6.8 6.8 0 0 1 4 12.4V10a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2.4a6 6 0 0 1-6 6h-4a6.6 6.6 0 0 1-2-.3Z" />
    <g fill="currentColor" stroke="none"><circle cx="8" cy="11.3" r=".8" /><circle cx="12" cy="11.3" r=".8" /><circle cx="16" cy="11.3" r=".8" /></g>
  </>,
  search: <><circle cx="10.5" cy="10.5" r="6" /><path d="m15 15 4.7 4.7" /></>,
  activity: <>
    <rect x="5" y="3.7" width="14" height="16.6" rx="3" />
    <path d="M9 8h6M9 12h6M9 16h4" />
  </>,
  idea: <>
    <path d="M9.1 16.5v-.8c0-1.1-.8-2-1.6-2.8a6 6 0 1 1 9 0c-.8.8-1.6 1.7-1.6 2.8v.8Z" />
    <path d="M9.5 19.4h5M11 21h2" />
  </>,
  goal: <>
    <rect x="4" y="4" width="16" height="16" rx="4" />
    <path d="m7.7 9 1.3 1.3L11.4 8M14.3 9.3h2.1m-8.7 5.8 1.3 1.3 2.4-2.3M14.3 15.4h2.1" />
  </>,
  library: <>
    <path d="m12 3.8 3.1 3.1-3.1 3.1-3.1-3.1Z" />
    <path d="m7.1 10.7 4.9 4.9 4.9-4.9" />
    <circle cx="12" cy="17.6" r="2.2" />
    <rect x="4" y="6.2" width="4.1" height="4.1" rx=".7" />
  </>,
  plus: <path d="M12 5v14M5 12h14" />,
  menu: <path d="M5 7h14M5 12h14M5 17h14" />,
  settings: <>
    <path d="m10 4-.5 2a6.5 6.5 0 0 0-1.6.9L6 6.3 4 9.7l1.5 1.5a6.8 6.8 0 0 0 0 1.6L4 14.3l2 3.4 1.9-.6a6.5 6.5 0 0 0 1.6.9l.5 2h4l.5-2a6.5 6.5 0 0 0 1.6-.9l1.9.6 2-3.4-1.5-1.5a6.8 6.8 0 0 0 0-1.6L20 9.7l-2-3.4-1.9.6a6.5 6.5 0 0 0-1.6-.9L14 4Z" />
    <circle cx="12" cy="12" r="2.7" />
  </>,
  invite: <>
    <circle cx="9" cy="8" r="3.3" />
    <path d="M3.8 19v-1.4a5.2 5.2 0 0 1 5.2-5.2 5.2 5.2 0 0 1 4.2 2.1M17.5 10.5v7M14 14h7" />
  </>,
  gift: <>
    <path d="M4.5 10.5h15v9.2a1.8 1.8 0 0 1-1.8 1.8H6.3a1.8 1.8 0 0 1-1.8-1.8Z" />
    <path d="M3.5 7.2h17v3.3h-17ZM12 7.2v14.3" />
    <path d="M12 7.2H8.9a2.3 2.3 0 1 1 2.3-2.3c0 1.3.8 2.3.8 2.3Zm0 0h3.1a2.3 2.3 0 1 0-2.3-2.3C12.8 6.2 12 7.2 12 7.2Z" />
  </>,
  'screen-user': <>
    <rect x="3.5" y="4.2" width="17" height="13" rx="2.2" />
    <path d="M8 20h8M12 17.2V20" />
    <circle cx="12" cy="9" r="2" />
    <path d="M8.3 14a3.8 3.8 0 0 1 7.4 0" />
  </>,
  microphone: <>
    <rect x="9" y="3.7" width="6" height="11" rx="3" />
    <path d="M6 11.5V13a6 6 0 0 0 12 0v-1.5M12 19v2M9 21h6" />
  </>,
  'arrow-up': <path d="M12 19V5m-5.5 5.5L12 5l5.5 5.5" />,
  'arrow-left': <path d="M19 12H5m5.5-5.5L5 12l5.5 5.5" />,
  close: <path d="m6.5 6.5 11 11m0-11-11 11" />,
  shield: <path d="M12 3.6 5.2 6.2v5.2c0 4.3 2.9 7.7 6.8 9 3.9-1.3 6.8-4.7 6.8-9V6.2Zm-3 8.6 2.1 2.1 4-4.1" />,
  'check-circle': <><circle cx="12" cy="12" r="8.2" /><path d="m8.6 12.2 2.3 2.3 4.5-4.6" /></>,
  'more-horizontal': <g fill="currentColor" stroke="none"><circle cx="5" cy="12" r="1.35" /><circle cx="12" cy="12" r="1.35" /><circle cx="19" cy="12" r="1.35" /></g>,
  spark: <path fill="currentColor" stroke="none" d="M12 2.7c.37 0 .6.28.77.75l1.36 3.81a4.9 4.9 0 0 0 2.94 2.94l3.48 1.25c.47.17.75.4.75.77s-.28.6-.75.77l-3.48 1.25a4.9 4.9 0 0 0-2.94 2.94l-1.36 3.37c-.17.47-.4.75-.77.75s-.6-.28-.77-.75l-1.36-3.37a4.9 4.9 0 0 0-2.94-2.94l-3.48-1.25c-.47-.17-.75-.4-.75-.77s.28-.6.75-.77l3.48-1.25a4.9 4.9 0 0 0 2.94-2.94l1.36-3.81c.17-.47.4-.75.77-.75Z" />,
  loader: <path d="M20 12a8 8 0 1 1-8-8" />,
  bell: <>
    <path d="M6.2 16.5V11a5.8 5.8 0 0 1 11.6 0v5.5l1.4 1.6H4.8Z" />
    <path d="M10 20.2a2.2 2.2 0 0 0 4 0" />
  </>,
  grid: <>
    <rect x="4" y="4" width="6.5" height="6.5" rx="1.6" /><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.6" />
    <rect x="4" y="13.5" width="6.5" height="6.5" rx="1.6" /><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.6" />
  </>,
  devices: <>
    <rect x="3" y="5" width="13.5" height="10" rx="1.8" />
    <path d="M6.5 19h6.2" />
    <rect x="16" y="9" width="5" height="10.5" rx="1.4" />
  </>,
  lock: <>
    <rect x="5" y="10.5" width="14" height="9.8" rx="2.2" />
    <path d="M8.3 10.5V8a3.7 3.7 0 0 1 7.4 0v2.5M12 14.4v2" />
  </>,
  help: <>
    <circle cx="12" cy="12" r="8.2" />
    <path d="M9.7 9.6a2.4 2.4 0 0 1 4.6.9c0 1.6-2.3 2-2.3 3.5" />
    <circle cx="12" cy="16.9" r=".6" fill="currentColor" stroke="none" />
  </>,
  logout: <>
    <path d="M10 4.8H7a2.2 2.2 0 0 0-2.2 2.2v10A2.2 2.2 0 0 0 7 19.2h3" />
    <path d="M14.5 8 18.5 12l-4 4M18.3 12H9.5" />
  </>,
  sun: <>
    <circle cx="12" cy="12" r="3.6" />
    <path d="M12 3.5v1.8M12 18.7v1.8M3.5 12h1.8M18.7 12h1.8M6 6l1.3 1.3M16.7 16.7 18 18M6 18l1.3-1.3M16.7 7.3 18 6" />
  </>,
  moon: <path d="M19.3 14.6A7.6 7.6 0 0 1 9.4 4.7a7.6 7.6 0 1 0 9.9 9.9Z" />,
  monitor: <>
    <rect x="3.5" y="4.5" width="17" height="11.8" rx="2" />
    <path d="M9 19.5h6M12 16.3v3.2" />
  </>,
  'chevron-right': <path d="m10 6.5 5.5 5.5-5.5 5.5" />,
  'arrow-up-right': <path d="M8 16 16 8m-6.5 0H16v6.5" />,
  download: <path d="M12 4.5v10.2m-4.3-4.3 4.3 4.3 4.3-4.3M5 19.5h14" />,
  user: <>
    <circle cx="12" cy="8.4" r="3.6" />
    <path d="M5.2 19.6a6.8 6.8 0 0 1 13.6 0" />
  </>,
};

export default function Icon({ name, size = 24, className, ...props }) {
  return <svg {...props} className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{drawings[name] || drawings.chat}</svg>;
}
