import type { APIRoute, GetStaticPaths } from "astro";
import { getDocs, type DocPage } from "../../lib/docs";

// Every docs page as plain Markdown at /docs/<slug>.md, for agents and for
// the page's "Copy page" button. The HTML page links to it as an alternate.
export const getStaticPaths: GetStaticPaths = async () =>
  (await getDocs()).map((page) => ({ params: { slug: page.slug }, props: { page } }));

export const GET: APIRoute = ({ props }) =>
  new Response((props as { page: DocPage }).page.markdown, {
    headers: { "Content-Type": "text/markdown; charset=utf-8" },
  });
