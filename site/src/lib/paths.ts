// The site can be served from the root of a host or from a sub-path of one
// (`astro.config.mjs` reads SITE_BASE). Every internal link and asset
// reference goes through `url()` so it follows the base.

// `import.meta.env.BASE_URL` is "/" at the root and "/<base>/" otherwise.
const BASE = import.meta.env.BASE_URL.replace(/\/+$/, "");

/** Prefix a root-relative path ("/docs/", "/#node", "/og/home.png") with the site base. */
export function url(path: string): string {
  if (!path.startsWith("/")) throw new Error(`url() takes a root-relative path, got "${path}"`);
  return BASE + path;
}

/**
 * `url()`, made absolute when the build knows its origin (SITE_URL). Text an
 * agent reads away from the site (the Markdown pages, llms.txt) uses this so
 * its links still resolve once copied elsewhere.
 */
export function absolute(path: string): string {
  const site = import.meta.env.SITE;
  return site ? new URL(url(path), site).href : url(path);
}

/** The Markdown twin of a docs page: /docs/nodes/ → /docs/nodes.md */
export function markdownPath(slug: string): string {
  return `/docs/${slug}.md`;
}
