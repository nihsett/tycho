import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The bundle is served by the dashboard API from the same origin, so there is
 * no API base URL to configure and no credential to embed. In development the
 * proxy forwards /api to a locally running dashboard service.
 */
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
    target: "es2022",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: false },
    },
  },
});
