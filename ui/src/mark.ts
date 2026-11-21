/**
 * The exuvia mark: a lobster's claw, open, on a 32 x 32 grid, in ten flat
 * facets of the lobster's blue. `outline` is the same claw as one shape, for
 * a place with one ink. The website draws the same mark from
 * `site/src/lib/brand.ts`; brand.test.ts fails when the two drift apart.
 *
 * Plain data, no React: vite.config.ts also reads it, for the favicon.
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
} as const;

/** The mark as a file of its own: what the browser tab shows. */
export function markSvg(): string {
  const facets = MARK.facets.map(([d, tone]) => `<path d="${d}" fill="${tone}" stroke="${tone}"/>`).join("");
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${MARK.box} ${MARK.box}">` +
    `<g stroke-width="0.6" stroke-linejoin="round">${facets}</g></svg>`
  );
}
