import type { Params } from "./views/protocol";
import type { ProjectParam } from "./api";

/**
 * App mode: one view full screen, without the workbench around it, at
 * `?app=<view>`. Run parameters other than their defaults ride along as
 * `p.<name>=<value>`, so a link opens the view as it was shown.
 */

const PREFIX = "p.";

/** The view the page opens as an app (`?app=<view>`), or null for the workbench. */
export function appViewOf(search: string): string | null {
  const v = new URLSearchParams(search).get("app");
  return v && /^[a-z_][a-z0-9_]*$/.test(v) ? v : null;
}

/** The parameter values in an app address, typed by the project's parameters; unknown names are dropped. */
export function paramsOf(search: string, specs: ProjectParam[]): Params {
  const q = new URLSearchParams(search);
  const out: Params = {};
  for (const s of specs) {
    const raw = q.get(PREFIX + s.name);
    if (raw === null) continue;
    if (s.type === "number") {
      const n = Number(raw);
      if (raw.trim() !== "" && Number.isFinite(n)) out[s.name] = n;
    } else if (s.type === "boolean") {
      if (raw === "true" || raw === "false") out[s.name] = raw === "true";
    } else out[s.name] = raw;
  }
  return out;
}

/** The query string of an app address: the view, then its parameters in name order. */
export function appSearch(view: string, params: Params = {}): string {
  const q = new URLSearchParams({ app: view });
  for (const k of Object.keys(params).sort()) {
    const v = params[k];
    if (v !== null && v !== undefined && v !== "") q.set(PREFIX + k, String(v));
  }
  return `?${q.toString()}`;
}

/** Where this project's pages live: `/p/<project>/` on a hub, `/` on its own server. */
export function projectRoot(project: string | null): string {
  return project ? `/p/${encodeURIComponent(project)}/` : "/";
}

/** The whole address of a view as an app. */
export function appUrl(origin: string, project: string | null, view: string, params?: Params): string {
  return origin + projectRoot(project) + appSearch(view, params);
}

/** A share link (`…/s/<token>`) that lands on a view as an app; without a view, on the workbench. */
export function linkToApp(link: string, view: string | null): string {
  return view ? `${link}?app=${encodeURIComponent(view)}` : link;
}
