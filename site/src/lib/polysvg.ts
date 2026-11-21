// The low-poly animals (./poly.ts) as SVG: the same meshes, light and view as
// the canvases on the landing page, drawn once while the site is built. For
// the places that want a still picture and no script: the brand page, the
// files it offers for download, and the social cards.
import { build, extent, facet, project, stage } from "./poly";

export type PolySvg = { svg: string; width: number; height: number };

export interface PolyOptions {
  /** Which way the head points: 0 is right. The default is the hero's, head left and turned a little toward the viewer. */
  yaw?: number;
  /** Pixels to one model unit. */
  unit?: number;
  /** Or the width to draw it at, in pixels, which sets the unit. */
  fit?: number;
  /** Room left round the animal, in pixels. */
  pad?: number;
  /** Lit facets, from `shade` in shadow to `ink` in the light. Left out, the animal is an empty shell: its edges alone. */
  solid?: { ink: string; shade: string };
  /** The line of an empty shell. */
  line?: string;
  lineWidth?: number;
  /** A name for the picture. Left out, it is hidden from screen readers. */
  title?: string;
}

export const HEADING = Math.PI + 0.28;

const rgb = (hex: string) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
const round = (n: number) => +n.toFixed(1);

export function polySvg(name: string, options: PolyOptions = {}): PolySvg {
  const { yaw = HEADING, fit, pad = 4, solid, line = "currentColor", lineWidth = 1, title } = options;
  const { v, tris, edges } = build(stage(name));
  const box = extent(v, [yaw]);
  const unit = fit ? (fit - pad * 2) / (box.right - box.left) : (options.unit ?? 20);
  const width = Math.ceil((box.right - box.left) * unit + pad * 2);
  const height = Math.ceil((box.bottom - box.top) * unit + pad * 2);
  const screen = new Float32Array(v.length);
  project(v, yaw, unit, pad - box.left * unit, pad - box.top * unit, screen);
  const at = (i: number) => `${round(screen[i])} ${round(screen[i + 1])}`;

  let body: string;
  if (solid) {
    const [ink, shade] = [rgb(solid.ink), rgb(solid.shade)];
    const order = Array.from({ length: tris.length / 3 }, (_, f) => f);
    const depth = order.map((f) => screen[tris[f * 3] * 3 + 2] + screen[tris[f * 3 + 1] * 3 + 2] + screen[tris[f * 3 + 2] * 3 + 2]);
    order.sort((p, q) => depth[p] - depth[q]);
    // Each facet is stroked in its own colour, so no seam shows between two.
    body = `<g stroke-width="0.75" stroke-linejoin="round">${order
      .map((f) => {
        const [a, b, c] = [tris[f * 3] * 3, tris[f * 3 + 1] * 3, tris[f * 3 + 2] * 3];
        const colour = facet(screen, a, b, c, ink, shade);
        return `<path d="M${at(a)}L${at(b)}L${at(c)}Z" fill="${colour}" stroke="${colour}"/>`;
      })
      .join("")}</g>`;
  } else {
    let d = "";
    for (let e = 0; e < edges.length; e += 2) d += `M${at(edges[e] * 3)}L${at(edges[e + 1] * 3)}`;
    body = `<path d="${d}" fill="none" stroke="${line}" stroke-width="${lineWidth}" stroke-linecap="round"/>`;
  }
  const label = title ? `role="img" aria-label="${title}"` : `aria-hidden="true"`;
  return {
    svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" ${label}>${body}</svg>`,
    width,
    height,
  };
}
