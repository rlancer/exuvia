import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { MARK } from "./mark";

const read = (path: string) => readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf8");

/** The value of a custom property in the first `selector { … }` block of a stylesheet. */
function token(css: string, selector: string, name: string): string | undefined {
  const start = css.indexOf(`${selector} {`);
  const block = css.slice(start, css.indexOf("}", start));
  return block.match(new RegExp(`${name}:\\s*([^;]+);`))?.[1].trim();
}

describe("one brand, in the app and on the website", () => {
  const site = read("../../site/src/lib/brand.ts");
  const siteCss = read("../../site/src/styles/global.css");
  const appCss = read("./styles.css");

  it("draws the same mark as the website", () => {
    const block = site.slice(site.indexOf("export const MARK"), site.indexOf("};", site.indexOf("export const MARK")));
    const facets = [...block.matchAll(/\["(M[^"]+)",\s*"(#[0-9a-f]{6})"\]/g)].map((m) => [m[1], m[2]]);
    expect(facets).toEqual(MARK.facets.map((f) => [...f]));
    expect(block.match(/outline:\s*"([^"]+)"/)?.[1]).toBe(MARK.outline);
  });

  it("has the website's colours as its dark theme", () => {
    for (const name of ["--bg", "--panel", "--panel-2", "--line", "--line-strong", "--text", "--accent", "--accent-hover", "--accent-ink", "--fresh", "--stale", "--running"]) {
      expect(token(appCss, ':root[data-theme="dark"]', name), name).toBe(token(siteCss, ":root", name));
    }
  });

  it("gives views the accent the app has, in both themes", () => {
    const host = read("./views/host.ts");
    const light = token(appCss, ":root", "--accent")!;
    const dark = token(appCss, ':root[data-theme="dark"]', "--accent")!;
    expect(host).toContain(`--accent:${light};`);
    expect(host).toContain(`--accent:${dark};`);
  });
});
