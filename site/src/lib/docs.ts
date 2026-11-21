/**
 * The docs portal is built from the root `README.md`, the same file PyPI shows.
 *
 * Each `## ` section listed in PAGES becomes one page. A section that is in
 * neither PAGES nor OMITTED fails the build, so a section added to the README
 * is published or left out on purpose, never dropped by accident. The one page
 * that is not a README section is the tutorial in `src/docs/getting-started.md`.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { Marked, type Tokens } from "marked";
import { codeFigure, escapeHtml, getHighlighter } from "./highlight";
import { absolute, markdownPath, url } from "./paths";

type PageSpec = {
  slug: string;
  group: string;
  /** README `## ` heading this page is cut from, or a file under src/docs/. */
  source: { heading: string } | { file: string };
  /** Shorter name for the sidebar and the page title. Defaults to the heading. */
  title?: string;
  summary: string;
};

/** README sections (and, within one, the `### ` subsection) whose first ```text
 * block is a prompt the landing page offers to copy. */
const AGENT_SETUP = "Set up with your agent";
const DEPLOY = { heading: "Deploying", subheading: "Hand it to an agent" };

const PAGES: PageSpec[] = [
  {
    slug: "getting-started",
    group: "Start",
    source: { file: "getting-started.md" },
    title: "Getting started",
    summary: "Install exuvia, build a three-node graph, put a view on it, and turn on the agent.",
  },
  {
    slug: "agent-setup",
    group: "Start",
    source: { heading: AGENT_SETUP },
    summary: "A prompt to paste into Claude Code, Cursor or any coding agent: it installs exuvia, starts the workbench and connects itself to the graph.",
  },
  {
    slug: "nodes",
    group: "Build",
    source: { heading: "Nodes" },
    summary: "Python and SQL node files, how dependencies are read, and shared helpers.",
  },
  {
    slug: "run-parameters",
    group: "Build",
    source: { heading: "Run parameters and replays" },
    summary: "Declare run parameters, keep one output per value, and replay date ranges.",
  },
  {
    slug: "views",
    group: "Build",
    source: { heading: "Views" },
    summary: "Your own interface over node outputs, written as front-end code: React and TypeScript files, sandboxed and read-only.",
  },
  {
    slug: "agent",
    group: "Build",
    source: { heading: "The agent" },
    summary: "The agent's tools, model providers, project notes and sessions.",
  },
  {
    slug: "execution-model",
    group: "Run",
    source: { heading: "Execution model" },
    summary: "Content-addressed outputs, derived freshness, schedules, cascade and revisions.",
  },
  {
    slug: "command-line",
    group: "Run",
    source: { heading: "Headless" },
    title: "Command line",
    summary: "Run and inspect a project without the UI, in CI or from cron.",
  },
  {
    slug: "architecture",
    group: "Run",
    source: { heading: "Architecture: control plane + executors" },
    title: "Architecture",
    summary: "The stateful control plane, stateless executors, and the executor protocol.",
  },
  {
    slug: "deploying",
    group: "Run",
    source: { heading: "Deploying" },
    summary: "The published image, ready hubs for Compose, Swarm and Kubernetes, auth, and every environment variable.",
  },
  {
    slug: "records",
    group: "Run",
    source: { heading: "Records: SQLite, Postgres, or your own adapter" },
    title: "Records",
    summary: "Where run history, schedules, sessions and code revisions are stored.",
  },
  {
    slug: "hub",
    group: "Run",
    source: { heading: "Hub: many projects from one process" },
    title: "Hub",
    summary: "Serve every project under one directory from a single process.",
  },
  {
    slug: "http-api",
    group: "Integrate",
    source: { heading: "Embedding / using it inside another app" },
    title: "HTTP API",
    summary: "Every route the UI uses, plus Arrow streams and stored Parquet files.",
  },
  {
    slug: "mcp",
    group: "Integrate",
    source: { heading: "MCP: the agent's tools in your own agent" },
    title: "MCP",
    summary: "Give Claude Code, Cursor or any MCP client the agent's tools: list, write and run nodes from your own agent.",
  },
  {
    slug: "status",
    group: "Project",
    source: { heading: "Status" },
    summary: "What exuvia does not do yet.",
  },
];

/** README sections that are deliberately not published, and why. */
const OMITTED: Record<string, string> = {
  Why: "covered by the landing page",
  "Quick start": "replaced by the Getting started tutorial, which has the same install commands",
  Contributing: "notes for working on exuvia from a clone: dev tasks, building the image, the site, releasing",
  License: "shown in the site footer",
};

export const GROUPS = ["Start", "Build", "Run", "Integrate", "Project"] as const;

export type TocEntry = { id: string; text: string; depth: number };

export type DocPage = {
  slug: string;
  group: string;
  title: string;
  summary: string;
  html: string;
  toc: TocEntry[];
  /** Plain text for the search index. */
  text: string;
  /** The page as standalone Markdown for agents, served at /docs/<slug>.md. */
  markdown: string;
};

const SITE_ROOT = process.cwd();

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .trim()
    .replace(/\s+/g, "-");
}

/** Splits the README into its `## ` sections, ignoring `##` inside code fences. */
function readmeSections(): Map<string, string> {
  const lines = readFileSync(path.join(SITE_ROOT, "..", "README.md"), "utf8").split("\n");
  const sections = new Map<string, string>();
  let heading: string | null = null;
  let body: string[] = [];
  let fenced = false;
  const flush = () => {
    if (heading !== null) sections.set(heading, body.join("\n").trim());
  };
  for (const line of lines) {
    if (line.trimStart().startsWith("```")) fenced = !fenced;
    const m = !fenced && /^## (.+?)\s*$/.exec(line);
    if (m) {
      flush();
      heading = m[1];
      body = [];
    } else if (heading !== null) {
      body.push(line);
    }
  }
  flush();
  return sections;
}

/** The first ```text block of a README section, or of one `### ` subsection of it. */
function textBlock(heading: string, subheading?: string): string {
  const where = subheading ? `"## ${heading}" > "### ${subheading}"` : `"## ${heading}"`;
  let text = (readmeSections().get(heading) ?? "").replace(/\r\n/g, "\n");
  if (subheading) {
    const start = text.split("\n").findIndex((line) => line.trimEnd() === `### ${subheading}`);
    text = start < 0 ? "" : text.split("\n").slice(start + 1).join("\n").split(/^### /m)[0];
  }
  const block = /^```text\n([\s\S]*?)\n```$/m.exec(text);
  if (!block) throw new Error(`README.md ${where} has no \`\`\`text block for the landing page to show.`);
  return block[1];
}

/**
 * The setup prompt people paste into their coding agent, read from the README
 * so the landing page and the docs page show one text.
 */
export function agentSetupPrompt(): string {
  return textBlock(AGENT_SETUP);
}

/** The prompt that has an agent deploy a hub to a server or cluster, read from the README. */
export function agentDeployPrompt(): string {
  return textBlock(DEPLOY.heading, DEPLOY.subheading);
}

/**
 * The page's Markdown as an agent reads it on its own: a `# title` and the
 * summary on top, README headings moved up a level to sit under that title,
 * and README `#anchor` links pointed at the Markdown page that holds them.
 * Code fences are left exactly as written.
 */
function agentMarkdown(spec: PageSpec, title: string, markdown: string, anchors: Map<string, string>): string {
  const fromReadme = "heading" in spec.source;
  let fenced = false;
  const body = markdown
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => {
      const fence = line.trimStart().startsWith("```");
      if (fence) fenced = !fenced;
      if (fenced || fence) return line;
      if (fromReadme) line = line.replace(/^#(#{2,5}) /, "$1 ");
      // The HTML renderer has already failed the build on an anchor no page holds.
      return line.replace(/\]\(#([^)\s]+)\)/g, (_, id: string) => {
        const [page, hash] = anchors.get(id)!.split("#");
        if (page === `/docs/${spec.slug}/`) return `](#${id})`;
        const slug = page.replace(/^\/docs\/|\/$/g, "");
        return `](${absolute(markdownPath(slug))}${hash ? `#${hash}` : ""})`;
      });
    })
    .join("\n")
    .trim();
  return `# ${title}\n\n> ${spec.summary}\n\n${body}\n`;
}

function inlineText(tokens: Tokens.Generic[] | undefined): string {
  return (tokens ?? [])
    .map((t) => ("tokens" in t && t.tokens ? inlineText(t.tokens as Tokens.Generic[]) : "text" in t ? String(t.text) : ""))
    .join("");
}

let cache: Promise<DocPage[]> | undefined;

export function getDocs(): Promise<DocPage[]> {
  cache ??= build();
  return cache;
}

async function build(): Promise<DocPage[]> {
  const sections = readmeSections();
  const used = new Set(PAGES.flatMap((p) => ("heading" in p.source ? [p.source.heading] : [])));

  for (const heading of sections.keys()) {
    if (!used.has(heading) && !(heading in OMITTED)) {
      throw new Error(
        `README.md has a "## ${heading}" section the docs site does not know about. ` +
          `Add it to PAGES or OMITTED in site/src/lib/docs.ts.`,
      );
    }
  }
  for (const heading of [...used, ...Object.keys(OMITTED)]) {
    if (!sections.has(heading)) {
      throw new Error(`site/src/lib/docs.ts names a README section that no longer exists: "## ${heading}".`);
    }
  }

  // README links are `#anchor` within one long file. Here each section is a
  // page, so resolve an anchor to the page that now holds that heading.
  const anchors = new Map<string, string>();
  const sources = PAGES.map((spec) => {
    const markdown =
      "heading" in spec.source
        ? sections.get(spec.source.heading)!
        : readFileSync(path.join(SITE_ROOT, "src", "docs", spec.source.file), "utf8");
    if ("heading" in spec.source) anchors.set(slugify(spec.source.heading), `/docs/${spec.slug}/`);
    for (const m of markdown.matchAll(/^#{2,4} (.+?)\s*$/gm)) {
      const id = slugify(m[1].replace(/`/g, ""));
      if (!anchors.has(id)) anchors.set(id, `/docs/${spec.slug}/#${id}`);
    }
    return { spec, markdown };
  });

  const highlighter = await getHighlighter();

  return sources.map(({ spec, markdown }) => {
    const toc: TocEntry[] = [];
    const marked = new Marked({
      gfm: true,
      renderer: {
        heading(token) {
          const text = inlineText(token.tokens as Tokens.Generic[]);
          const id = slugify(text);
          // README sections start at h3 (their h2 is the page title); the
          // tutorial is its own file and starts at h2.
          if (token.depth <= 3) toc.push({ id, text, depth: token.depth });
          const inner = this.parser.parseInline(token.tokens);
          return `<h${token.depth} id="${id}"><a class="anchor" href="#${id}">${inner}</a></h${token.depth}>\n`;
        },
        code(token) {
          const [lang, ...meta] = (token.lang ?? "").split(/\s+/);
          const file = meta.find((m) => m.startsWith("file="))?.slice(5);
          return codeFigure(highlighter, token.text, lang, file);
        },
        link(token) {
          let href = token.href;
          if (href.startsWith("#")) {
            const target = anchors.get(href.slice(1));
            if (!target) {
              throw new Error(`"${spec.slug}" links to ${href}, which is not on any published docs page.`);
            }
            href = target.startsWith(`/docs/${spec.slug}/#`) ? target.slice(target.indexOf("#")) : url(target);
          }
          const external = /^https?:/.test(href);
          const inner = this.parser.parseInline(token.tokens);
          return `<a href="${escapeHtml(href)}"${external ? ' rel="noopener"' : ""}>${inner}</a>`;
        },
        table(token) {
          const cell = (c: Tokens.TableCell, tag: string) => `<${tag}>${this.parser.parseInline(c.tokens)}</${tag}>`;
          // A table whose header row is empty (`| | |`) is a key/value list.
          const hasHead = token.header.some((c) => c.text.trim() !== "");
          const head = hasHead ? `<thead><tr>${token.header.map((c) => cell(c, "th")).join("")}</tr></thead>` : "";
          const rows = token.rows.map((r) => `<tr>${r.map((c) => cell(c, "td")).join("")}</tr>`).join("");
          return `<div class="table-scroll" tabindex="0"><table>${head}<tbody>${rows}</tbody></table></div>\n`;
        },
      },
    });
    const html = marked.parse(markdown, { async: false });
    const title = spec.title ?? ("heading" in spec.source ? spec.source.heading : spec.slug);
    const text = html
      .replace(/<button[^>]*>.*?<\/button>/g, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/&amp;/g, "&")
      .replace(/\s+/g, " ")
      .trim();
    return {
      slug: spec.slug,
      group: spec.group,
      title,
      summary: spec.summary,
      html,
      toc,
      text,
      markdown: agentMarkdown(spec, title, markdown, anchors),
    };
  });
}
