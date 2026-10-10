import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
// From `vitest/config`, not `vite` — vite 8's own defineConfig rejects the `test` key
// (TS2769: 'test' does not exist in type 'UserConfigExport'). vitest re-exports a widened
// version that accepts it, which keeps test config colocated with the build config.
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: {
      output: {
        // Vendors that change rarely get their own long-cached chunks; every page is already its own
        // chunk via `React.lazy` in App.tsx. `codeSplitting` is rolldown's replacement for Rollup's
        // `manualChunks`, which it still accepts but has deprecated.
        codeSplitting: {
          groups: [
            { name: "react", test: /[\\/]node_modules[\\/](react|react-dom|scheduler|react-router)[\\/]/ },
            { name: "query", test: /[\\/]node_modules[\\/]@tanstack[\\/]/ },
            { name: "icons", test: /[\\/]node_modules[\\/]lucide-react[\\/]/ },
            {
              name: "markdown",
              test: /[\\/]node_modules[\\/](react-markdown|remark-[^\\/]+|micromark[^\\/]*|mdast-[^\\/]+|hast-[^\\/]+|unist-[^\\/]+|unified|vfile[^\\/]*)[\\/]/,
            },
          ],
        },
      },
    },
  },
  experimental: {
    // URLs INSIDE the bundle are relative; only index.html names `/assets/...` absolutely.
    //
    // Behind APP_BASE_PATH the server rewrites `="/assets/` in index.html and nothing else
    // (`shortlist/server/base_path.py::render_shell`). Lazy routes and the self-hosted fonts make
    // Vite write asset URLs into the JS (the dynamic-import preload list) and the CSS (`url()` for
    // each .woff2); left absolute, those would load from the proxy's root and the app would break
    // under a prefix as a blank page. Relative, they resolve against the file that names them, which
    // the rewritten shell already loaded from the right place. index.html itself must stay absolute:
    // it is served for every deep link (`/runs/12`), where a relative `assets/` would miss.
    renderBuiltUrl: (_filename, { hostType }) => (hostType === "html" ? undefined : { relative: true }),
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    proxy: {
      // Defaults to the port a deployed container uses, which is the common case: hack on the UI
      // against a real backend with real data. Point it somewhere else when the backend is also
      // changing, or when you don't want clicks landing on a live server:
      //   SHORTLIST_API_PROXY=http://localhost:5960 pnpm dev   (see scripts/devrun.sh)
      "/api": {
        target: process.env.SHORTLIST_API_PROXY ?? "http://localhost:5959",
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
