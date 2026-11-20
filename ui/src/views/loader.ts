/**
 * The module system of a view, run inside its iframe: compiles each file of
 * the view with Sucrase (TypeScript + JSX → CommonJS) on first `require` and
 * evaluates it. No bundler, no network: a view can import
 *
 *  - its own files by relative path (`./Chart`, `../lib/format.ts`); the
 *    extension may be left out, and `./dir` finds `./dir/index.tsx`;
 *  - `.css` files (injected as a <style>), `.json` (the parsed value) and
 *    `.svg` (a data: URL string);
 *  - the packages the runtime ships, by bare name (`BUILTINS` in runtime.tsx).
 */
import { transform } from "sucrase";

const CODE = [".tsx", ".ts", ".jsx", ".js"];
const RESOLVE = [...CODE, ".json", ".css", ".svg"];

/** A file that failed to compile or evaluate, with where. */
export class ViewError extends Error {
  file?: string;
  constructor(message: string, file?: string) {
    super(message);
    this.name = "ViewError";
    this.file = file;
  }
}

function normalize(parts: string[]): string | null {
  const out: string[] = [];
  for (const p of parts) {
    if (p === "" || p === ".") continue;
    if (p === "..") {
      if (!out.length) return null; // above the view folder
      out.pop();
    } else out.push(p);
  }
  return out.join("/");
}

function dirname(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

/** The view file `spec` (a relative import in `from`) names, or null. */
export function resolveImport(from: string, spec: string, files: Record<string, string>): string | null {
  if (!spec.startsWith("./") && !spec.startsWith("../")) return null;
  const base = normalize([...dirname(from).split("/"), ...spec.split("/")]);
  if (base === null) return null;
  const has = (p: string) => Object.prototype.hasOwnProperty.call(files, p);
  if (base && has(base)) return base;
  for (const ext of RESOLVE) if (has(base + ext)) return base + ext;
  for (const ext of CODE) if (has(`${base ? base + "/" : ""}index${ext}`)) return `${base ? base + "/" : ""}index${ext}`;
  return null;
}

export function isBare(spec: string): boolean {
  return !spec.startsWith(".") && !spec.startsWith("/");
}

/** One file → CommonJS source (code files) or null (handled as an asset). */
export function compile(path: string, source: string): string {
  const ext = path.slice(path.lastIndexOf("."));
  const transforms: ("typescript" | "jsx" | "imports")[] =
    ext === ".tsx" ? ["typescript", "jsx", "imports"]
    : ext === ".ts" ? ["typescript", "imports"]
    : ["jsx", "imports"];
  try {
    return transform(source, {
      transforms,
      filePath: path,
      jsxRuntime: "automatic",
      production: true,
      disableESTransforms: true,
    }).code;
  } catch (e) {
    throw new ViewError(e instanceof Error ? e.message : String(e), path);
  }
}

export interface LoaderOptions {
  files: Record<string, string>;
  /** Packages a view may import by bare name, as module objects. */
  builtins: Record<string, unknown>;
  /** Called once per imported .css file. */
  onCss: (path: string, css: string) => void;
}

type Module = { exports: Record<string, unknown> };

/** A `require` over the view's files. Each module is compiled and evaluated
 * once; a cycle sees the partly-filled exports, as in CommonJS. */
export function createLoader({ files, builtins, onCss }: LoaderOptions): (path: string) => Record<string, unknown> {
  const cache = new Map<string, Module>();

  const load = (path: string): Record<string, unknown> => {
    const hit = cache.get(path);
    if (hit) return hit.exports;
    const source = files[path];
    if (source === undefined) throw new ViewError(`No file ${path} in this view.`, path);
    const mod: Module = { exports: {} };
    cache.set(path, mod);
    if (path.endsWith(".css")) {
      onCss(path, source);
      return mod.exports;
    }
    if (path.endsWith(".json")) {
      try {
        mod.exports = { __esModule: true, default: JSON.parse(source) } as Record<string, unknown>;
      } catch (e) {
        throw new ViewError(`Invalid JSON: ${(e as Error).message}`, path);
      }
      return mod.exports;
    }
    if (path.endsWith(".svg")) {
      mod.exports = { __esModule: true, default: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(source)}` };
      return mod.exports;
    }
    if (!CODE.some((ext) => path.endsWith(ext))) throw new ViewError(`Cannot import ${path}.`, path);
    const code = compile(path, source);
    const require = (spec: string): unknown => {
      if (isBare(spec)) {
        if (Object.prototype.hasOwnProperty.call(builtins, spec)) return builtins[spec];
        throw new ViewError(
          `Cannot import "${spec}". Views run offline and can import only their own files and: ${Object.keys(builtins).join(", ")}.`,
          path,
        );
      }
      const target = resolveImport(path, spec, files);
      if (target === null) throw new ViewError(`Cannot find "${spec}" from ${path}.`, path);
      return load(target);
    };
    let fn: (require: unknown, module: Module, exports: unknown) => void;
    try {
      // The sourceURL names the file in stack traces and the browser's devtools.
      fn = new Function("require", "module", "exports", `${code}\n//# sourceURL=view:///${path}`) as typeof fn;
    } catch (e) {
      throw new ViewError((e as Error).message, path);
    }
    try {
      fn(require, mod, mod.exports);
    } catch (e) {
      cache.delete(path);
      if (e instanceof ViewError) throw e;
      const err = new ViewError(e instanceof Error ? e.message : String(e), path);
      if (e instanceof Error) err.stack = e.stack;
      throw err;
    }
    return mod.exports;
  };

  return load;
}
