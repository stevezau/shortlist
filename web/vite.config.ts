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
    // vitest's default is 5000ms, and it measures WALL CLOCK while every other worker is competing
    // for the same cores. These are component tests: each one boots a jsdom environment and drives
    // it through real `userEvent` interactions, so the per-test cost is a little work and a large
    // multiple of that in scheduling. Measured on this suite: `row-editor.test.tsx` runs its 106
    // tests in 11s ON ITS OWN and takes 145s inside the full run — the same tests, 13x the wall
    // clock, purely from contention. At a 5s budget that put a shifting handful over the line on
    // every run: 0 to 6 failures, never the same ones twice, in whichever files happened to be
    // scheduled together. Every one of them passed when its file was run alone.
    //
    // So the budget was wrong, not the tests. A generous timeout costs nothing on a passing run —
    // it is a ceiling, not a delay — and a genuinely hung test still fails, just later. Do NOT tune
    // this back down to make the suite "feel faster": it makes nothing faster, it only decides how
    // much CPU starvation gets reported as a test failure. CI runs this on a 2-core runner with no
    // retry, where the starvation is worse than on any laptop.
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
