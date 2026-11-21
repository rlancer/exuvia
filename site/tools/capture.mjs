/**
 * Captures the site's product screenshots from a running exuvia, and the
 * social cards from the site's own /card/ pages. Uses the Chrome (or Edge)
 * already on the machine, so there is no browser download.
 *
 *   # in exuvia/: serve a scratch copy of the example, with an agent key set
 *   cp -r examples/markets /tmp/markets && uv run exuvia serve /tmp/markets --port 7431
 *   # in exuvia/site/
 *   node tools/capture.mjs app http://127.0.0.1:7431    -> src/assets/shots/{workbench,views,agent,agent-panel}.png
 *   # with the site itself running (npm run dev): the card pages exist only under `astro dev`
 *   node tools/capture.mjs cards http://localhost:4380  -> public/og/<card>.png, one for every card in
 *                                                          src/lib/cards.ts, and public/brand/<lockup>.png
 *
 * The agent shot sends one real prompt, so it changes the project it runs
 * against (the agent writes a node) and spends a few cents of model credit.
 * Pass --no-agent to skip it.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import sharp from "sharp";

const SITE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const [mode, address, ...flags] = process.argv.slice(2);
if (!["app", "cards"].includes(mode) || !address) {
  console.error("usage: node tools/capture.mjs app <exuvia url> [--no-agent] | cards <site url>");
  process.exit(2);
}
const base = address.replace(/\/+$/, "");

async function launch() {
  for (const channel of ["chrome", "msedge"]) {
    try {
      return await chromium.launch({ channel });
    } catch {
      /* try the next installed browser */
    }
  }
  throw new Error("Neither Chrome nor Edge is installed.");
}

const browser = await launch();
try {
  if (mode === "app") await app();
  else await cards();
} finally {
  await browser.close();
}

async function app() {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    colorScheme: "dark",
  });
  await context.addInitScript(() => localStorage.setItem("exuvia-theme", "dark"));
  const page = await context.newPage();
  const shot = (name) => page.screenshot({ path: path.join(SITE, "src/assets/shots", `${name}.png`) });
  const allFresh = async () => {
    const project = await (await fetch(`${base}/api/project`)).json();
    return project.nodes.every((n) => n.state === "fresh");
  };

  // Bring every node up to date before looking at anything.
  await fetch(`${base}/api/run-stale`, { method: "POST" });
  for (let i = 0; i < 120 && !(await allFresh()); i++) await new Promise((r) => setTimeout(r, 500));

  // 1. The workbench: graph on the left, a node's chart on the right.
  await page.goto(base);
  await page.locator(".bnode", { hasText: "rolling_vol" }).click();
  await page.getByRole("tab", { name: "Chart", exact: true }).click();
  await page.locator(".insp-body canvas, .insp-body svg").first().waitFor();
  await page.waitForTimeout(800);
  await shot("workbench");
  console.log("wrote workbench.png");

  // 2. Views: custom UI over node outputs.
  await page.getByRole("tab", { name: /Views/ }).click();
  await page.frameLocator("iframe").locator("body *").first().waitFor({ timeout: 30_000 });
  await page.waitForTimeout(1500);
  await shot("views");
  console.log("wrote views.png");

  // 3. The agent, after one real turn.
  if (flags.includes("--no-agent")) return;
  await page.getByRole("tab", { name: /Graph/ }).click();
  await page.locator(".bnode", { hasText: "prices" }).click();
  await page.locator(".agent textarea").fill("Add a drawdown node per ticker with a chart");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByRole("button", { name: "Stop", exact: true }).waitFor({ timeout: 15_000 });
  await page.getByRole("button", { name: "Stop", exact: true }).waitFor({ state: "detached", timeout: 300_000 });
  await page.waitForTimeout(1200);
  await shot("agent");
  await page.locator(".agent").screenshot({ path: path.join(SITE, "src/assets/shots/agent-panel.png") });
  console.log("wrote agent.png, agent-panel.png");
}

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
