import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
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
