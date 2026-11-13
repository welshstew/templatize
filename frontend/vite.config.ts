import { defineConfig } from "vite";

// API routes served by the FastAPI backend; proxied in `npm run dev`.
const api = ["/suggest", "/build", "/render", "/download", "/info", "/healthz"];

export default defineConfig({
  oxc: { jsx: { runtime: "automatic", importSource: "preact" } },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: false,
    // Everything is bundled locally; nothing is fetched at runtime.
    assetsInlineLimit: 0,
  },
  server: {
    proxy: Object.fromEntries(api.map((p) => [p, "http://127.0.0.1:8000"])),
  },
});
