import { afterEach, describe, expect, it, vi } from "vitest";

import { readBasePath } from "@/lib/base-path";

describe("readBasePath", () => {
  it("is empty when the server injected nothing", () => {
    expect(readBasePath(undefined)).toBe("");
    expect(readBasePath({})).toBe("");
    expect(readBasePath({ __SHORTLIST_BASE_PATH__: "" })).toBe("");
    expect(readBasePath({ __SHORTLIST_BASE_PATH__: "/" })).toBe("");
  });

  it("normalises whatever the operator put in APP_BASE_PATH", () => {
    for (const raw of ["/shortlist", "shortlist", "/shortlist/", "  /shortlist  ", "/shortlist//"]) {
      expect(readBasePath({ __SHORTLIST_BASE_PATH__: raw })).toBe("/shortlist");
    }
  });

  it("ignores a non-string global", () => {
    expect(readBasePath({ __SHORTLIST_BASE_PATH__: 42 as unknown as string })).toBe("");
  });
});

describe("api base path", () => {
  afterEach(() => {
    delete window.__SHORTLIST_BASE_PATH__;
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  async function freshApiUrl(): Promise<(path: string) => string> {
    vi.resetModules();
    const mod = await import("@/lib/api");
    return mod.apiUrl;
  }

  it("stays same-origin root when nothing is injected", async () => {
    vi.stubEnv("VITE_API_BASE", undefined as unknown as string);
    const apiUrl = await freshApiUrl();
    expect(apiUrl("/api/runs")).toBe("/api/runs");
  });

  it("follows the injected prefix", async () => {
    window.__SHORTLIST_BASE_PATH__ = "/shortlist";
    vi.stubEnv("VITE_API_BASE", undefined as unknown as string);
    const apiUrl = await freshApiUrl();
    expect(apiUrl("/api/runs")).toBe("/shortlist/api/runs");
  });

  it("lets VITE_API_BASE win, for a split deployment", async () => {
    window.__SHORTLIST_BASE_PATH__ = "/shortlist";
    vi.stubEnv("VITE_API_BASE", "https://api.example.com");
    const apiUrl = await freshApiUrl();
    expect(apiUrl("/api/runs")).toBe("https://api.example.com/api/runs");
  });

  it("keeps an explicit empty VITE_API_BASE meaning same-origin root", async () => {
    window.__SHORTLIST_BASE_PATH__ = "/shortlist";
    vi.stubEnv("VITE_API_BASE", "");
    const apiUrl = await freshApiUrl();
    expect(apiUrl("/api/runs")).toBe("/api/runs");
  });
});
