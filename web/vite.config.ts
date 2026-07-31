import path from "node:path";

import react from "@vitejs/plugin-react";
// From `vitest/config`, not `vite` — vite 8's own defineConfig rejects the `test` key
// (TS2769: 'test' does not exist in type 'UserConfigExport'). vitest re-exports a widened
// version that accepts it, which keeps test config colocated with the build config.
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:5959",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
