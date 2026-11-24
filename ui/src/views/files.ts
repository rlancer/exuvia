/** The order the Code pane lists a view's files in: the manifest first, then the entry, then the rest by path. */
export function fileOrder(files: Iterable<string>, entry: string): string[] {
  const rank = (f: string) => (f === "view.json" ? 0 : f === entry ? 1 : 2);
  return [...new Set(files)].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

/** The npm package a view's error says is missing from package.json (loader.ts), or null. */
export function missingNpmPackage(error: string): string | null {
  const m = /^Cannot import "[^"]+": (\S+) is not in the project's package\.json/.exec(error);
  return m ? m[1] : null;
}
