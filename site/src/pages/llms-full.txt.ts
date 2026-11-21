import type { APIRoute } from "astro";
import { getDocs } from "../lib/docs";
import { absolute } from "../lib/paths";
import { SUMMARY } from "../lib/project";

// /llms-full.txt: every docs page's Markdown in one file, in reading order,
// for an agent that wants the whole manual in its context at once.
export const GET: APIRoute = async () => {
  const docs = await getDocs();
  const head = `# exuvia documentation\n\n> ${SUMMARY}\n\nIndex: ${absolute("/llms.txt")}`;
  const pages = docs.map((d) => `<!-- ${absolute(`/docs/${d.slug}/`)} -->\n\n${d.markdown.trim()}`);
  return new Response([head, ...pages].join("\n\n---\n\n") + "\n", {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
};
