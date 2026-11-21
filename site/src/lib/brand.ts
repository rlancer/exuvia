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

/** The mark: two paths that cross, on a 32 x 32 grid. Drawn by components/Mark.astro. */
export const MARK = {
  box: 32,
  path: "M7 9c8 0 10 14 18 14M7 23c8 0 10-14 18-14",
  stroke: 3,
};

/** The mark as a file of its own, in one colour. `tile` puts it on a rounded square, as the favicon has it. */
export function markSvg(colour: string, tile?: string): string {
  const cap = tile ? "round" : "square";
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${MARK.box} ${MARK.box}">` +
    (tile ? `<rect width="${MARK.box}" height="${MARK.box}" rx="7" fill="${tile}"/>` : "") +
    `<path d="${MARK.path}" stroke="${colour}" stroke-width="${MARK.stroke}" fill="none" stroke-linecap="${cap}"/></svg>`
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

/** The colours that mean something. Each has one job, the same one it has in the app. */
export const SIGNALS = swatches(APP, [
  ["--accent", "Amber", "The mark, and the one thing to do next. In the app, also a node that needs a run."],
  ["--fresh", "Green", "A node whose output is up to date."],
  ["--running", "Blue", "A node at work, and the lobster."],
]);

/** The landing page's palette: the same roles, with the greys leaned to navy and the lobster's blue as the accent. */
export const WATER_PALETTE = swatches(WATER, [
  ["--bg", "Water", "The ground of the landing page, under the sea drawn on it."],
  ["--bg-deep", "Deep water", "Code, and the bands that set a section apart."],
  ["--panel", "Panel", "A panel in the water."],
  ["--panel-2", "Raised", "A panel that is raised or pointed at."],
  ["--line", "Line", "The quiet border."],
  ["--line-strong", "Strong line", "Borders of controls."],
  ["--text", "Text", "Body text and headings."],
  ["--muted", "Muted", "Paragraphs of second rank."],
  ["--accent", "Lobster blue", "The accent on the landing page: the mark, links and the one thing to do next."],
  ["--accent-hover", "Light blue", "The accent when it is pointed at."],
]);

/** The lobster's facets in shadow (components/PolyMolts.astro). */
export const LOBSTER = { ink: APP["--running"], shade: "#1c2c5e" };

/** Black or white, whichever reads on `hex`. */
export function inkOn(hex: string): string {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? "#111214" : "#e9e7e1";
}

// ---- the files the brand page offers ------------------------------------------

/** The ground of the app icon's tile. */
const TILE = "#14161a";

export type BrandFile = { file: string; what: string; svg: string };

/** The mark in each colour it is used in. Served from /brand/ (pages/brand/[file].svg.ts). */
export const MARK_FILES: BrandFile[] = [
  { file: "exuvia-mark-amber.svg", what: "Amber, for the app's dark grounds", svg: markSvg(APP["--accent"]) },
  { file: "exuvia-mark-blue.svg", what: "Lobster blue, for the water", svg: markSvg(WATER["--accent"]) },
  { file: "exuvia-mark-light.svg", what: "One colour, for any other dark ground", svg: markSvg(APP["--text"]) },
  { file: "exuvia-mark-ink.svg", what: "One colour, for a light ground", svg: markSvg(APP["--bg"]) },
  { file: "exuvia-icon.svg", what: "The app icon: the mark on its tile", svg: icon() },
];

/** The app icon, which is also the site's favicon (pages/favicon.svg.ts). */
export function icon(): string {
  return markSvg(APP["--accent"], TILE);
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
