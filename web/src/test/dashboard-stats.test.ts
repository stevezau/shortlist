import { afterEach, describe, expect, it, vi } from "vitest";

import { dashboardStats } from "@/lib/dashboard-stats";
import type { Run, User } from "@/lib/types";

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1,
    username: "sarah",
    slug: "sarah",
    user_type: "shared",
    enabled: true,
    cold_start: false,
    history_depth: 100,
    last_run_at: null,
    hit_rate: null,
    ...overrides,
  };
}

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: 1,
    trigger: "manual",
    started_at: "2026-07-14T11:00:00Z",
    finished_at: "2026-07-14T11:05:00Z",
    status: "ok",
    dry_run: false,
    stats: { users_ok: 3, users_error: 0 },
    ...overrides,
  };
}

describe("dashboardStats", () => {
  afterEach(() => vi.useRealTimers());

  it("counts enabled users out of the total", () => {
    const stats = dashboardStats(
      [makeUser({ id: 1 }), makeUser({ id: 2, enabled: false })],
      [],
    );
    expect(stats.enabled).toBe(1);
    expect(stats.total).toBe(2);
  });

  it("averages only the measured hit rates", () => {
    const stats = dashboardStats(
      [
        makeUser({ id: 1, hit_rate: 0.2 }),
        makeUser({ id: 2, hit_rate: 0.4 }),
        makeUser({ id: 3, hit_rate: null }),
      ],
      [],
    );
    // (0.2 + 0.4) / 2 = 0.3 → "30%"; the unmeasured user is excluded, not counted as zero.
    expect(stats.hitRate).toBe("30%");
  });

  it("reports an em dash for hit rate before any measurement exists", () => {
    const stats = dashboardStats([makeUser({ hit_rate: null })], []);
    expect(stats.hitRate).toBe("—");
  });

  it("takes last-run fields from the newest run", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-14T12:00:00Z"));
    const stats = dashboardStats(
      [makeUser()],
      [
        makeRun({
          started_at: "2026-07-14T11:00:00Z",
          stats: { users_ok: 4, users_error: 2 },
        }),
      ],
    );
    expect(stats.lastRunAgo).toBe("1h ago");
    expect(stats.errors).toBe(2);
  });

  it("falls back to 'never'/null when there are no runs", () => {
    const stats = dashboardStats([makeUser()], []);
    expect(stats.lastRunAgo).toBe("never");
    expect(stats.errors).toBeNull();
  });
});
