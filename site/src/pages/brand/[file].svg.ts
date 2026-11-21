// The brand's SVG files, at /brand/<file>.svg: the mark in each of its
// colours and the five animals. They are drawn from src/lib/brand.ts while
// the site is built, so there is no copy of the mark to keep in step.
import type { APIRoute } from "astro";
import { ANIMAL_FILES, MARK_FILES } from "../../lib/brand";

export function getStaticPaths() {
  return [...MARK_FILES, ...ANIMAL_FILES].map(({ file, svg }) => ({
    params: { file: file.replace(/\.svg$/, "") },
    props: { svg },
  }));
}

export const GET: APIRoute = ({ props }) =>
  new Response(props.svg, { headers: { "Content-Type": "image/svg+xml; charset=utf-8" } });
