/**
 * The exuvia palette, as [light, dark] pairs. The one place a colour of the
 * app is written down: the Astryx theme (exuviaTheme.ts) maps these onto its
 * semantic tokens, and the website wears the dark column
 * (site/src/styles/global.css; brand.test.ts fails when the two drift apart).
 *
 * One accent, the lobster's blue of the exuvia mark, for links, the selection
 * and the one thing to do next. Green, amber and red are states of a node
 * (fresh, stale, failed) and are never used as decoration.
 */
export const BRAND = {
  bg: ["#f6f5f1", "#111214"],
  panel: ["#ffffff", "#17181b"],
  panel2: ["#f0eee8", "#1d1f23"],
  line: ["#e2dfd6", "#2a2c31"],
  lineStrong: ["#cfcbbf", "#3a3d44"],
  text: ["#1b1c1f", "#e9e7e1"],
  muted: ["#6c6a63", "#9b998f"],
  faint: ["#9a978d", "#6d6b64"],
  accent: ["#3b6fd8", "#6f9bff"],
  accentHover: ["#2f5fc4", "#94b5ff"],
  accentInk: ["#ffffff", "#0a1230"],
  accentSoft: ["#e1e9fb", "#1c2748"],
  fresh: ["#2f8f5b", "#4cc38a"],
  stale: ["#c98a12", "#f2b84b"],
  failed: ["#c4413a", "#ef6b62"],
  running: ["#3b6fd8", "#6f9bff"],
  codeBg: ["#fbfaf7", "#141518"],
} as const satisfies Record<string, readonly [string, string]>;

export type BrandColor = keyof typeof BRAND;
