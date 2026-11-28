/**
 * Records a help video: runs help/<name>/video.mjs against the real app,
 * films it, and puts the narration under it.
 *
 *   node help/tools/record.mjs getting-started            -> out/help/getting-started.mp4 (+ .srt)
 *   node help/tools/record.mjs getting-started --draft    -> out/help/getting-started-draft.mp4, silent,
 *                                                            timed from each line's estimate (no voice needed)
 *   node help/tools/record.mjs getting-started --stills   -> also out/help/getting-started-stills/<beat>.jpg
 *
 * Without --draft it times the beats from help/<name>/audio/timings.json, so
 * run `python tools/audio.py --script help/<name>/script.json voice` first.
 * Afterwards it runs `audio.py ... mix` with the beat times it recorded, and
 * muxes that mix into the mp4.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { launch } from "../../tools/browser.mjs";
import { SCALE, Studio, VIEWPORT } from "./studio.mjs";

const VIDEO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const args = process.argv.slice(2);
const name = args.find((a) => !a.startsWith("--"));
if (!name || !fs.existsSync(path.join(VIDEO, "help", name, "video.mjs"))) {
  console.error("usage: node help/tools/record.mjs <name> [--draft] [--stills]   (a folder in help/ with video.mjs)");
  process.exit(2);
}
const draft = args.includes("--draft");
const dir = path.join(VIDEO, "help", name);
const scriptPath = path.join(dir, "script.json");
const script = JSON.parse(fs.readFileSync(scriptPath, "utf8"));
const timingsPath = path.join(dir, "audio", "timings.json");
const timings = draft ? null : fs.existsSync(timingsPath) ? JSON.parse(fs.readFileSync(timingsPath, "utf8")) : null;
if (!draft) {
  const missing = script.segments.filter((s) => !timings?.[s.id]).map((s) => s.id);
  if (missing.length) {
    console.error(`no voice yet for: ${missing.join(", ")}\nrun: python tools/audio.py --script help/${name}/script.json voice   (or record with --draft)`);
    process.exit(1);
  }
}

const out = path.join(VIDEO, "out", "help");
const work = path.join(out, name);
const frames = path.join(work, "frames");
// The video's own files (its home folder, its projects), at a short path: commands print it on camera.
const stage = path.join(os.tmpdir(), `exuvia-${name}`);
fs.rmSync(work, { recursive: true, force: true });
fs.rmSync(stage, { recursive: true, force: true });
fs.mkdirSync(frames, { recursive: true });
fs.mkdirSync(stage, { recursive: true });

const { default: film } = await import(path.join(dir, "video.mjs"));
const browser = await launch();
const studio = new Studio({ browser, script, timings, frames });
await studio.open();
let failed = null;
try {
  await film(studio, { stage, draft });
  await studio.wait(script.tail ?? 1.5);
} catch (error) {
  failed = error;
} finally {
  studio.close();
}
const end = studio.now();
await new Promise((resolve) => setTimeout(resolve, 500)); // the last frames to reach the disk
await browser.close();
if (failed) {
  console.error(failed);
  console.error(`recording failed at ${end.toFixed(1)}s; the frames so far are in ${frames}`);
}

// The frames as the camera saw them, each held until the next: a concat list, made constant-rate by ffmpeg.
const list = studio.frames.filter(([t]) => t >= 0).sort((a, b) => a[0] - b[0]);
if (!list.length) process.exit(1);
const lines = ["ffconcat version 1.0"];
for (let i = 0; i < list.length; i++) {
  const next = i + 1 < list.length ? list[i + 1][0] : end;
  // The first frame also covers the moment before it arrived.
  const from = i === 0 ? 0 : list[i][0];
  lines.push(`file '${list[i][1]}'`, `duration ${Math.max(0.001, next - from).toFixed(4)}`);
}
lines.push(`file '${list.at(-1)[1]}'`);
fs.writeFileSync(path.join(work, "frames.txt"), lines.join("\n") + "\n");
fs.writeFileSync(path.join(work, "timeline.json"), JSON.stringify({ duration: end, beats: studio.beats }, null, 1) + "\n");

const silent = path.join(work, "silent.mp4");
const size = `${VIEWPORT.width * SCALE}x${VIEWPORT.height * SCALE}`;
execFileSync("ffmpeg", [
  "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", path.join(work, "frames.txt"),
  "-vf", `scale=${size}:flags=lanczos,fps=30,format=yuv420p`, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
  "-movflags", "+faststart", silent,
], { stdio: "inherit" });

if (draft || failed) {
  const target = path.join(out, `${name}-draft.mp4`);
  fs.copyFileSync(silent, target);
  console.log(`${target}: ${end.toFixed(1)} s, silent`);
} else {
  execFileSync("python3", [path.join(VIDEO, "tools", "audio.py"), "--script", scriptPath, "--timeline", path.join(work, "timeline.json"), "mix"], { stdio: "inherit" });
  const target = path.join(out, `${name}.mp4`);
  execFileSync("ffmpeg", [
    "-v", "error", "-y", "-i", silent, "-i", path.join(dir, "audio", "mix.wav"),
    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", target,
  ], { stdio: "inherit" });
  console.log(`${target}: ${end.toFixed(1)} s`);
}

if (args.includes("--stills")) {
  const stills = path.join(out, `${name}-stills`);
  fs.rmSync(stills, { recursive: true, force: true });
  fs.mkdirSync(stills, { recursive: true });
  for (const [i, beat] of studio.beats.entries()) {
    const t = Math.min(end - 0.1, beat.at + beat.duration * 0.85);
    execFileSync("ffmpeg", ["-v", "error", "-y", "-ss", t.toFixed(2), "-i", silent, "-frames:v", "1", "-q:v", "3", path.join(stills, `${String(i + 1).padStart(2, "0")}-${beat.id}.jpg`)]);
  }
  console.log(`stills: ${stills}`);
}
process.exit(failed ? 1 : 0);
