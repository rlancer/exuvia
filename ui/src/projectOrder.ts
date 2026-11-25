import type { HubProject } from "./api";

/**
 * How the hub's home page orders its projects (exuvia/activity.py says when
 * each was used and changed): what you used last first, what anyone changed
 * last first, or by name.
 */
export type ProjectSort = "used" | "changed" | "name";

export const SORT_LABEL: Record<ProjectSort, string> = {
  used: "Recently used",
  changed: "Recently changed",
  name: "Name",
};

export const isProjectSort = (s: unknown): s is ProjectSort => typeof s === "string" && s in SORT_LABEL;

/** When you last opened or changed it (null: never). */
export const usedAt = (p: HubProject): number | null => Math.max(p.opened_at ?? 0, p.edited_at ?? 0) || null;

const title = (p: HubProject) => p.name || p.id;
const byName = (a: HubProject, b: HubProject) =>
  title(a).localeCompare(title(b), undefined, { numeric: true, sensitivity: "base" }) || a.id.localeCompare(b.id);
/** Newest first; what has no time goes after what has one. */
const newest = (a: number | null | undefined, b: number | null | undefined) => (b ?? 0) - (a ?? 0);

/** The projects in ``sort``'s order. What you never used goes after what you did, newest change first. */
export function sortProjects(projects: HubProject[], sort: ProjectSort): HubProject[] {
  const order: Record<ProjectSort, (a: HubProject, b: HubProject) => number> = {
    used: (a, b) => newest(usedAt(a), usedAt(b)) || newest(a.changed_at, b.changed_at) || byName(a, b),
    changed: (a, b) => newest(a.changed_at, b.changed_at) || byName(a, b),
    name: byName,
  };
  return [...projects].sort(order[sort]);
}

/** How far back "Recently worked on" looks. */
export const RECENT_DAYS = 30;
/** How many it shows at most. */
export const RECENT_MAX = 5;

/** What you created or changed in the last ``RECENT_DAYS``, the latest first, at most ``RECENT_MAX``. */
export function recentWork(projects: HubProject[], now = Date.now() / 1000): HubProject[] {
  const since = now - RECENT_DAYS * 86400;
  return projects
    .filter((p) => (p.edited_at ?? 0) > since)
    .sort((a, b) => newest(a.edited_at, b.edited_at) || byName(a, b))
    .slice(0, RECENT_MAX);
}
