import { describe, expect, it, vi } from "vitest";
import { ViewError, compile, createLoader, packageOf, resolveImport, resolvePackageImport } from "./loader";
import { CHANNEL, NO_PACKAGES, isToFrame, isToHost } from "./protocol";
import { ViewBridge, frameDocument, refusal, resolveOutput, splitOutput } from "./host";
import { fileOrder, missingNpmPackage } from "./files";

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
    const dl = (args: unknown) => isToHost({ channel: CHANNEL, type: "request", id: 1, method: "download", args });
    expect(dl({ node: "prices", format: "csv" })).toBe(true);
    expect(dl({ sql: "select 1", format: "xlsx", filename: "x" })).toBe(true);
    expect(dl({ node: "prices", sql: "select 1", format: "csv" })).toBe(false); // one or the other
    expect(dl({ format: "csv" })).toBe(false);
    expect(dl({ node: "prices", format: "docx" })).toBe(false);
    expect(dl({ node: "prices", format: "csv", filename: 3 })).toBe(false);
    expect(isToHost(null)).toBe(false);
    expect(isToFrame({ channel: CHANNEL, type: "changed", nodes: [], params: { params: {}, specs: [] } })).toBe(true);
    expect(isToFrame({ channel: CHANNEL, type: "refreshing", nodes: ["prices"] })).toBe(true);
    expect(isToFrame({ channel: CHANNEL, type: "request" })).toBe(false);
  });
});

describe("live refresh", () => {
  const view = { name: "board", title: "", reads: ["prices", "universe"], outputs: [] } as never;
  const params = { params: {}, specs: [] };

  it("tells the frame which read nodes are being re-run, once per change", () => {
    const sent: unknown[] = [];
    const frame = { contentWindow: { postMessage: (m: unknown) => sent.push(m) } } as unknown as HTMLIFrameElement;
    const b = new ViewBridge(frame, view, params, false, { setParams: async () => params, onStatus: () => {} });
    b.setRefreshing(["prices"]);
    b.setRefreshing(["prices"]);
    b.setRefreshing([]);
    expect(sent).toEqual([
      { channel: CHANNEL, type: "refreshing", nodes: ["prices"] },
      { channel: CHANNEL, type: "refreshing", nodes: [] },
    ]);
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

describe("fileOrder", () => {
  it("lists the manifest, then the entry, then the rest by path, once each", () => {
    expect(fileOrder(["styles.css", "components/Chart.tsx", "App.tsx", "view.json", "App.tsx"], "App.tsx")).toEqual([
      "view.json",
      "App.tsx",
      "components/Chart.tsx",
      "styles.css",
    ]);
    expect(fileOrder(["b.ts", "a.ts"], "Main.tsx")).toEqual(["a.ts", "b.ts"]);
  });
});

describe("outputs of a node with several", () => {
  const outputs = [
    { node: "prices", state: "fresh" as const, artifact_id: "a1", kind: "table" as const },
    {
      node: "fit",
      state: "fresh" as const,
      artifact_id: "f1",
      kind: "multi" as const,
      parts: [
        { name: "coefs", kind: "table" as const, rows: 2 },
        { name: "metrics", kind: "json" as const, rows: null },
      ],
    },
  ];

  it("reads node.part and checks it against the node's reads", () => {
    expect(splitOutput("fit.coefs")).toEqual({ node: "fit", part: "coefs" });
    expect(splitOutput("prices")).toEqual({ node: "prices", part: null });
    expect(refusal({ name: "v", reads: ["fit"] }, "fit.coefs")).toBeNull();
    expect(refusal({ name: "v", reads: ["prices"] }, "fit.coefs")).toMatch(/does not read fit/);
  });

  it("resolves a part, and says what is wrong otherwise", () => {
    expect(resolveOutput(outputs, "fit.coefs", "table")).toEqual({ node: "fit", part: "coefs", artifact_id: "f1" });
    expect(resolveOutput(outputs, "fit.metrics", "json")).toEqual({ node: "fit", part: "metrics", artifact_id: "f1" });
    expect(resolveOutput(outputs, "prices", "table")).toEqual({ node: "prices", part: null, artifact_id: "a1" });
    expect(() => resolveOutput(outputs, "fit", "table")).toThrow(/several outputs: read one of fit.coefs, fit.metrics/);
    expect(() => resolveOutput(outputs, "fit.nope", "table")).toThrow(/no output nope/);
    expect(() => resolveOutput(outputs, "fit.metrics", "table")).toThrow(/is json, not a table: use json\(\)/);
    expect(() => resolveOutput(outputs, "prices.x", "table")).toThrow(/single output/);
  });
});

describe("npm packages", () => {
  const react = { __esModule: true, default: { version: "19" }, useState: (v: unknown) => [v] };
  const builtins = { react };
  // What esm.sh serves: a stub that re-exports the bundle by its absolute path,
  // a bundle that imports react bare and a helper by a relative path.
  const STUB = "/chartlib@1.2.3?bundle&external=react&target=es2022";
  const BUNDLE = "/chartlib@1.2.3/X-ZXh0/es2022/chartlib.bundle.mjs";
  const HELPER = "/chartlib@1.2.3/X-ZXh0/es2022/helper.mjs";
  const packages = {
    declared: ["chartlib", "broken"],
    entries: { chartlib: STUB, "chartlib/style.css": "/chartlib@1.2.3/style.css" },
    modules: {
      [STUB]: `/* esm.sh - chartlib@1.2.3 */\nexport * from "${BUNDLE}";\nexport { default } from "${BUNDLE}";\n`,
      [BUNDLE]:
        'import{useState as u}from"react";import{twice}from"./helper.mjs";' +
        "export function Chart(){return u(twice(2))[0]}export const where=import.meta.url;export default 7;",
      [HELPER]: "export const twice=(n)=>n*2;",
      "/chartlib@1.2.3/style.css": ".chart{color:red}",
    },
    errors: { broken: "broken could not be loaded from https://esm.sh: 404 Not Found" },
  };

  it("loads a package's modules, sharing the runtime's react", () => {
    const css = vi.fn();
    const require = createLoader({
      files: {
        "App.tsx":
          'import "chartlib/style.css";\nimport seven, { Chart, where } from "chartlib";\nexport const out = [seven, Chart(), where];\n',
      },
      builtins,
      packages,
      onCss: css,
    });
    const mod = require("App.tsx") as { out: [number, number, string] };
    expect(mod.out).toEqual([7, 4, `https://cdn.invalid${BUNDLE}`]);
    expect(css).toHaveBeenCalledWith("/chartlib@1.2.3/style.css", ".chart{color:red}");
  });

  it("says why a declared package is missing and points at the view's file", () => {
    const require = createLoader({ files: { "App.tsx": 'import "broken";' }, builtins, packages, onCss: () => {} });
    try {
      require("App.tsx");
      throw new Error("no error");
    } catch (e) {
      expect((e as ViewError).message).toContain("404 Not Found");
      expect((e as ViewError).file).toBe("App.tsx");
    }
    const failed = createLoader({
      files: { "App.tsx": 'import "chartlib";' },
      builtins,
      packages: { ...NO_PACKAGES, declared: ["chartlib"], error: "the server could not be reached" },
      onCss: () => {},
    });
    expect(() => failed("App.tsx")).toThrow(/Cannot import "chartlib": the server could not be reached/);
    // As the page sends it when GET /packages fails: nothing declared, only the error.
    const unread = createLoader({
      files: { "App.tsx": 'import "chartlib";' },
      builtins,
      packages: { ...NO_PACKAGES, error: "package.json is not valid JSON" },
      onCss: () => {},
    });
    expect(() => unread("App.tsx")).toThrow(/Cannot import "chartlib": package.json is not valid JSON/);
  });

  it("offers no package to add for a runtime package's other parts or a node: import", () => {
    for (const spec of ["react/jsx-dev-runtime", "node:fs"]) {
      const require = createLoader({ files: { "App.tsx": `import "${spec}";` }, builtins, packages, onCss: () => {} });
      try {
        require("App.tsx");
        throw new Error("no error");
      } catch (e) {
        expect((e as ViewError).message).toContain(`Cannot import "${spec}"`);
        expect(missingNpmPackage((e as ViewError).message)).toBeNull();
      }
    }
  });

  it("names a package that is not in package.json, so the page can offer to add it", () => {
    // (TypeScript drops an import nothing uses: this one is used.)
    const files = { "App.tsx": 'import { x } from "@scope/thing/sub";\nexport const y = x;' };
    const require = createLoader({ files, builtins, packages, onCss: () => {} });
    try {
      require("App.tsx");
      throw new Error("no error");
    } catch (e) {
      expect(missingNpmPackage((e as ViewError).message)).toBe("@scope/thing");
    }
    expect(missingNpmPackage("Cannot find ./x from App.tsx.")).toBeNull();
  });

  it("an error inside a package names its module, not a file of the view", () => {
    const id = "/bad@1.0.0/es2022/bad.mjs";
    const require = createLoader({
      files: { "App.tsx": 'import "bad";' },
      builtins,
      packages: { ...NO_PACKAGES, declared: ["bad"], entries: { bad: id }, modules: { [id]: 'throw new Error("kaput")' } },
      onCss: () => {},
    });
    try {
      require("App.tsx");
      throw new Error("no error");
    } catch (e) {
      expect((e as ViewError).message).toBe("kaput");
      expect((e as ViewError).file).toBeUndefined();
    }
  });

  it("resolves package names and module paths", () => {
    expect(packageOf("lodash-es/debounce")).toBe("lodash-es");
    expect(packageOf("@tanstack/react-table")).toBe("@tanstack/react-table");
    expect(resolvePackageImport("/a@1.0.0/es2022/x.mjs", "./y.mjs")).toBe("/a@1.0.0/es2022/y.mjs");
    expect(resolvePackageImport("/a@1.0.0/es2022/x.mjs", "/b@2.0.0?target=es2022")).toBe("/b@2.0.0?target=es2022");
    expect(compile(BUNDLE, "export const m = import.meta.url;")).toContain("__importMeta.url");
  });
});
