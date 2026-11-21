import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { markSvg } from "./src/mark";

/** The tab icon is the exuvia mark, inlined so the page asks the server for nothing. */
const icon = `data:image/svg+xml,${encodeURIComponent(markSvg())}`;

export default defineConfig({
  plugins: [react(), { name: "exuvia-icon", transformIndexHtml: { order: "pre", handler: (html) => html.replace("data:,exuvia-icon", icon) } }],
  base: "/",
  // Busts the browser cache of the views runtime (view-runtime/runtime.js) per build.
  define: { __EXUVIA_BUILD__: JSON.stringify(Date.now().toString(36)) },
  server: {
    port: 5180,
    proxy: {
      // Includes the views runtime (`/api/view-runtime/runtime.js`), a
      // separate build (vite.runtime.config.ts): run `bun run build` once.
      "/api": { target: "http://127.0.0.1:7420", changeOrigin: true },
    },
  },
  build: {
    outDir: "../exuvia/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 2500,
  },
});
