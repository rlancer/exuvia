import type { APIRoute } from "astro";
import { GROUPS, getDocs } from "../lib/docs";
import { absolute, markdownPath } from "../lib/paths";
import { GITHUB, PYPI, SUMMARY } from "../lib/project";

// /llms.txt (https://llmstxt.org): the docs as an index an agent can read in
// one fetch, each entry linking to that page's Markdown.
export const GET: APIRoute = async () => {
  const docs = await getDocs();
  const sections = GROUPS.map((group) => {
    const entries = docs
      .filter((d) => d.group === group)
      .map((d) => `- [${d.title}](${absolute(markdownPath(d.slug))}): ${d.summary}`);
    return `## ${group}\n\n${entries.join("\n")}`;
  });
  const body = [
    "# exuvia",
    `> ${SUMMARY}`,
    [
      "exuvia is a Python package (`pip install exuvia`) with a browser workbench, a command line,",
      "an HTTP API and an MCP server. Each docs page below is plain Markdown. To have a coding agent",
      "install and set up exuvia, start with Agent setup.",
      "",
      `Source code: ${GITHUB}`,
      `Package: ${PYPI}`,
    ].join("\n"),
    ...sections,
    `## Optional\n\n- [Every docs page in one file](${absolute("/llms-full.txt")}): the pages above, concatenated in order.`,
  ];
  return new Response(body.join("\n\n") + "\n", { headers: { "Content-Type": "text/plain; charset=utf-8" } });
};
