import { defineTheme } from "@astryxdesign/core/theme";
import { BRAND } from "./brand";

type Pair = [string, string];
const pair = (c: readonly [string, string]): Pair => [c[0], c[1]];

/**
 * The exuvia theme for Astryx: the brand palette (brand.ts) on Astryx's
 * semantic tokens, the app's two typefaces, and a dense scale for a workbench.
 * Every component and every style in the app reads these tokens; nothing
 * else names a colour, a size or a font.
 *
 * States of a node are Astryx's statuses: fresh is success, stale is warning,
 * failed is error, and running is the accent (ui/state.tsx).
 *
 * The app loads the theme as built: after a change here run `bun run theme`,
 * which rewrites exuvia.css, exuvia.js and their declarations beside this
 * file. `bun run build` fails while they are behind.
 */
export const exuviaTheme = defineTheme({
  name: "exuvia",

  typography: {
    // A dense screen: 13px body, and small steps between sizes.
    scale: { base: 13, ratio: 1.125 },
    body: { family: "Inter Variable", fallbacks: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif' },
    // Headings are set in the mono, as on the website and as a node's name is.
    heading: { family: "JetBrains Mono Variable", fallbacks: '"Cascadia Code", ui-monospace, SFMono-Regular, Consolas, monospace', weight: "bold" },
    code: { family: "JetBrains Mono Variable", fallbacks: '"Cascadia Code", ui-monospace, SFMono-Regular, Consolas, monospace' },
  },

  radius: { base: 4, multiplier: 0.75 },

  tokens: {
    // Grounds, from the page up: the canvas, a panel on it, a raised or hovered panel.
    "--color-background-body": pair(BRAND.bg),
    "--color-background-surface": pair(BRAND.panel),
    "--color-background-card": pair(BRAND.panel),
    "--color-background-popover": [BRAND.panel[0], BRAND.panel2[1]],
    "--color-background-muted": pair(BRAND.panel2),
    "--color-background-inverted": [BRAND.text[0], BRAND.text[1]],
    "--color-border": pair(BRAND.line),
    "--color-border-emphasized": pair(BRAND.lineStrong),
    "--color-overlay": ["rgba(20, 20, 20, 0.35)", "rgba(0, 0, 0, 0.6)"],
    "--color-overlay-hover": ["rgba(27, 28, 31, 0.05)", "rgba(255, 255, 255, 0.05)"],
    "--color-overlay-pressed": ["rgba(27, 28, 31, 0.1)", "rgba(255, 255, 255, 0.1)"],
    "--color-neutral": ["rgba(27, 28, 31, 0.08)", "rgba(233, 231, 225, 0.14)"],
    "--color-skeleton": pair(BRAND.lineStrong),
    "--color-track": pair(BRAND.lineStrong),

    // Text and icons.
    "--color-text-primary": pair(BRAND.text),
    "--color-text-secondary": pair(BRAND.muted),
    "--color-text-disabled": pair(BRAND.faint),
    "--color-icon-primary": pair(BRAND.text),
    "--color-icon-secondary": pair(BRAND.muted),
    "--color-icon-disabled": pair(BRAND.faint),

    // The accent: the lobster's blue.
    "--color-accent": pair(BRAND.accent),
    "--color-on-accent": pair(BRAND.accentInk),
    "--color-accent-muted": pair(BRAND.accentSoft),
    "--color-text-accent": pair(BRAND.accent),
    "--color-icon-accent": pair(BRAND.accent),
    "--focus-outline-color": pair(BRAND.accent),
    "--focus-outline-offset": "2px",

    // States. A dot or a rule takes the brand's colour; text on a light page
    // takes a darker shade of it, to stay readable (4.5:1).
    "--color-info": pair(BRAND.running),
    "--color-on-info": pair(BRAND.accentInk),
    "--color-info-muted": pair(BRAND.accentSoft),
    "--color-text-info": pair(BRAND.accent),
    "--color-on-info-muted": pair(BRAND.text),
    "--color-success": pair(BRAND.fresh),
    "--color-on-success": ["#ffffff", "#06210f"],
    "--color-success-muted": ["#dff1e6", "#17301f"],
    "--color-on-success-muted": pair(BRAND.text),
    "--color-text-success": ["#237148", BRAND.fresh[1]],
    "--color-icon-success": pair(BRAND.fresh),
    "--color-border-success": pair(BRAND.fresh),
    "--color-warning": pair(BRAND.stale),
    "--color-on-warning": ["#1b1c1f", "#2a1c00"],
    "--color-warning-muted": ["#f8ecd2", "#33280f"],
    "--color-on-warning-muted": pair(BRAND.text),
    "--color-text-warning": ["#8a5d00", BRAND.stale[1]],
    "--color-icon-warning": pair(BRAND.stale),
    "--color-border-warning": pair(BRAND.stale),
    "--color-error": pair(BRAND.failed),
    "--color-on-error": ["#ffffff", "#2b0705"],
    "--color-error-muted": ["#f8e1df", "#3a1917"],
    "--color-on-error-muted": pair(BRAND.text),
    "--color-text-error": ["#b0352f", BRAND.failed[1]],
    "--color-icon-error": pair(BRAND.failed),
    "--color-border-error": pair(BRAND.failed),
    "--color-background-error-inverted": pair(BRAND.failed),

    // Controls: a workbench's height, a step under Astryx's default.
    "--size-element-sm": "24px",
    "--size-element-md": "28px",
    "--size-element-lg": "32px",

    "--shadow-low": "0 1px 2px light-dark(rgba(20, 20, 20, 0.06), rgba(0, 0, 0, 0.4)), 0 2px 8px light-dark(rgba(20, 20, 20, 0.05), rgba(0, 0, 0, 0.25))",
    "--shadow-med": "0 1px 2px light-dark(rgba(20, 20, 20, 0.06), rgba(0, 0, 0, 0.4)), 0 6px 18px light-dark(rgba(20, 20, 20, 0.06), rgba(0, 0, 0, 0.35))",
    "--shadow-high": "0 2px 4px light-dark(rgba(20, 20, 20, 0.08), rgba(0, 0, 0, 0.4)), 0 12px 32px light-dark(rgba(20, 20, 20, 0.12), rgba(0, 0, 0, 0.45))",
  },

  // What exuvia has and Astryx has no name for.
  localTokens: {
    /** The ground under code: an editor, a log, a block to copy. */
    "--exuvia-code-background": pair(BRAND.codeBg),
    /** The dots of the graph's canvas. */
    "--exuvia-canvas-dot": ["#d6d2c6", "#26282d"],
    /** The accent, pointed at. */
    "--exuvia-accent-hover": pair(BRAND.accentHover),
  },

  components: {
    heading: { base: { letterSpacing: "-0.035em", wordSpacing: "-0.18em" } },
  },
});
