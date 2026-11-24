/**
 * The module system of a view, run inside its iframe: compiles each file of
 * the view with Sucrase (TypeScript + JSX → CommonJS) on first `require` and
 * evaluates it. No bundler, no network: a view can import
 *
 *  - its own files by relative path (`./Chart`, `../lib/format.ts`); the
 *    extension may be left out, and `./dir` finds `./dir/index.tsx`;
 *  - `.css` files (injected as a <style>), `.json` (the parsed value) and
 *    `.svg` (a data: URL string);
 *  - the packages the runtime ships, by bare name (`BUILTINS` in runtime.tsx);
 *  - the npm packages in the project's package.json, by bare name: the page
 *    sends the modules each is made of (`PackageModules`, from
 *    `GET /api/views/{name}/packages`), keyed by their path on the CDN
 *    (`/recharts@2.15.0/...`). Their imports of each other are those paths;
 *    their imports of the runtime's packages stay bare and get the same copy.
 */
import { transform } from "sucrase";
import { NO_PACKAGES, type PackageModules } from "./protocol";

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

/** The package a bare import names: `lodash-es` for `lodash-es/debounce`, `@tanstack/react-table`. */
export function packageOf(spec: string): string {
  const parts = spec.split("/");
  return spec.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
}

/** An npm module's id (a path on the CDN), as opposed to a file of the view. */
export const isPackageModule = (id: string) => id.startsWith("/");

/** The module id an import inside the npm module `from` names (`/x.mjs`, `./x.mjs`). */
export function resolvePackageImport(from: string, spec: string): string {
  if (spec.startsWith("/")) return spec;
  const u = new URL(spec, `https://cdn.invalid${from}`);
  return u.pathname + u.search;
}

/** One file → CommonJS source. An npm module is JavaScript already built
 * for the browser: only its imports are rewritten. */
export function compile(path: string, source: string): string {
  const pkg = isPackageModule(path);
  const ext = path.slice(path.lastIndexOf("."));
  const transforms: ("typescript" | "jsx" | "imports")[] =
    pkg ? ["imports"]
    : ext === ".tsx" ? ["typescript", "jsx", "imports"]
    : ext === ".ts" ? ["typescript", "imports"]
    : ["jsx", "imports"];
  // A module is evaluated as a function body, where import.meta does not parse.
  if (pkg) source = source.replace(/\bimport\.meta\b/g, "__importMeta");
  try {
    return transform(source, {
      transforms,
      filePath: path,
      jsxRuntime: "automatic",
      production: true,
      disableESTransforms: true,
    }).code;
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    throw pkg ? new ViewError(`${path}: ${message}`) : new ViewError(message, path);
  }
}

export interface LoaderOptions {
  files: Record<string, string>;
  /** Packages a view may import by bare name, as module objects. */
  builtins: Record<string, unknown>;
  /** The project's npm packages the view imports. */
  packages?: PackageModules;
  /** Called once per imported .css file. */
  onCss: (path: string, css: string) => void;
}

type Module = { exports: Record<string, unknown> };

/** A `require` over the view's files. Each module is compiled and evaluated
 * once; a cycle sees the partly-filled exports, as in CommonJS. */
export function createLoader({ files, builtins, onCss, packages = NO_PACKAGES }: LoaderOptions): (path: string) => Record<string, unknown> {
  const cache = new Map<string, Module>();
  const has = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k);

  /** A bare import in `path`: a package the runtime ships, then one of the project's npm packages. */
  const bare = (spec: string, path: string): unknown => {
    if (has(builtins, spec)) return builtins[spec];
    if (has(packages.entries, spec)) return load(packages.entries[spec]);
    if (isPackageModule(path))
      throw new ViewError(`The npm module ${path} imports "${spec}", which was not loaded: import "${spec}" in the view too.`);
    if (has(packages.errors, spec)) throw new ViewError(packages.errors[spec], path);
    const name = packageOf(spec);
    if (packages.declared.includes(name))
      throw new ViewError(`Cannot import "${spec}": ${packages.error ?? `the modules of ${name} were not loaded`}.`, path);
    throw new ViewError(
      `Cannot import "${spec}": ${name} is not in the project's package.json. Add it under Packages (npm, for views); ` +
        `without it, a view can import its own files and: ${Object.keys(builtins).join(", ")}.`,
      path,
    );
  };

  const load = (path: string): Record<string, unknown> => {
    const hit = cache.get(path);
    if (hit) return hit.exports;
    const pkg = isPackageModule(path);
    const source = pkg ? packages.modules[path] : files[path];
    if (source === undefined) throw pkg ? new ViewError(`The npm module ${path} was not loaded.`) : new ViewError(`No file ${path} in this view.`, path);
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
    if (!pkg && !CODE.some((ext) => path.endsWith(ext))) throw new ViewError(`Cannot import ${path}.`, path);
    const code = compile(path, source);
    const require = (spec: string): unknown => {
      if (isBare(spec)) return bare(spec, path);
      if (pkg) return load(resolvePackageImport(path, spec));
      const target = resolveImport(path, spec, files);
      if (target === null) throw new ViewError(`Cannot find "${spec}" from ${path}.`, path);
      return load(target);
    };
    // An error in an npm module points at no file of the view; its message names the module.
    const file = pkg ? undefined : path;
    let fn: (require: unknown, module: Module, exports: unknown, importMeta: unknown) => void;
    try {
      // The sourceURL names the file in stack traces and the browser's devtools.
      const url = pkg ? `npm://${path}` : `view:///${path}`;
      fn = new Function("require", "module", "exports", "__importMeta", `${code}\n//# sourceURL=${url}`) as typeof fn;
    } catch (e) {
      throw new ViewError(pkg ? `${path}: ${(e as Error).message}` : (e as Error).message, file);
    }
    try {
      fn(require, mod, mod.exports, { url: `https://cdn.invalid${path}` });
    } catch (e) {
      cache.delete(path);
      if (e instanceof ViewError) throw e;
      const err = new ViewError(e instanceof Error ? e.message : String(e), file);
      if (e instanceof Error) err.stack = e.stack;
      throw err;
    }
    return mod.exports;
  };

  return load;
}
