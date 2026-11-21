/**
 * The brand in one place: the mark, and the colours, which are read out of
 * `src/styles/global.css` so that the brand page and the social cards print
 * the values the site is really painted with. A token that is renamed or
 * removed there fails the build here.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { STAGES } from "./poly";
import { polySvg } from "./polysvg";

/**
 * The mark: a lobster's claw, open, on a 32 x 32 grid. It is ten flat
 * facets, like the animals of src/lib/poly.ts, each in a tone of the
 * lobster's blue: the heavy finger on top catches the light, the underside
 * is in shadow. `outline` is the same claw as one shape, for one colour.
 * Drawn by components/Mark.astro.
 */
export const MARK = {
  box: 32,
  facets: [
    ["M3 18L8 9l8 6z", "#6f9bff"],
    ["M3 18l13-3L5 28z", "#4f76d6"],
    ["M5 28l11-13-6 15z", "#2f4a9a"],
    ["M8 9l10-6-2 12z", "#94b5ff"],
    ["M18 3l9-1-6 7z", "#c9d8ff"],
    ["M18 3l3 6-5 6z", "#7fa6ff"],
    ["M16 15l8-1-7 11z", "#4f76d6"],
    ["M24 14l6-4-5 10z", "#7fa6ff"],
    ["M24 14l1 6-8 5z", "#3a5bb4"],
    ["M16 15l1 10-7 5z", "#263d82"],
  ],
  outline: "M5 28L3 18 8 9l10-6 9-1-6 7-5 6 8-1 6-4-5 10-8 5-7 5z",
};

/**
 * What goes inside the mark's `<svg>`: its facets, or with `flat` the one
 * shape in that colour. Each facet is stroked in its own tone, so no seam
 * shows between two.
 */
export function markBody(flat?: string): string {
  if (flat) return `<path d="${MARK.outline}" fill="${flat}"/>`;
  const facets = MARK.facets.map(([d, tone]) => `<path d="${d}" fill="${tone}" stroke="${tone}"/>`).join("");
  return `<g stroke-width="0.6" stroke-linejoin="round">${facets}</g>`;
}

/** The mark as a file of its own: in its facets, or `flat` in one colour. `tile` puts it on a rounded square. */
export function markSvg({ flat, tile }: { flat?: string; tile?: string } = {}): string {
  const body = tile ? `<g transform="translate(5 5) scale(0.6875)">${markBody(flat)}</g>` : markBody(flat);
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${MARK.box} ${MARK.box}">` +
    (tile ? `<rect width="${MARK.box}" height="${MARK.box}" rx="7" fill="${tile}"/>` : "") +
    `${body}</svg>`
  );
}

const css = readFileSync(path.join(process.cwd(), "src/styles/global.css"), "utf8");

/** The custom properties set in the first `selector { … }` block of global.css. */
function tokens(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`global.css has no "${selector} {" block for the brand page to read.`);
  const block = css.slice(start, css.indexOf("}", start)).replace(/\/\*[\s\S]*?\*\//g, "");
  return Object.fromEntries([...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

const APP = tokens(":root");
const WATER = { ...APP, ...tokens("body:has(.sea)") };

export type Swatch = { token: string; name: string; value: string; job: string };

function swatches(from: Record<string, string>, rows: [token: string, name: string, job: string][]): Swatch[] {
  return rows.map(([token, name, job]) => {
    const value = from[token];
    if (!value) throw new Error(`global.css no longer sets ${token}, which the brand page shows.`);
    return { token, name, value, job };
  });
}

/** The app's own palette: the docs, and every screenshot of the workbench. */
export const APP_PALETTE = swatches(APP, [
  ["--bg", "Canvas", "The ground of every page, dotted like the app's canvas."],
  ["--bg-deep", "Deep", "One step down: code, and the bands that set a section apart."],
  ["--panel", "Panel", "A panel on the canvas."],
  ["--panel-2", "Raised", "A panel that is raised or pointed at."],
  ["--line", "Line", "The quiet border that boxes a panel."],
  ["--line-strong", "Strong line", "Borders of controls, and of anything on Deep."],
  ["--text", "Text", "Body text and headings."],
  ["--muted", "Muted", "Paragraphs of second rank, captions, an empty shell's outline."],
  ["--faint", "Faint", "Labels and small print."],
]);

/** The colours that mean something: the one accent, and the two states of a node that the site also shows. */
export const SIGNALS = swatches(APP, [
  ["--accent", "Lobster blue", "The accent: the lobster, the mark, links and the one thing to do next. In the app, a node at work."],
  ["--fresh", "Green", "A node whose output is up to date."],
  ["--stale", "Amber", "A node that needs a run. It is a state, and is never used as decoration."],
]);

/** The landing page's palette: the same roles, with the greys leaned to navy. */
export const WATER_PALETTE = swatches(WATER, [
  ["--bg", "Water", "The ground of the landing page, under the sea drawn on it."],
  ["--bg-deep", "Deep water", "Code, and the bands that set a section apart."],
  ["--panel", "Panel", "A panel in the water."],
  ["--panel-2", "Raised", "A panel that is raised or pointed at."],
  ["--line", "Line", "The quiet border."],
  ["--line-strong", "Strong line", "Borders of controls."],
  ["--text", "Text", "Body text and headings."],
  ["--muted", "Muted", "Paragraphs of second rank."],
  ["--accent", "Lobster blue", "The accent, as everywhere."],
  ["--accent-hover", "Light blue", "The accent when it is pointed at."],
]);

/** The lobster's facets in shadow (components/PolyMolts.astro). */
export const LOBSTER = { ink: APP["--running"], shade: "#1c2c5e" };

/** Black or white, whichever reads on `hex`. */
export function inkOn(hex: string): string {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.3 ? "#111214" : "#e9e7e1";
}

// ---- the files the brand page offers ------------------------------------------

/** The ground of the app icon's tile. */
const TILE = "#14161a";

export type BrandFile = { file: string; what: string; svg: string };

/** The mark in each form it is used in. Served from /brand/ (pages/brand/[file].svg.ts). */
export const MARK_FILES: BrandFile[] = [
  { file: "exuvia-mark.svg", what: "The mark in its facets, for any ground", svg: markSvg() },
  { file: "exuvia-mark-blue.svg", what: "One colour: the lobster's blue", svg: markSvg({ flat: APP["--running"] }) },
  { file: "exuvia-mark-light.svg", what: "One colour, for a dark ground", svg: markSvg({ flat: APP["--text"] }) },
  { file: "exuvia-mark-ink.svg", what: "One colour, for a light ground", svg: markSvg({ flat: APP["--bg"] }) },
  { file: "exuvia-icon.svg", what: "An icon: the mark on a dark tile", svg: markSvg({ tile: TILE }) },
];

/** The site's favicon (pages/favicon.svg.ts): the mark on nothing, so it sits on a light tab bar or a dark one. */
export function icon(): string {
  return markSvg();
}

/** The five animals as pictures: the lobster lit, the four before it as the shells they left. */
export const ANIMAL_FILES: BrandFile[] = STAGES.map((s, i) => {
  const living = i === STAGES.length - 1;
  return {
    file: living ? "exuvia-lobster.svg" : `exuvia-shell-${s.name.toLowerCase()}.svg`,
    what: `${s.name}, ${s.age}`,
    svg: polySvg(s.name, { unit: 28, pad: 8, title: `${s.name}, ${s.age}`, ...(living ? { solid: LOBSTER } : { line: APP["--muted"], lineWidth: 1.25 }) }).svg,
  };
});
