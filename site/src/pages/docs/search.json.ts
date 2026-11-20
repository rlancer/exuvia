import type { APIRoute } from "astro";
import { getDocs } from "../../lib/docs";
import { url } from "../../lib/paths";

// The docs search index: small enough to ship whole and search in the browser.
export const GET: APIRoute = async () => {
  const docs = await getDocs();
  const index = docs.map((d) => ({
    url: url(`/docs/${d.slug}/`),
    title: d.title,
    group: d.group,
    headings: d.toc.map(({ id, text }) => ({ id, text })),
    text: d.text,
  }));
  return new Response(JSON.stringify(index), { headers: { "Content-Type": "application/json" } });
};
