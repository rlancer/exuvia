import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import stylex from "@stylexjs/unplugin";
import { markSvg } from "./src/mark";

/** The tab icon is the exuvia mark, inlined so the page asks the server for nothing. */
const icon = `data:image/svg+xml,${encodeURIComponent(markSvg())}`;

/**
 * The StyleX plugin keeps every package that depends on StyleX out of Vite's
 * dependency pre-bundling, to compile it. Astryx is already compiled, so that
 * only makes the dev server send its thousands of files one by one: this puts
 * it back among the pre-bundled dependencies.
 */
const prebundleAstryx: Plugin = {
  name: "exuvia-prebundle-astryx",
  config(config) {
    if (config.optimizeDeps?.exclude) config.optimizeDeps.exclude = config.optimizeDeps.exclude.filter((d) => !d.startsWith("@astryxdesign/"));
  },
};

export default defineConfig({
  // StyleX compiles the app's own styles (`stylex.create` in src/); Astryx's
  // components come prebuilt (astryx.css, imported in main.tsx). The dev
  // server serves the styles from a virtual stylesheet the plugin links itself.
  plugins: [stylex.vite({ useCSSLayers: true }), prebundleAstryx, react(), { name: "exuvia-icon", transformIndexHtml: { order: "pre", handler: (html) => html.replace("data:,exuvia-icon", icon) } }],
  base: "/",
  // Busts the browser cache of the views runtime (view-runtime/runtime.js) per build.
  define: { __EXUVIA_BUILD__: JSON.stringify(Date.now().toString(36)) },
  server: {
    port: 5180,
    proxy: {
      // Includes the views runtime (`/api/view-runtime/runtime.js`), a
      // separate build (vite.runtime.config.ts): run `bun run build` once.
      "/api": { target: process.env.EXUVIA_API ?? "http://127.0.0.1:7420", changeOrigin: true },
    },
  },
  build: {
    outDir: "../exuvia/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 2500,
  },
});
