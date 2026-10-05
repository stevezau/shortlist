import { afterEach, describe, expect, it, vi } from "vitest";

import { clearCachedReports, loadCachedReport, saveCachedReport } from "@/lib/report-cache";
import type { EffectivenessReport } from "@/lib/types";

const REPORT = {
  overall: { delivered: 1 },
  runs: { total: 1 },
  coverage: {},
  requests: {},
  watch_sync: {},
  trend: [],
  per_user: [],
  per_row: [],
  top_titles: [],
  recent: [],
} as unknown as EffectivenessReport;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("report cache", () => {
  it("round-trips a report under its window's key", () => {
    saveCachedReport("90", REPORT);

    expect(localStorage.getItem("shortlist.report.v1.90")).not.toBeNull();
    expect(loadCachedReport("90")).toEqual(REPORT);
    expect(loadCachedReport("7")).toBeUndefined();
  });

  it("reads as no cache when nothing is stored", () => {
    expect(loadCachedReport("30")).toBeUndefined();
  });

  it("reads as no cache when the value is corrupt JSON", () => {
    localStorage.setItem("shortlist.report.v1.30", "{not json");

    expect(loadCachedReport("30")).toBeUndefined();
  });

  it.each([
    ["a string", JSON.stringify("hi")],
    ["null", "null"],
    ["missing runs", JSON.stringify({ overall: {} })],
    ["missing overall", JSON.stringify({ runs: {} })],
    ["null fields", JSON.stringify({ overall: null, runs: null })],
    ["missing coverage", JSON.stringify({ ...REPORT, coverage: undefined })],
    ["missing requests", JSON.stringify({ ...REPORT, requests: undefined })],
    ["missing watch_sync", JSON.stringify({ ...REPORT, watch_sync: undefined })],
    ["trend not an array", JSON.stringify({ ...REPORT, trend: {} })],
    ["missing per_user", JSON.stringify({ ...REPORT, per_user: undefined })],
    ["missing per_row", JSON.stringify({ ...REPORT, per_row: undefined })],
    ["missing top_titles", JSON.stringify({ ...REPORT, top_titles: undefined })],
    ["missing recent", JSON.stringify({ ...REPORT, recent: undefined })],
  ])("reads as no cache when the shape is wrong: %s", (_name, raw) => {
    localStorage.setItem("shortlist.report.v1.30", raw);

    expect(loadCachedReport("30")).toBeUndefined();
  });

  it("does not throw when reading from storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });

    expect(loadCachedReport("30")).toBeUndefined();
  });

  it("does not throw when writing to storage throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });

    expect(() => saveCachedReport("30", REPORT)).not.toThrow();
  });

  it("ignores a copy stored under the unversioned key", () => {
    localStorage.setItem("shortlist.report.30", JSON.stringify(REPORT));

    expect(loadCachedReport("30")).toBeUndefined();
  });

  it("clearCachedReports removes every report key, any version, and nothing else", () => {
    saveCachedReport("30", REPORT);
    saveCachedReport("all", REPORT);
    localStorage.setItem("shortlist.report.30", "old");
    localStorage.setItem("shortlist.theme", "dark");

    clearCachedReports();

    expect(loadCachedReport("30")).toBeUndefined();
    expect(loadCachedReport("all")).toBeUndefined();
    expect(localStorage.getItem("shortlist.report.30")).toBeNull();
    expect(localStorage.getItem("shortlist.theme")).toBe("dark");
  });

  it("clearCachedReports does not throw when storage throws", () => {
    vi.spyOn(Storage.prototype, "key").mockImplementation(() => {
      throw new Error("blocked");
    });

    expect(() => clearCachedReports()).not.toThrow();
  });
});
