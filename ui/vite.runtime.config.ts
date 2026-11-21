// The views runtime (src/views/runtime.tsx): one self-contained classic
// script. A view's sandboxed iframe fetches nothing (its opaque origin may
// not reach a local server, and its CSP names no URL): the page fetches this
// file and posts its source to the frame's bootstrap, which evaluates it. So
// it has to be a single IIFE with every dependency inside, no chunks.
// Built after the app (`bun run build`), into exuvia/static/view-runtime/.
import { defineConfig } from "vite";

export default defineConfig({
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  esbuild: { jsx: "automatic" },
  build: {
    outDir: "../exuvia/static/view-runtime",
    emptyOutDir: true,
    copyPublicDir: false,
    chunkSizeWarningLimit: 4000,
    lib: {
      entry: "src/views/runtime.tsx",
      formats: ["iife"],
      name: "exuviaViewRuntime",
      fileName: () => "runtime.js",
    },
  },
});
