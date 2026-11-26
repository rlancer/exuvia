/**
 * The docs portal is built from the root `README.md`, the same file PyPI shows.
 *
 * Each `## ` section listed in PAGES becomes one page. A section that is in
 * neither PAGES nor OMITTED fails the build, so a section added to the README
 * is published or left out on purpose, never dropped by accident. The one page
 * that is not a README section is the tutorial in `src/docs/getting-started.md`.
 *
 * Screenshots of the app (`src/assets/shots/`, taken by `tools/capture.mjs`)
 * go into README pages through FIGURES, so the README itself stays text for
 * PyPI. The tutorial places its own as Markdown images with a `shot:` address.
 */
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { Lexer, Marked, type Tokens } from "marked";
import { codeFigure, escapeHtml, getHighlighter } from "./highlight";
import { absolute, markdownPath, url } from "./paths";
import { SITE } from "./project";
import { DEPLOY_TARGETS, EXAMPLE_HOSTNAME } from "./targets";

export { DEPLOY_TARGETS, EXAMPLE_HOSTNAME };

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
 * block is a prompt the landing page offers to copy, and whose ```text file=…
 * block is the steps that prompt has the agent fetch from this site. */
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
    slug: "packages",
    group: "Build",
    source: { heading: "Packages" },
    summary: "The packages nodes import, listed in the project's pyproject.toml, private package indexes included.",
  },
  {
    slug: "environment",
    group: "Build",
    source: { heading: "Environment variables and secrets" },
    title: "Environment and secrets",
    summary: "API keys, tokens and settings for your nodes: set in the workbench, kept out of code and masked in run logs, and shared by a hub group with its projects.",
  },
  {
    slug: "run-parameters",
    group: "Build",
    source: { heading: "Run parameters and replays" },
    summary: "Declare run parameters, keep one output per value, and replay date ranges.",
  },
  {
    slug: "partitioned-nodes",
    group: "Build",
    source: { heading: "Partitioned nodes: a history that grows each run" },
    title: "Partitioned nodes",
    summary: "Nodes whose runs add, replace or merge by key into a day (or any partition) of one output: daily runs, backfills and corrections without rewriting the history.",
  },
  {
    slug: "keys",
    group: "Build",
    source: { heading: "Keys: no row twice" },
    title: "Keys",
    summary: "Declare the columns that identify a row, and a run that would store a duplicate or a null key fails instead.",
  },
  {
    slug: "push-nodes",
    group: "Build",
    source: { heading: "Push nodes: data from someone's desktop" },
    title: "Push nodes",
    summary: "Data only one desktop can read, such as a Bloomberg Terminal's: pushed to the hub from that machine, then cascaded like any output.",
  },
  {
    slug: "views",
    group: "Build",
    source: { heading: "Views" },
    summary: "Your own interface over node outputs, written as front-end code: React and TypeScript files, sandboxed and read-only.",
  },
  {
    slug: "live-nodes",
    group: "Build",
    source: { heading: "Live nodes: current data when a view opens" },
    title: "Live nodes",
    summary: "Nodes that re-run whenever a view that reads them opens: the view shows what it has at once, then updates.",
  },
  {
    slug: "downloads",
    group: "Build",
    source: { heading: "Downloads" },
    summary: "Save any table as CSV, Excel, Parquet or JSON, and the project's code as a zip.",
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
    source: { heading: "Command line" },
    summary: "Every exuvia command, running a project without the UI in CI or from cron, and updating exuvia.",
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
    summary: "Serve every project under one directory from a single process, shared with people and grouped into folders that share access and secrets.",
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

type Figure = {
  /** A screenshot in src/assets/shots/, without `.png`. */
  shot: string;
  /** The figure goes after the paragraph, list or heading whose first line starts with this. */
  after: string;
  alt: string;
  caption: string;
};

/**
 * Screenshots in README pages, by page slug. The build fails when a figure's
 * `after` text is no longer in its section, or its screenshot is missing.
 * In the tutorial the same thing is `![alt](shot:<name> "caption")`.
 */
const FIGURES: Record<string, Figure[]> = {
  nodes: [
    {
      shot: "files",
      after: "The workbench's side panel has three tabs",
      alt: "The Files tab of the side panel: nodes/, lib/ and views/vol_board/ as one tree. lib/stats.py is open in place of the graph, used by rolling_vol.",
      caption: "The Files tab. A helper opens in place of the graph, with the nodes that import it.",
    },
  ],
  packages: [
    {
      shot: "packages",
      after: "**Adding a package in the workbench.**",
      alt: "The Packages dialog over the workbench: a field to add packages, scipy 1.18.1 installed, and buttons to edit pyproject.toml and show the install log.",
      caption: "The Packages button in the top bar: scipy added and installed for this project alone.",
    },
  ],
  views: [
    {
      shot: "views",
      after: "A **view** is custom UI over node outputs",
      alt: "A view named Volatility board: summary tiles, a table of sectors ranked by average volatility, and a line chart for the selected sector.",
      caption: "The example project's view, Volatility board. It reads sector_summary, rolling_vol and universe, and nothing else.",
    },
    {
      shot: "view-code",
      after: "The view's **Code** tab lists its files",
      alt: "The Code tab of the Volatility board view: its five files on the left, App.tsx open in the editor with useTable and useQuery calls.",
      caption: "A view's Code tab: its files, edited in place, with a History button for the one that is open.",
    },
  ],
  downloads: [
    {
      shot: "downloads",
      after: "Anything you can see in the workbench you can save as a file.",
      alt: "The Data tab of rolling_vol, with the Download menu open under it: CSV, Excel, Parquet and JSON, each with what it is for.",
      caption: "Every row of a node's output, in the format the next tool wants.",
    },
  ],
  agent: [
    {
      shot: "agent",
      after: "The agent runs a tool loop over the live workspace",
      alt: "The workbench after an agent turn. The agent panel lists its steps: looked at the graph, read prices, wrote drawdown, ran drawdown, then a summary. The new drawdown node is selected on the canvas, with its chart.",
      caption: "Asked for a drawdown node, the agent read the graph and the upstream code, wrote the node, ran it and said what it made.",
    },
  ],
  "execution-model": [
    {
      shot: "history",
      after: "Each lists the revisions newest first",
      alt: "The History tab of rolling_vol: two revisions, the newer one saved from the workbench, and a diff against the version before it that swaps an inline rolling standard deviation for an import from lib.stats.",
      caption: "A node's History tab, showing what one save changed. Restore makes a version current again.",
    },
  ],
  hub: [
    {
      shot: "hub",
      after: "`exuvia hub <dir>` serves every project under one directory",
      alt: "The hub's home page: three projects, Customer churn, Credit risk and Markets, each with its node count, owner and who can edit it.",
      caption: "The hub's home page, for a signed-in person: their own project and two shared with them.",
    },
    {
      shot: "share",
      after: "Each project on a hub is one workflow, and it is shared the way a document",
      alt: "The Share dialog for the Markets project: a request for edit access with a note, the people with access and their roles, general access set to only people added here, and a share link that expires in a month.",
      caption: "The Share dialog: an access request to answer, people and their roles, general access, and a read-only link.",
    },
  ],
};

const SHOTS_DIR = path.join(process.cwd(), "src", "assets", "shots");
const SHOT_IMAGE = /^!\[([^\]]*)\]\(shot:([\w-]+) "([^"]*)"\)$/;

/** A section's Markdown with its FIGURES written in as `shot:` images. */
function withFigures(slug: string, markdown: string): string {
  const lines = markdown.split("\n");
  for (const figure of FIGURES[slug] ?? []) {
    let fenced = false;
    const start = lines.findIndex((line) => {
      if (line.trimStart().startsWith("```")) fenced = !fenced;
      return !fenced && line.startsWith(figure.after);
    });
    if (start < 0) {
      throw new Error(`FIGURES in site/src/lib/docs.ts: the "${slug}" page has no paragraph starting "${figure.after}".`);
    }
    let end = start + 1;
    while (end < lines.length && lines[end].trim() !== "") end++;
    if (/["\]]/.test(figure.alt + figure.caption)) {
      throw new Error(`FIGURES in site/src/lib/docs.ts: the ${figure.shot} figure's alt and caption can't hold " or ].`);
    }
    lines.splice(end, 0, "", `![${figure.alt}](shot:${figure.shot} "${figure.caption}")`);
  }
  return lines.join("\n");
}

/** A `shot:` image as the docs page shows it. The page puts the real image in place of the `data-shot` one. */
function shotFigure(slug: string, name: string, alt: string, caption: string): string {
  if (!existsSync(path.join(SHOTS_DIR, `${name}.png`))) {
    throw new Error(`"${slug}" shows the screenshot ${name}, but src/assets/shots/${name}.png does not exist.`);
  }
  return `<figure class="shot"><img data-shot="${name}" alt="${alt}"><figcaption>${caption}</figcaption></figure>\n`;
}

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

/** A README section, or one `### ` subsection of it. */
function readmeText(heading: string, subheading?: string): string {
  let text = (readmeSections().get(heading) ?? "").replace(/\r\n/g, "\n");
  if (subheading) {
    const start = text.split("\n").findIndex((line) => line.trimEnd() === `### ${subheading}`);
    text = start < 0 ? "" : text.split("\n").slice(start + 1).join("\n").split(/^### /m)[0];
  }
  return text;
}

/** The first ```text block of a README section (`file` unset), or its ```text file=<file> block. */
function textBlock(heading: string, subheading?: string, file?: string): string {
  const where = subheading ? `"## ${heading}" > "### ${subheading}"` : `"## ${heading}"`;
  const info = file ? `text file=${file}` : "text";
  const open = info.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const block = new RegExp(`^\`\`\`${open}\n([\\s\\S]*?)\n\`\`\`$`, "m").exec(readmeText(heading, subheading));
  if (!block) throw new Error(`README.md ${where} has no \`\`\`${info} block.`);
  return block[1];
}

/**
 * The setup prompt people paste into their coding agent, read from the README
 * so the landing page and the docs page show one text.
 */
export function agentSetupPrompt(): string {
  return checkPrompt(textBlock(AGENT_SETUP));
}

/** The prompt that has an agent deploy a hub to a server or cluster, read from the README. */
export function agentDeployPrompt(): string {
  const prompt = checkPrompt(textBlock(DEPLOY.heading, DEPLOY.subheading));
  // The landing page fills in the reader's hostname and target by swapping these.
  if (!prompt.includes(EXAMPLE_HOSTNAME) || !prompt.includes(`${SITE}/agent/deploy/${DEPLOY_TARGETS[0].id}.txt`)) {
    throw new Error(
      `The README's deploy prompt must name the host ${EXAMPLE_HOSTNAME} and ` +
        `${SITE}/agent/deploy/${DEPLOY_TARGETS[0].id}.txt: the landing page replaces them.`,
    );
  }
  return prompt;
}

export type AgentFile = { path: string; text: string };

/**
 * The steps the prompts send an agent to, as plain text files under /agent/:
 * the setup steps as written, and the deploy steps once per target.
 */
export function agentFiles(): AgentFile[] {
  const deploy = textBlock(DEPLOY.heading, DEPLOY.subheading, "agent/deploy/<target>.txt");
  return [
    { path: "/agent/setup.txt", text: textBlock(AGENT_SETUP, undefined, "agent/setup.txt") + "\n" },
    ...DEPLOY_TARGETS.map((target) => ({
      path: `/agent/deploy/${target.id}.txt`,
      text: stepsFor(deploy, target.label).replaceAll("<target>", target.name) + "\n",
    })),
  ];
}

/** Fails the build when a prompt sends the agent to a file under this site that the build does not make. */
function checkPrompt(prompt: string): string {
  const site = SITE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const made = new Set([
    "/agent/setup.txt",
    ...DEPLOY_TARGETS.map((t) => `/agent/deploy/${t.id}.txt`),
  ]);
  for (const m of prompt.matchAll(new RegExp(`${site}(/\\S*)`, "g"))) {
    if (!made.has(m[1])) throw new Error(`A README prompt links to ${m[0]}, which the site does not serve.`);
  }
  return prompt;
}

const TARGET_ITEM = /^( +)- ((?:Compose|Swarm|Kubernetes)(?:(?:, | and )(?:Compose|Swarm|Kubernetes))*): (.*)$/;

/**
 * One target's copy of the deploy steps. A list item that starts with target
 * names (`- Compose: …`, `- Compose and Swarm: …`) belongs to those targets:
 * the others' items are dropped, and the target's own loses its label and
 * becomes a plain line of its step. Every run of such items has to cover each
 * target exactly once, so a step can't leave one out by mistake.
 */
function stepsFor(steps: string, label: string): string {
  const lines = steps.split("\n");
  const out: string[] = [];
  let run: string[] = [];
  const closeRun = (where: string) => {
    if (!run.length) return;
    for (const t of DEPLOY_TARGETS) {
      const n = run.filter((l) => l === t.label).length;
      if (n !== 1) {
        throw new Error(`The README's deploy steps name ${t.label} ${n} times in the items before "${where}".`);
      }
    }
    run = [];
  };
  for (let i = 0; i < lines.length; ) {
    const m = TARGET_ITEM.exec(lines[i]);
    if (!m) {
      closeRun(lines[i].trim());
      out.push(lines[i++]);
      continue;
    }
    const [, indent, names, first] = m;
    const labels = names.split(/, | and /);
    run.push(...labels);
    const body = [indent + first];
    for (i++; i < lines.length && lines[i].trim() && lines[i].search(/\S/) > indent.length; i++) {
      body.push(lines[i].replace(/^  /, ""));
    }
    if (labels.includes(label)) out.push(...body);
  }
  closeRun("the end");
  return out.join("\n");
}

/**
 * The page's Markdown as an agent reads it on its own: a `# title` and the
 * summary on top, README headings moved up a level to sit under that title,
 * and README `#anchor` links pointed at the Markdown page that holds them.
 * Code fences are left exactly as written.
 */
function agentMarkdown(spec: PageSpec, title: string, markdown: string, anchors: Map<string, string>): string {
  const fromReadme = "heading" in spec.source;
  // Screenshots are for the HTML page: an agent reading the Markdown gets the text.
  markdown = markdown
    .replace(/\r\n/g, "\n")
    .replace(/^!\[[^\]]*\]\(shot:.*\)\n\n?/gm, "");
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

  for (const slug of Object.keys(FIGURES)) {
    const spec = PAGES.find((p) => p.slug === slug);
    if (!spec || !("heading" in spec.source)) {
      throw new Error(`FIGURES in site/src/lib/docs.ts names "${slug}", which is not a README page.`);
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
        paragraph(token) {
          const shot = SHOT_IMAGE.exec(token.raw.trim());
          if (!shot) return `<p>${this.parser.parseInline(token.tokens)}</p>\n`;
          const [, alt, name, caption] = shot;
          return shotFigure(spec.slug, name, escapeHtml(alt), this.parser.parseInline(Lexer.lexInline(caption)));
        },
        image(token) {
          if (token.href.startsWith("shot:")) {
            throw new Error(`"${spec.slug}" has a shot: image inside a paragraph. Give it a paragraph of its own.`);
          }
          return `<img src="${escapeHtml(token.href)}" alt="${escapeHtml(token.text)}">`;
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
    const shown = "heading" in spec.source ? withFigures(spec.slug, markdown) : markdown;
    const html = marked.parse(shown, { async: false });
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
