import type { APIRoute } from "astro";
import { DEPLOY_TARGETS, GROUPS, getDocs } from "../lib/docs";
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
      "Steps written for an agent to follow, as plain text:",
      `- ${absolute("/agent/setup.txt")}: set exuvia up on this machine`,
      ...DEPLOY_TARGETS.map((t) => `- ${absolute(`/agent/deploy/${t.id}.txt`)}: deploy a hub with ${t.name}`),
      "",
      `Source code: ${GITHUB}`,
      `Package: ${PYPI}`,
      "",
      `If exuvia misbehaves, a page here is wrong, or something you need is missing, report it at ${GITHUB}/issues.`,
      "The report_issue tool (MCP and the HTTP tools) drafts the issue for the user to submit. Reports are welcome.",
    ].join("\n"),
    ...sections,
    `## Optional\n\n- [Every docs page in one file](${absolute("/llms-full.txt")}): the pages above, concatenated in order.`,
  ];
  return new Response(body.join("\n\n") + "\n", { headers: { "Content-Type": "text/plain; charset=utf-8" } });
};
