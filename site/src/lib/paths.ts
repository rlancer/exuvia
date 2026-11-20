// The site can be served from the root of a host or from a sub-path of one
// (`astro.config.mjs` reads SITE_BASE). Every internal link and asset
// reference goes through `url()` so it follows the base.

// `import.meta.env.BASE_URL` is "/" at the root and "/<base>/" otherwise.
const BASE = import.meta.env.BASE_URL.replace(/\/+$/, "");

/** Prefix a root-relative path ("/docs/", "/#node", "/og.png") with the site base. */
export function url(path: string): string {
  if (!path.startsWith("/")) throw new Error(`url() takes a root-relative path, got "${path}"`);
  return BASE + path;
}
