/**
 * The recording studio for the help videos: a real browser driving the real
 * app, a camera on one page at a time, a pointer that moves like a hand, and
 * a clock that keeps the picture on the narration.
 *
 * A video (help/<name>/video.mjs) is a list of beats, one per segment of its
 * script.json. `beat(id, fn)` starts the segment's line, runs the actions in
 * `fn` while it is said, and then waits until the line and its pause are
 * over. When the actions take longer (a run, the agent thinking), the
 * picture keeps going and the next line waits for it. The time each beat
 * started is written to timeline.json, and tools/audio.py lays each voice
 * file at that moment, so the sound follows the picture wherever it went.
 *
 * `fast(factor, fn)` plays a stretch of waiting faster (the agent working,
 * say), with a badge that says so: the clock the beats keep is the video's,
 * not the wall's.
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HELP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CURSOR = fs.readFileSync(path.join(HELP, "tools", "cursor.js"), "utf8");
export const VIEWPORT = { width: 1280, height: 720 };
export const SCALE = 1.5; // the default: 1280x720 laid out, 1920x1080 recorded, so the app's text reads on a phone. 3 records 4K.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);

export class Studio {
  constructor({ browser, script, timings, frames, scale = SCALE, slow = 1 }) {
    this.scale = scale;
    // Slow capture: everything a person does (and the page's animations) runs `slow` times slower
    // while filming, and the video clock plays it back at speed. 4K frames come too slowly to film live.
    this.slow = slow;
    this.browser = browser;
    this.script = script;
    this.segments = new Map(script.segments.map((s) => [s.id, s]));
    this.timings = timings;
    this.framesDir = frames;
    this.frames = []; // [video seconds, file]
    this.beats = []; // {id, at, duration}
    this.children = [];
    this.pointer = new WeakMap();
    // The video's clock: wall time, slowed to 1/factor inside fast().
    this.factor = slow;
    this.mark = { wall: 0, video: 0 };
    this.breaks = []; // [wall ms, video s, factor]: how to place a frame stamped with wall time
  }

  async open() {
    // Dark mode: the app follows the system setting, and the videos keep to dark.
    this.context = await this.browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: this.scale, colorScheme: "dark" });
    await this.context.addInitScript(CURSOR);
  }

  /** Seconds of video so far. */
  now() {
    return this.mark.video + (Date.now() - this.mark.wall) / 1000 / this.factor;
  }

  start() {
    this.mark = { wall: Date.now(), video: 0 };
    this.breaks = [[this.mark.wall, 0, this.factor]];
  }

  setFactor(factor) {
    this.mark = { wall: Date.now(), video: this.now() };
    this.factor = factor;
    this.breaks.push([this.mark.wall, this.mark.video, factor]);
    return this.pace(this.page);
  }

  /** Waits `ms` of video time: a person's pause, slowed with everything else while filming slowly. */
  pause(ms) {
    return sleep(ms * this.factor);
  }

  /** Slows the page's CSS animations and the pointer's own to the filming speed. */
  async pace(page) {
    if (!page) return;
    await page.evaluate((k) => (window.__helpSlow = k), this.factor).catch(() => {});
    if (page === this.page && this.cdp) {
      await this.cdp.send("Animation.enable").catch(() => {});
      await this.cdp.send("Animation.setPlaybackRate", { playbackRate: 1 / this.factor }).catch(() => {});
    }
  }

  videoTime(wallMs) {
    let at = this.breaks[0];
    for (const b of this.breaks) if (b[0] <= wallMs) at = b;
    return at[1] + (wallMs - at[0]) / 1000 / at[2];
  }

  duration(id) {
    const segment = this.segments.get(id);
    if (!segment) throw new Error(`no segment "${id}" in script.json`);
    return this.timings?.[id]?.duration ?? segment.est ?? segment.text.length / 15.5;
  }

  /** A narrated step: the line starts now, `fn` acts while it is said, and the beat ends when both are done. */
  async beat(id, fn = async () => {}) {
    const segment = this.segments.get(id);
    const expected = this.script.segments[this.beats.length]?.id;
    if (id !== expected) throw new Error(`beat "${id}" is out of order: script.json has "${expected}" next`);
    const at = this.now();
    const duration = this.duration(id);
    this.beats.push({ id, at, duration });
    this.current = { at, duration, text: segment.text };
    console.log(`${at.toFixed(1).padStart(6)}s  ${id}`);
    await fn();
    const end = at + duration + (segment.pad ?? 0.6);
    while (this.now() < end) await sleep(Math.min(50, (end - this.now()) * 1000 * this.factor));
  }

  /** Wait until the current line reaches `phrase` (estimated from where it sits in the text). */
  async said(phrase) {
    const { at, duration, text } = this.current;
    const index = text.indexOf(phrase);
    if (index < 0) throw new Error(`"${phrase}" is not in: ${text}`);
    const when = at + (duration * index) / text.length;
    while (this.now() < when) await sleep(30);
  }

  async wait(seconds) {
    await sleep(seconds * 1000 * this.factor);
  }

  /** Plays the waiting inside `fn` `factor` times faster, with a badge on the page that says so. */
  async fast(factor, fn) {
    const page = this.page;
    await page.evaluate((f) => {
      const badge = document.createElement("div");
      badge.id = "__helpFast";
      badge.textContent = `${f}× speed`;
      badge.style.cssText =
        "position:fixed;right:16px;bottom:16px;z-index:2147483646;padding:4px 10px;border-radius:6px;" +
        "font:600 13px system-ui,sans-serif;color:#fff;background:rgba(15,19,28,.82);pointer-events:none;";
      document.documentElement.appendChild(badge);
    }, factor);
    await this.setFactor(this.slow * factor);
    try {
      return await fn();
    } finally {
      await this.setFactor(this.slow);
      await page.evaluate(() => document.getElementById("__helpFast")?.remove());
    }
  }

  /** A new page in the recording's browser (not on camera until show()). */
  async newPage(url) {
    const page = await this.context.newPage();
    page.on("pageerror", (error) => console.error("page error:", error.message));
    if (url) await page.goto(url);
    return page;
  }

  /** A title card (card.html): the help video's name and its docs page. */
  async card(params) {
    const url = new URL(`file://${path.join(HELP, "tools", "card.html")}`);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
    const page = await this.newPage(url.href);
    await page.evaluate(() => document.fonts.ready);
    return page;
  }

  /** Puts the camera on `page`. */
  async show(page) {
    if (this.cdp) {
      await this.cdp.send("Page.stopScreencast").catch(() => {});
      await this.cdp.detach().catch(() => {});
    }
    this.page = page;
    await page.bringToFront();
    this.cdp = await this.context.newCDPSession(page);
    this.cdp.on("Page.screencastFrame", ({ data, metadata, sessionId }) => {
      this.cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
      const file = path.join(this.framesDir, `${String(this.frames.length).padStart(6, "0")}.jpg`);
      this.frames.push([this.videoTime(metadata.timestamp * 1000), file]);
      fs.writeFile(file, Buffer.from(data, "base64"), () => {});
    });
    await this.cdp.send("Page.startScreencast", {
      format: "jpeg",
      quality: 92,
      maxWidth: VIEWPORT.width * this.scale,
      maxHeight: VIEWPORT.height * this.scale,
    });
    await this.pace(page);
  }

  // ---- the hand on the mouse ---------------------------------------------

  async move(x, y, { duration } = {}) {
    const page = this.page;
    const from = this.pointer.get(page) ?? { x: VIEWPORT.width * 0.6, y: VIEWPORT.height * 0.7 };
    const distance = Math.hypot(x - from.x, y - from.y);
    const ms = duration ?? Math.min(900, 250 + distance * 0.9);
    const steps = Math.max(2, Math.round(ms / 16));
    await page.evaluate(() => window.__helpCursorShow?.(true));
    for (let i = 1; i <= steps; i++) {
      const t = ease(i / steps);
      await page.mouse.move(from.x + (x - from.x) * t, from.y + (y - from.y) * t);
      await this.pause(ms / steps);
    }
    this.pointer.set(page, { x, y });
  }

  async point(locator, { dx = 0.5, dy = 0.5 } = {}) {
    await locator.waitFor({ state: "visible", timeout: 30000 });
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    await this.move(box.x + box.width * dx, box.y + box.height * dy);
    return box;
  }

  async click(locator, options = {}) {
    await this.point(locator, options);
    await this.pause(140);
    await this.page.mouse.down();
    await this.pause(70);
    await this.page.mouse.up();
    await this.pause(options.after ?? 250);
  }

  async hover(locator, seconds = 0.6, options) {
    await this.point(locator, options);
    await this.wait(seconds);
  }

  /** Types like a person: a key at a time, at a speed that varies a little. */
  async type(text, { cps = 16 } = {}) {
    await this.page.evaluate(() => window.__helpCursorShow?.(false));
    for (const ch of text) {
      if (ch === "\n") await this.page.keyboard.press("Enter");
      else await this.page.keyboard.type(ch);
      await this.pause((1000 / cps) * (0.6 + Math.random() * 0.8));
    }
  }

  async press(key, pause = 120) {
    await this.page.keyboard.press(key);
    await this.pause(pause);
  }

  /** A keyboard shortcut, with its keycaps shown on screen. */
  async shortcut(key, pause = 600) {
    await this.page.evaluate((k) => window.__helpKeys?.(k.replace("Control", "Ctrl")), key);
    await this.pause(250);
    await this.page.keyboard.press(key);
    await this.pause(pause);
  }

  // ---- a code editor (CodeMirror) on the page ----------------------------

  /** Where a character of a CodeMirror document is on screen: line and column count from 1 and 0; col "end" is the line's end. */
  async codeAt(line, col, editor = this.page.locator(".cm-editor").first()) {
    const handle = await editor.elementHandle();
    return this.page.evaluate(([root, line, col]) => {
      const el = root.querySelectorAll(".cm-line")[line - 1];
      if (!el) throw new Error(`no line ${line}`);
      const text = el.textContent;
      const at = col === "end" ? text.length : col === "indent" ? text.length - text.trimStart().length : col;
      // Walk the line's text nodes to the one that holds the column.
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let left = at, node;
      while ((node = walker.nextNode())) {
        if (left <= node.length) break;
        left -= node.length;
      }
      const range = document.createRange();
      if (!node) {
        const box = el.getBoundingClientRect();
        return { x: box.left + 2, y: box.top + box.height / 2 };
      }
      range.setStart(node, left);
      range.setEnd(node, left);
      const rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
      return { x: rect.left, y: rect.top + rect.height / 2 };
    }, [handle, line, col]);
  }

  /** Selects code with the mouse, from (line, col) to (line, col), as a person drags across it. */
  async selectCode(from, to, editor) {
    const a = await this.codeAt(from[0], from[1], editor);
    const b = await this.codeAt(to[0], to[1], editor);
    await this.move(a.x, a.y);
    await this.pause(120);
    await this.page.mouse.down();
    await this.move(b.x, b.y, { duration: 450 });
    await this.page.mouse.up();
    await this.pause(250);
  }

  /** Clicks in code at (line, col). */
  async clickCode(line, col, editor) {
    const a = await this.codeAt(line, col, editor);
    await this.move(a.x, a.y);
    await this.pause(120);
    await this.page.mouse.down();
    await this.pause(60);
    await this.page.mouse.up();
    await this.pause(200);
  }

  // ---- terminals ----------------------------------------------------------

  /** A terminal window: bash in a pseudo-terminal (terminal.py), drawn in its own page. */
  async terminal({ port, home, cwd, title }) {
    const child = spawn("python3", [path.join(HELP, "tools", "terminal.py"), "--port", String(port), "--home", home, ...(cwd ? ["--cwd", cwd] : []), "--title", title ?? "Terminal"], {
      stdio: ["ignore", "pipe", "inherit"],
    });
    this.children.push(child);
    await new Promise((resolve, reject) => {
      child.stdout.once("data", resolve);
      child.once("exit", (code) => reject(new Error(`terminal.py exited (${code})`)));
    });
    const page = await this.newPage(`http://127.0.0.1:${port}/`);
    await page.waitForFunction("window.READY === true");
    return new Terminal(this, page);
  }

  close() {
    for (const child of this.children) child.kill("SIGTERM");
  }
}

export class Terminal {
  constructor(studio, page) {
    this.studio = studio;
    this.page = page;
  }

  text() {
    return this.page.evaluate(() => window.screenText());
  }

  /** Waits until the screen's text matches `pattern` (past what was there before, when `since` is given). */
  async waitFor(pattern, { since = 0, timeout = 120000 } = {}) {
    const end = Date.now() + timeout;
    for (;;) {
      const text = (await this.text()).slice(since);
      if (pattern.test(text)) return text;
      if (Date.now() > end) throw new Error(`terminal: timed out waiting for ${pattern}; screen ends:\n${text.slice(-600)}`);
      await sleep(100);
    }
  }

  /** Types a command and presses Enter; unless `wait` is false, waits for the prompt to come back (or for `until`). */
  async run(command, { wait = true, until, cps = 17, timeout } = {}) {
    if (this.studio.page !== this.page) throw new Error("terminal.run: this terminal is not on camera");
    const since = (await this.text()).length;
    await this.studio.type(command, { cps });
    await this.studio.pause(250);
    await this.page.keyboard.press("Enter");
    if (until) return this.waitFor(until, { since, timeout });
    // Done when the last line is a bare prompt again: the folder, a dollar, nothing typed after it.
    if (wait) return this.waitFor(/(^|\n)[~/]\S* \$ ?$/, { timeout });
  }

  /** Pastes text into the shell at once, as from the clipboard. */
  async paste(text) {
    await this.page.evaluate((t) => window.term.paste(t), text);
  }

  /** Ctrl+C (shown on screen), then waits for the shell's prompt. */
  async interrupt() {
    // The prompt may come back on the line the terminal echoed ^C on.
    const prompt = /(^|\n)(\^C)?[~/]\S* \$ ?$/;
    // A second Ctrl+C after a few seconds, as a person would press it when the first does
    // nothing: `exuvia serve` waits for open browser tabs on the first (rlancer/exuvia#246).
    for (let tries = 0; ; tries++) {
      await this.studio.shortcut("Control+c", 200);
      try {
        return await this.waitFor(prompt, { timeout: tries < 2 ? 3000 : 30000 });
      } catch (error) {
        if (tries >= 2) throw error;
      }
    }
  }
}
