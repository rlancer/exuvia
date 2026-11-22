/**
 * Captures the site's product screenshots from a running exuvia, and the
 * social cards from the site's own /card/ pages. Uses the Chrome (or Edge)
 * already on the machine, or the browser CHROME_PATH names, so there is no
 * browser download. Every product shot is 1440x900 at twice the pixels, in
 * the dark theme, and lands in src/assets/shots/.
 *
 *   # in exuvia/: serve a scratch copy of the example, with an agent key set
 *   cp -r examples/markets /tmp/markets && uv run exuvia serve /tmp/markets --port 7431
 *   # in exuvia/site/
 *   node tools/capture.mjs app http://127.0.0.1:7431
 *       -> workbench, downloads, history, packages, files, views, view-code, agent, agent-panel
 *
 *   # a hub on an empty directory, with sign-in from headers
 *   EXUVIA_AUTH=headers uv run exuvia hub /tmp/hub --port 7432
 *   node tools/capture.mjs hub http://127.0.0.1:7432           -> hub, share
 *
 *   # the getting-started project, from `exuvia init`
 *   uv run exuvia init /tmp/my-research && uv run exuvia serve /tmp/my-research --port 7433
 *   node tools/capture.mjs tutorial http://127.0.0.1:7433      -> tutorial-graph, tutorial-view
 *
 *   # with the site itself running (npm run dev): the card pages exist only under `astro dev`
 *   node tools/capture.mjs cards http://localhost:4380  -> public/og/<card>.png, one for every card in
 *                                                          src/lib/cards.ts, and public/brand/<lockup>.png
 *
 * Each product mode changes the project it runs against, which is why they
 * want scratch copies. `app` adds lib/stats.py, makes rolling_vol use it (so
 * History has a change to show) and adds scipy to the project's packages.
 * Its agent shots send one real prompt (the agent writes a node), which
 * spends a few cents of model credit; pass --no-agent to skip them. `hub`
 * signs in as three made-up people, creates three projects and shares one.
 * `tutorial` writes every `file=` block of src/docs/getting-started.md.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import sharp from "sharp";

const SITE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO = path.resolve(SITE, "..");
const SHOTS = path.join(SITE, "src/assets/shots");
const [mode, address, ...flags] = process.argv.slice(2);
if (!["app", "hub", "tutorial", "cards"].includes(mode) || !address) {
  console.error("usage: node tools/capture.mjs app <exuvia url> [--no-agent] | hub <hub url> | tutorial <exuvia url> | cards <site url>");
  process.exit(2);
}
const base = address.replace(/\/+$/, "");

async function launch() {
  if (process.env.CHROME_PATH) return chromium.launch({ executablePath: process.env.CHROME_PATH });
  for (const channel of ["chrome", "msedge"]) {
    try {
      return await chromium.launch({ channel });
    } catch {
      /* try the next installed browser */
    }
  }
  throw new Error("Neither Chrome nor Edge is installed. Set CHROME_PATH to a Chromium binary.");
}

// -- helpers for the product shots ---------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Calls exuvia's API as the workbench does, so the code it saves is labelled `ui` in History. */
async function api(method, route, body, headers = {}) {
  const response = await fetch(`${base}${route}`, {
    method,
    headers: { "content-type": "application/json", "X-Exuvia-Origin": "ui", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${route} answered ${response.status}: ${text}`);
  return text ? JSON.parse(text) : null;
}

async function until(what, check, seconds) {
  for (let i = 0; i < seconds * 2; i++) {
    if (await check()) return;
    await sleep(500);
  }
  throw new Error(`gave up waiting for ${what}`);
}

/** Runs every stale node of a project and waits until all of them are fresh. */
async function runStale(prefix = "", headers = {}) {
  await api("POST", `${prefix}/api/run-stale`, undefined, headers);
  await until("every node to be fresh", async () => {
    const project = await api("GET", `${prefix}/api/project`, undefined, headers);
    return project.nodes.every((n) => n.state === "fresh");
  }, 120);
}

async function workbench({ headers, height = 900 } = {}) {
  const context = await browser.newContext({
    viewport: { width: 1440, height },
    deviceScaleFactor: 2,
    colorScheme: "dark",
    extraHTTPHeaders: headers,
  });
  await context.addInitScript(() => localStorage.setItem("exuvia-theme", "dark"));
  return context.newPage();
}

async function shot(page, name, locator) {
  await page.waitForTimeout(600);
  const file = path.join(SHOTS, `${name}.png`);
  await (locator ?? page).screenshot({ path: file });
  console.log(`wrote ${name}.png`);
}

const tab = (page, name) => page.getByRole("tab", { name, exact: true });
const node = (page, name) => page.locator(".bnode", { hasText: name });

// -- app: the example project ----------------------------------------------------

const STATS = `"""Statistics the market nodes share."""


def annualized_vol(returns, window=20, days=252):
    """Rolling standard deviation of daily returns, scaled to a year."""
    return returns.rolling(window).std() * days**0.5
`;

/** rolling_vol as the example has it, changed to use the helper in lib/stats.py. */
function useHelper(source) {
  const changed = source
    .replace("def rolling_vol(returns):\n", "def rolling_vol(returns):\n    from lib.stats import annualized_vol\n\n")
    .replace(".transform(lambda s: s.rolling(20).std() * 252**0.5)", ".transform(annualized_vol)");
  if (!changed.includes("transform(annualized_vol)") || !changed.includes("from lib.stats")) {
    throw new Error("examples/markets/nodes/rolling_vol.py changed: update useHelper() in tools/capture.mjs.");
  }
  return changed;
}

async function app() {
  // What the History, Files and Packages shots show.
  await api("PUT", "/api/files/lib/stats.py", { source: STATS });
  const { source } = await api("GET", "/api/nodes/rolling_vol");
  if (!source.includes("annualized_vol")) await api("PUT", "/api/nodes/rolling_vol", { source: useHelper(source), kind: "python" });
  await api("POST", "/api/packages", { add: ["scipy"] });
  await until("scipy to install", async () => (await api("GET", "/api/packages")).status === "ready", 300);
  await runStale();

  const page = await workbench();

  // The workbench: graph on the left, a node's chart in the inspector.
  await page.goto(base);
  await node(page, "rolling_vol").click();
  await tab(page, "Chart").click();
  await page.locator(".insp-body canvas, .insp-body svg").first().waitFor();
  await shot(page, "workbench");

  // Downloads: the node's rows, and the formats they save as.
  await page.getByRole("tab", { name: /^Data/ }).click();
  await page.locator("button", { hasText: "Download" }).last().click();
  await page.getByText("Parquet", { exact: true }).waitFor();
  await shot(page, "downloads");
  await page.keyboard.press("Escape");

  // History: the edit to rolling_vol, against the version before it.
  await tab(page, "History").first().click();
  await page.getByText("vs previous").waitFor();
  await shot(page, "history");

  // Packages: the project's pyproject.toml, managed from the top bar.
  await page.goto(base);
  await page.getByRole("button", { name: "Packages" }).click();
  await page.getByText("scipy").first().waitFor();
  await shot(page, "packages");
  await page.keyboard.press("Escape");

  // Files: every node, helper and view file in one tree, with a helper open.
  await tab(page, "Files").click();
  await page.getByText("stats.py", { exact: true }).click();
  await page.getByText("annualized_vol").first().waitFor();
  await shot(page, "files");

  // Views: custom UI over node outputs, and the files it is made of.
  await page.goto(base);
  await page.getByRole("tab", { name: /Views/ }).click();
  await page.frameLocator("iframe").locator("body *").first().waitFor({ timeout: 30_000 });
  await page.waitForTimeout(1500);
  await shot(page, "views");
  await page.getByRole("tab", { name: /^Code/ }).click();
  await page.getByRole("button", { name: "App.tsx" }).click();
  await shot(page, "view-code");

  // The agent, after one real turn, with the node it made selected.
  if (flags.includes("--no-agent")) return;
  await page.goto(base);
  await tab(page, "Agent").click();
  await node(page, "prices").click();
  await page.locator(".agent textarea").fill("Add a drawdown node per ticker with a chart");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByRole("button", { name: "Stop", exact: true }).waitFor({ timeout: 15_000 });
  await page.getByRole("button", { name: "Stop", exact: true }).waitFor({ state: "detached", timeout: 300_000 });
  if (await node(page, "drawdown").count()) {
    await node(page, "drawdown").click();
    if (await tab(page, "Chart").count()) await tab(page, "Chart").click();
  }
  await page.waitForTimeout(1200);
  await shot(page, "agent");
  await shot(page, "agent-panel", page.locator(".agent"));
}

// -- hub: projects, people and sharing -------------------------------------------

/** The headers a sign-in proxy would send for this person (EXUVIA_AUTH=headers). */
const person = (name) => ({ "X-Forwarded-User": name, "X-Forwarded-Email": `${name}@example.com` });

async function createProject(id, name, owner) {
  const { projects } = await api("GET", "/api/projects", undefined, person(owner));
  if (!projects.some((p) => p.id === id)) await api("POST", "/api/projects", { id, name }, person(owner));
}

/** Copies examples/markets into a hub project through its API. */
async function fillFromExample(project, owner) {
  const example = path.join(REPO, "examples/markets");
  const prefix = `/p/${project}/api`;
  for (const file of (await fs.readdir(path.join(example, "nodes"))).sort()) {
    const source = await fs.readFile(path.join(example, "nodes", file), "utf8");
    const [name, ext] = file.split(".");
    await api("PUT", `${prefix}/nodes/${name}`, { source, kind: ext === "sql" ? "sql" : "python" }, person(owner));
  }
  for (const view of await fs.readdir(path.join(example, "views"))) {
    const dir = path.join(example, "views", view);
    await api("PUT", `${prefix}/views/${view}`, JSON.parse(await fs.readFile(path.join(dir, "view.json"), "utf8")), person(owner));
    for (const file of await fs.readdir(dir)) {
      if (file === "view.json") continue;
      await api("PUT", `${prefix}/views/${view}/files/${file}`, { source: await fs.readFile(path.join(dir, file), "utf8") }, person(owner));
    }
  }
}

/** Two small nodes for each project the hub shot lists besides Markets, so none reads "0 nodes". */
const SMALL_PROJECTS = {
  credit_risk: {
    "loans.py": `"""Open loans with their balance and days past due."""\nimport exuvia\n\n\n@exuvia.node()\ndef loans():\n    import pandas as pd\n\n    return pd.DataFrame({"loan": [1, 2, 3, 4], "balance": [12000, 5400, 830, 22000], "dpd": [0, 35, 0, 95]})\n`,
    "past_due.sql": `-- Balance more than 30 days past due.\nselect sum(balance) as past_due from loans where dpd > 30\n`,
  },
  churn: {
    "accounts.py": `"""Accounts with their plan and whether they cancelled."""\nimport exuvia\n\n\n@exuvia.node()\ndef accounts():\n    import pandas as pd\n\n    return pd.DataFrame({"plan": ["team", "team", "solo", "solo"], "cancelled": [False, True, False, False]})\n`,
    "churn_by_plan.sql": `-- Share of accounts that cancelled, per plan.\nselect plan, avg(cancelled::int) as churn from accounts group by plan\n`,
  },
};

async function hub() {
  const uid = {};
  for (const name of ["ada", "grace", "linus"]) uid[name] = (await api("GET", "/api/me", undefined, person(name))).user.uid;

  await createProject("markets", "Markets", "ada");
  await createProject("credit_risk", "Credit risk", "ada");
  await createProject("churn", "Customer churn", "grace");
  await fillFromExample("markets", "ada");
  for (const [project, files] of Object.entries(SMALL_PROJECTS)) {
    const owner = project === "churn" ? "grace" : "ada";
    for (const [file, source] of Object.entries(files)) {
      const [name, ext] = file.split(".");
      await api("PUT", `/p/${project}/api/nodes/${name}`, { source, kind: ext === "sql" ? "sql" : "python" }, person(owner));
    }
  }
  await runStale("/p/markets", person("ada"));

  // Markets is ada's: private, shared with grace and an invited colleague, with a link and linus asking in.
  const sharing = "/api/projects/markets/sharing";
  await api("PATCH", sharing, { general: "private" }, person("ada"));
  await api("PUT", `${sharing}/people`, { user: uid.grace, role: "editor" }, person("ada"));
  await api("PUT", `${sharing}/people`, { email: "margaret@example.com", role: "viewer" }, person("ada"));
  const { links } = await api("GET", sharing, undefined, person("ada"));
  if (!links.some((l) => l.name === "Weekly review")) {
    await api("POST", `${sharing}/links`, { name: "Weekly review", expires_in_days: 30 }, person("ada"));
  }
  const ask = { role: "editor", message: "I'd like to add a fixed-income node." };
  await api("POST", `${sharing}/requests`, ask, person("linus")).catch(() => {});

  // The hub's home page as grace sees it: her own project and two of ada's.
  const home = await workbench({ headers: person("grace"), height: 360 });
  await home.goto(base);
  await home.getByText("Customer churn").waitFor();
  await shot(home, "hub");

  // The Share dialog, from the workbench of the project's owner.
  const page = await workbench({ headers: person("ada") });
  await page.goto(`${base}/p/markets/`);
  await page.getByRole("button", { name: "Share" }).first().click();
  await page.getByText("Asking for access", { exact: false }).waitFor();
  await shot(page, "share");
}

// -- tutorial: the getting-started project -----------------------------------------

async function tutorial() {
  const markdown = await fs.readFile(path.join(SITE, "src/docs/getting-started.md"), "utf8");
  const files = [...markdown.matchAll(/^```\w+ file=(\S+)\n([\s\S]*?)^```$/gm)].map(([, file, source]) => ({ file, source }));
  for (const { file, source } of files) {
    const nodeFile = /^nodes\/(\w+)\.(py|sql)$/.exec(file);
    const viewFile = /^views\/(\w+)\/(.+)$/.exec(file);
    if (nodeFile) {
      await api("PUT", `/api/nodes/${nodeFile[1]}`, { source, kind: nodeFile[2] === "sql" ? "sql" : "python" });
    } else if (viewFile?.[2] === "view.json") {
      await api("PUT", `/api/views/${viewFile[1]}`, JSON.parse(source));
    } else if (viewFile) {
      await api("PUT", `/api/views/${viewFile[1]}/files/${viewFile[2]}`, { source });
    } else {
      throw new Error(`getting-started.md has a file=${file} block that tools/capture.mjs does not know where to put.`);
    }
  }
  await runStale();

  const page = await workbench();
  await page.goto(base);
  await node(page, "summary").click();
  await page.getByRole("tab", { name: /^Data/ }).click();
  await page.locator(".insp-body table").first().waitFor();
  await shot(page, "tutorial-graph");

  await page.getByRole("tab", { name: /Views/ }).click();
  await page.frameLocator("iframe").locator("li").first().waitFor({ timeout: 30_000 });
  await page.waitForTimeout(1000);
  await shot(page, "tutorial-view");
}

// -- cards: the social cards and lockups -------------------------------------------

/**
 * Photographs every social card, and the lockups, from the site's /card/
 * pages. A PNG whose card is gone is deleted, so public/og/ holds the cards
 * of src/lib/cards.ts and nothing else.
 */
async function cards() {
  const og = path.join(SITE, "public/og");
  const brand = path.join(SITE, "public/brand");
  await fs.mkdir(og, { recursive: true });
  await fs.mkdir(brand, { recursive: true });
  const open = async (page, name = "") => {
    const response = await page.goto(`${base}/card/${name && `${name}/`}`);
    if (!response?.ok()) throw new Error(`${base}/card/${name} answered ${response?.status()}: is this the site under astro dev?`);
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(200);
  };

  const context = await browser.newContext({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  await open(page);
  const names = await page.locator("a[data-card]").evaluateAll((links) => links.map((a) => a.dataset.card));
  const lockups = await page.locator("a[data-lockup]").evaluateAll((links) => links.map((a) => a.dataset.lockup));
  for (const name of names) {
    await open(page, name);
    // A card is flat colour and a few gradients: 256 colours hold it at a fraction of the size.
    const shot = await page.screenshot();
    await sharp(shot).png({ palette: true, colours: 256, effort: 10 }).toFile(path.join(og, `${name}.png`));
  }
  for (const file of await fs.readdir(og)) {
    if (!names.includes(path.basename(file, ".png"))) await fs.rm(path.join(og, file));
  }
  console.log(`wrote ${names.length} cards to public/og/`);

  // The lockups: twice the size and on nothing, so they sit on any ground.
  const large = await (await browser.newContext({ deviceScaleFactor: 2 })).newPage();
  for (const name of lockups) {
    await open(large, name);
    await large.locator("[data-shot]").screenshot({ path: path.join(brand, `${name}.png`), omitBackground: true });
  }
  console.log(`wrote ${lockups.length} lockups to public/brand/`);
}

// Last, so every constant above is defined before a mode runs.
const browser = await launch();
try {
  await { app, hub, tutorial, cards }[mode]();
} finally {
  await browser.close();
}
