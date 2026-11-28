/**
 * Renders the film: opens src/index.html in the Chrome (or Edge) already on
 * the machine, calls its `seek(t)` once per frame, and pipes the screenshots
 * into ffmpeg. Several pages render at once, each its own stretch of the film.
 *
 *   node tools/render.mjs                    -> out/exuvia-overview.mp4 (with out/../audio/mix.wav if it exists)
 *   node tools/render.mjs --fps 60 --workers 6
 *   node tools/render.mjs --stills 2,8.5,31  -> out/stills/<t>.png, to look at single frames (--dir names another folder in out/)
 *   node tools/render.mjs --serve            -> serves the page to open in a browser (it plays itself; ?t=12 holds a frame)
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { launch } from "./browser.mjs";

const VIDEO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO = path.resolve(VIDEO, "..");
const OUT = path.join(VIDEO, "out");
const args = process.argv.slice(2);
const flag = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
const fps = +flag("fps", 30);
const workers = +flag("workers", 4);

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".woff2": "font/woff2", ".mp3": "audio/mpeg", ".wav": "audio/wav" };
const server = http.createServer((request, response) => {
  const file = path.join(REPO, decodeURIComponent(new URL(request.url, "http://x").pathname));
  if (!file.startsWith(REPO) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return response.writeHead(404).end();
  response.writeHead(200, { "content-type": TYPES[path.extname(file)] ?? "application/octet-stream", "cache-control": "no-store" });
  fs.createReadStream(file).pipe(response);
});
await new Promise((resolve) => server.listen(+flag("port", 0), "127.0.0.1", resolve));
const page_url = `http://127.0.0.1:${server.address().port}/video/src/index.html`;

if (args.includes("--serve")) {
  console.log(page_url);
  await new Promise(() => {});
}

const browser = await launch();
async function open() {
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.on("pageerror", (error) => console.error("page error:", error.message));
  await page.goto(`${page_url}?render`);
  await page.waitForFunction("window.READY === true", null, { timeout: 60000 });
  const cdp = await context.newCDPSession(page);
  const shot = async (t) => {
    await page.evaluate((at) => window.seek(at), t);
    const { data } = await cdp.send("Page.captureScreenshot", { format: "png", optimizeForSpeed: true });
    return Buffer.from(data, "base64");
  };
  return { page, shot };
}

const run = (command, argv, options = {}) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, argv, { stdio: ["ignore", "inherit", "inherit"], ...options });
    child.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`${command} exited with ${code}`))));
  });

fs.mkdirSync(OUT, { recursive: true });

if (args.includes("--stills")) {
  const dir = path.join(OUT, flag("dir", "stills"));
  fs.mkdirSync(dir, { recursive: true });
  const { page, shot } = await open();
  const film = await page.evaluate(() => window.FILM);
  const wanted = flag("stills") === "scenes"
    ? Object.values(film.scenes).flatMap((scene) => [0.3, 0.75].map((k) => +(scene.start + scene.dur * k).toFixed(2)))
    : flag("stills").split(",").map(Number);
  for (const t of wanted) fs.writeFileSync(path.join(dir, `${t.toFixed(2).padStart(6, "0")}.png`), await shot(t));
  console.log(JSON.stringify(film.scenes, null, 1));
  await browser.close();
  server.close();
  process.exit(0);
}

const first = await open();
const film = await first.page.evaluate(() => window.FILM);
const frames = Math.ceil(film.total * fps);
console.log(`${film.total.toFixed(2)} s, ${frames} frames at ${fps} fps, ${workers} workers`);

const started = Date.now();
let done = 0;
const parts = await Promise.all(
  Array.from({ length: workers }, async (_, w) => {
    const { shot } = w === 0 ? first : await open();
    const [from, to] = [Math.floor((frames * w) / workers), Math.floor((frames * (w + 1)) / workers)];
    const part = path.join(OUT, `part-${w}.mp4`);
    const ffmpeg = spawn(
      "ffmpeg",
      ["-v", "error", "-y", "-f", "image2pipe", "-framerate", String(fps), "-c:v", "png", "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-vf", "scale=out_color_matrix=bt709:out_range=tv", part],
      { stdio: ["pipe", "inherit", "inherit"] },
    );
    const closed = new Promise((resolve, reject) => ffmpeg.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited with ${code}`)))));
    for (let f = from; f < to; f++) {
      const png = await shot(f / fps);
      if (!ffmpeg.stdin.write(png)) await new Promise((resolve) => ffmpeg.stdin.once("drain", resolve));
      if (++done % 150 === 0) console.log(`${done}/${frames} frames, ${((Date.now() - started) / 1000).toFixed(0)} s`);
    }
    ffmpeg.stdin.end();
    await closed;
    return part;
  }),
);
await browser.close();
server.close();

const list = path.join(OUT, "parts.txt");
fs.writeFileSync(list, parts.map((part) => `file '${part.replace(/\\/g, "/")}'`).join("\n"));
const silent = path.join(OUT, "silent.mp4");
await run("ffmpeg", ["-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", list, "-c", "copy", silent]);
for (const part of [...parts, list]) fs.rmSync(part);

const mix = path.join(VIDEO, "audio", "mix.wav");
const final = path.join(OUT, "exuvia-overview.mp4");
if (fs.existsSync(mix)) {
  await run("ffmpeg", ["-v", "error", "-y", "-i", silent, "-i", mix, "-c:v", "copy", "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-shortest", "-movflags", "+faststart", final]);
  fs.rmSync(silent);
  console.log(final);
} else {
  console.log(`${silent} (no audio/mix.wav yet: run tools/audio.py, then render again)`);
}
