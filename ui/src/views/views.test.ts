import { describe, expect, it, vi } from "vitest";
import { ViewError, compile, createLoader, resolveImport } from "./loader";
import { CHANNEL, isToFrame, isToHost } from "./protocol";
import { frameDocument, refusal } from "./host";

const files = {
  "App.tsx": 'import { label } from "./lib/format";\nexport default function App() { return <b>{label(1)}</b>; }\n',
  "lib/format.ts": "export const label = (n: number): string => `#${n}`;\n",
  "lib/index.ts": 'export * from "./format";\n',
  "style.css": "b { color: red; }",
  "data.json": '{"a": 1}',
  "icon.svg": "<svg/>",
};

describe("resolveImport", () => {
  it("finds files with or without an extension, and index files", () => {
    expect(resolveImport("App.tsx", "./lib/format", files)).toBe("lib/format.ts");
    expect(resolveImport("App.tsx", "./lib/format.ts", files)).toBe("lib/format.ts");
    expect(resolveImport("App.tsx", "./lib", files)).toBe("lib/index.ts");
    expect(resolveImport("lib/format.ts", "../style.css", files)).toBe("style.css");
    expect(resolveImport("lib/format.ts", "./../data.json", files)).toBe("data.json");
  });

  it("never leaves the view folder", () => {
    expect(resolveImport("App.tsx", "../App.tsx", files)).toBeNull();
    expect(resolveImport("lib/format.ts", "../../App.tsx", files)).toBeNull();
    expect(resolveImport("App.tsx", "/App.tsx", files)).toBeNull();
    expect(resolveImport("App.tsx", "./nope", files)).toBeNull();
    expect(resolveImport("App.tsx", "./constructor", files)).toBeNull(); // not an inherited key
  });
});

describe("compile", () => {
  it("turns TSX into CommonJS with the automatic JSX runtime", () => {
    const out = compile("App.tsx", files["App.tsx"]);
    expect(out).toContain('require("react/jsx-runtime")');
    expect(out).toContain("require('./lib/format')");
    expect(out).toContain("exports.default = App");
  });

  it("reports syntax errors with the file", () => {
    expect(() => compile("Broken.tsx", "export default () => <div>")).toThrow(ViewError);
    try {
      compile("Broken.tsx", "export default () => <div>");
    } catch (e) {
      expect((e as ViewError).file).toBe("Broken.tsx");
    }
  });
});

describe("createLoader", () => {
  const jsx = { jsx: (t: unknown, p: unknown) => ({ t, p }), jsxs: () => null, Fragment: "frag", __esModule: true };
  const builtins = { "react/jsx-runtime": jsx, react: { __esModule: true, default: {} } };

  it("loads a module graph with css, json and svg imports", () => {
    const css = vi.fn();
    const require = createLoader({
      files: {
        ...files,
        "App.tsx":
          'import "./style.css";\nimport data from "./data.json";\nimport icon from "./icon.svg";\nimport { label } from "./lib";\n' +
          "export default function App() { return <b>{label(data.a)}</b>; }\nexport const src = icon;\n",
      },
      builtins,
      onCss: css,
    });
    const mod = require("App.tsx") as { default: () => { p: { children: string } }; src: string };
    expect(mod.default().p.children).toBe("#1");
    expect(mod.src.startsWith("data:image/svg+xml")).toBe(true);
    expect(css).toHaveBeenCalledWith("style.css", "b { color: red; }");
  });

  it("evaluates each file once and allows cycles", () => {
    const require = createLoader({
      files: {
        "a.ts": 'import { b } from "./b";\nexport const a = 1;\nexport const fromB = () => b;\n',
        "b.ts": 'import { a } from "./a";\nexport const b = 2;\nexport const fromA = () => a;\n',
      },
      builtins,
      onCss: () => {},
    });
    const a = require("a.ts") as { fromB: () => number };
    const b = require("b.ts") as { fromA: () => number };
    expect(a.fromB()).toBe(2);
    expect(b.fromA()).toBe(1);
  });

  it("refuses packages it does not ship and missing files, naming the importer", () => {
    const require = createLoader({
      files: { "App.tsx": 'import x from "lodash";\nexport default x;', "B.tsx": 'import "./missing";' },
      builtins,
      onCss: () => {},
    });
    expect(() => require("App.tsx")).toThrow(/Cannot import "lodash".*react/);
    try {
      require("B.tsx");
    } catch (e) {
      expect(e).toBeInstanceOf(ViewError);
      expect((e as ViewError).file).toBe("B.tsx");
      expect((e as ViewError).message).toContain("./missing");
    }
  });

  it("reports a runtime error with the file that threw", () => {
    const require = createLoader({ files: { "x.ts": 'throw new Error("boom");' }, builtins, onCss: () => {} });
    expect(() => require("x.ts")).toThrow("boom");
    try {
      require("x.ts");
    } catch (e) {
      expect((e as ViewError).file).toBe("x.ts");
    }
  });
});

describe("protocol", () => {
  it("accepts only well-formed messages on its channel", () => {
    expect(isToHost({ channel: CHANNEL, type: "ready" })).toBe(true);
    expect(isToHost({ channel: CHANNEL, type: "request", id: 1, method: "table", args: { node: "prices" } })).toBe(true);
    expect(isToHost({ channel: CHANNEL, type: "request", id: 1, method: "query", args: { sql: "select 1" } })).toBe(true);
    expect(isToHost({ channel: CHANNEL, type: "request", id: 1, method: "setParams", args: { params: { as_of: "x" } } })).toBe(true);
    expect(isToHost({ channel: "other", type: "ready" })).toBe(false);
    expect(isToHost({ channel: CHANNEL, type: "request", id: "1", method: "table", args: { node: "p" } })).toBe(false);
    expect(isToHost({ channel: CHANNEL, type: "request", id: 1, method: "run", args: {} })).toBe(false);
    expect(isToHost({ channel: CHANNEL, type: "request", id: 1, method: "table", args: {} })).toBe(false);
    expect(isToHost({ channel: CHANNEL, type: "request", id: 1, method: "setParams", args: { params: [] } })).toBe(false);
    expect(isToHost(null)).toBe(false);
    expect(isToFrame({ channel: CHANNEL, type: "changed", nodes: [], params: { params: {}, specs: [] } })).toBe(true);
    expect(isToFrame({ channel: CHANNEL, type: "request" })).toBe(false);
  });
});

describe("host", () => {
  it("refuses nodes the view does not read", () => {
    const view = { name: "board", reads: ["prices"] };
    expect(refusal(view, "prices")).toBeNull();
    expect(refusal(view, "universe")).toMatch(/does not read universe/);
  });

  it("builds a frame document that names no URL and runs only its bootstrap", () => {
    const doc = frameDocument("abc123");
    const csp = /Content-Security-Policy" content="([^"]+)"/.exec(doc)?.[1] ?? "";
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("connect-src 'none'");
    expect(csp).toContain("script-src 'nonce-abc123' 'unsafe-eval'");
    expect(csp).not.toMatch(/https?:|'self'|\*|script-src[^;]*'unsafe-inline'/);
    expect(doc).not.toMatch(/<script[^>]* src=/);
    expect(doc.match(/<script/g)).toHaveLength(1);
    expect(doc).toContain('<script nonce="abc123">');
    expect(doc).toContain(`"${CHANNEL}"`);
  });

  it("a new nonce for every frame", () => {
    const n = (d: string) => /nonce-([^']+)'/.exec(d)?.[1];
    expect(n(frameDocument())).not.toBe(n(frameDocument()));
  });
});
