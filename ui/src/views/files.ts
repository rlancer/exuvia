/** The order the Code pane lists a view's files in: the manifest first, then the entry, then the rest by path. */
export function fileOrder(files: Iterable<string>, entry: string): string[] {
  const rank = (f: string) => (f === "view.json" ? 0 : f === entry ? 1 : 2);
  return [...new Set(files)].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}
