import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the API (make api) so the SPA and API share an origin.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "dist", sourcemap: true, chunkSizeWarningLimit: 4000 },
});
