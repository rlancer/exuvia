import { defineConfig } from "astro/config";

// SITE_URL (e.g. https://exuvia.dev) makes canonical and social-card URLs
// absolute. Without it the site still builds and every link stays relative.
// It is the origin only; the sub-path goes in SITE_BASE.
//
// SITE_BASE (e.g. /exuvia) serves the site from a sub-path of a host instead
// of its root: links, assets and the search index follow it. Unset, the site
// is built for the root of its host.
const base = (process.env.SITE_BASE || "").trim().replace(/^\/*/, "/").replace(/\/+$/, "");

export default defineConfig({
  site: process.env.SITE_URL || undefined,
  base: base || undefined,
  trailingSlash: "always",
  devToolbar: { enabled: false },
  server: { port: 4380 },
});
