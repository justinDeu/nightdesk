import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// The SPA talks only to the JSON API. In dev we proxy /api and /auth to a
// throwaway test instance on :8799 (never the live :8765). See frontend/README.md.
const API_TARGET = process.env.NIGHTDESK_API ?? "http://127.0.0.1:8799";

// FastAPI now mounts the SPA at the root (/assets, index.html fallback, and a
// legacy /app 308→/), so the default base "/" is correct for both dev and prod.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
      "/auth": { target: API_TARGET, changeOrigin: true },
    },
  },
});
