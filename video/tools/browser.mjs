/**
 * The browser the video tools drive: the Chrome or Edge already on the
 * machine, or the Chromium that CHROME_PATH names. Shared by render.mjs (the
 * overview) and help/tools/record.mjs (the help videos).
 */
import { chromium } from "playwright-core";

export async function launch(options = {}) {
  if (process.env.CHROME_PATH) return chromium.launch({ ...options, executablePath: process.env.CHROME_PATH });
  for (const channel of ["chrome", "msedge"]) {
    try {
      return await chromium.launch({ ...options, channel });
    } catch {
      /* try the next installed browser */
    }
  }
  throw new Error("Neither Chrome nor Edge is installed. Set CHROME_PATH to a Chromium binary.");
}
