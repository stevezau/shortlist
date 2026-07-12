import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";

import { api, ApiError, configureApiBase } from "@/lib/api";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("api", () => {
  let fetchMock: Mock;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    configureApiBase("");
  });

  it("returns parsed JSON on success", async () => {
    const users = [{ id: 1, username: "sarah" }];
    fetchMock.mockResolvedValue(jsonResponse(users));

    await expect(api.getUsers()).resolves.toEqual(users);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/users");
    expect(init.headers).toMatchObject({ Accept: "application/json" });
  });

  it("omits the CSRF header on GET requests", async () => {
    fetchMock.mockResolvedValue(jsonResponse([]));

    await api.getUsers();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.headers).not.toHaveProperty("x-rowarr-csrf");
  });

  it("sends the CSRF header on every mutation", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ run_id: 1 }));

    await api.startRun({});

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "x-rowarr-csrf": "1" });
  });

  it("sends PATCH bodies as JSON with the content-type and CSRF headers", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 3 }));

    await api.patchUser(3, { enabled: false, prefs: { row_size: 10 } });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/users/3");
    expect(init.method).toBe("PATCH");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      "x-rowarr-csrf": "1",
    });
    expect(JSON.parse(init.body as string)).toEqual({
      enabled: false,
      prefs: { row_size: 10 },
    });
  });

  it("wraps settings writes in a values envelope", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await api.putSettings({ "row.size": 15, "schedule.cron": "30 3 * * *" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/settings");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({
      values: { "row.size": 15, "schedule.cron": "30 3 * * *" },
    });
  });

  it("sends the literal confirm string and dry_run flag on uninstall", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        filters_restored: 3,
        collections_deleted: ["✨ Picked for You (sarah)"],
        dry_run: true,
        message: "Preview only — nothing was changed.",
      }),
    );

    await api.uninstall(true);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/system/uninstall");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      confirm: "UNINSTALL",
      dry_run: true,
    });
  });

  it("defaults the privacy check to the fast read-only pass", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ passed: true, tiers: {} }));

    await api.runPrivacyCheck();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/privacy/check");
    expect(JSON.parse(init.body as string)).toEqual({ probe: false });
  });

  it("runs the full probe when asked for it (the wizard's step 5)", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ passed: true, tiers: {} }));

    await api.runPrivacyCheck({ probe: true });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ probe: true });
  });

  it("normalizes FastAPI error bodies into ApiError with the detail message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "Plex token is invalid" }, { status: 401 }),
    );

    const error = await api.getUsers().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
    expect((error as ApiError).message).toBe("Plex token is invalid");
  });

  it("falls back to the status line for non-JSON error bodies", async () => {
    fetchMock.mockResolvedValue(
      new Response("", { status: 502, statusText: "Bad Gateway" }),
    );

    const error = await api.getRuns().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(502);
    expect((error as ApiError).message).toContain("502");
  });

  it("normalizes network failures into a status-0 ApiError with plain-English copy", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const error = await api.getHealth().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).message).toBe(
      "Could not reach the Rowarr server. Is it running?",
    );
  });

  it("prefixes requests with the configured base path, trimming trailing slashes", async () => {
    configureApiBase("/rowarr/");
    fetchMock.mockResolvedValue(jsonResponse([]));

    await api.getRuns();

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/rowarr/api/runs");
  });

  it("posts run requests with the selected users", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ run_id: 9 }));

    await expect(
      api.startRun({ user_ids: [4], dry_run: true }),
    ).resolves.toEqual({ run_id: 9 });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/runs");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      user_ids: [4],
      dry_run: true,
    });
  });
});
