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
    expect(init.headers).not.toHaveProperty("x-shortlist-csrf");
  });

  it("asks the server to drop routine job kinds only when told to", async () => {
    // The param name is the whole contract: FastAPI ignores a query param it doesn't declare, so a
    // typo here silences nothing and reports no error — the header would just keep filling with the
    // 165-a-day reconciles this exists to keep out.
    // A fresh Response per call: a body can only be read once.
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse([])));

    await api.getJobs(undefined, 30, undefined, true);
    await api.getJobs(undefined, 30);

    const urls = fetchMock.mock.calls.map((call) => call[0] as string);
    expect(urls).toEqual([
      "/api/system/jobs?limit=30&exclude_routine=true",
      "/api/system/jobs?limit=30",
    ]);
  });

  it("sends the CSRF header on every mutation", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ run_id: 1 }));

    await api.startRun({});

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "x-shortlist-csrf": "1" });
  });

  it("sends PATCH bodies as JSON with the content-type and CSRF headers", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 3 }));

    await api.patchUser(3, { enabled: false, prefs: { paused: true } });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/users/3");
    expect(init.method).toBe("PATCH");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      "x-shortlist-csrf": "1",
    });
    expect(JSON.parse(init.body as string)).toEqual({
      enabled: false,
      prefs: { paused: true },
    });
  });

  it("wraps settings writes in a values envelope", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await api.putSettings({ "row.size": 15, "row.name_template": "✨ Picks" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/settings");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({
      values: { "row.size": 15, "row.name_template": "✨ Picks" },
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

    const error = await api.getRuns().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).message).toBe(
      "Could not reach the Shortlist server. Is it running?",
    );
  });

  it("prefixes requests with the configured base path, trimming trailing slashes", async () => {
    configureApiBase("/shortlist/");
    fetchMock.mockResolvedValue(jsonResponse([]));

    await api.getRuns();

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/shortlist/api/runs");
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

describe("api — a reverse proxy's own error page", () => {
  let fetchMock: Mock;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    configureApiBase("");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const APACHE_502 = `<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">
<html><head><title>502 Proxy Error</title></head><body><h1>Proxy Error</h1>
<p>The proxy server received an invalid response from an upstream server.</p>
<hr><address>Apache/2.4.66 (Ubuntu) Server at shortlist.example.com Port 443</address>
</body></html>`;

  it("never shows the proxy's HTML, which carries the server's hostname", async () => {
    // Seen for real: a 502 during a container restart rendered the Apache banner — including the
    // hostname — into the diagnostics panel. Someone screenshotting that into a public issue
    // publishes their hostname, which is the thing the whole report is careful about.
    fetchMock.mockResolvedValue(
      new Response(APACHE_502, {
        status: 502,
        headers: { "Content-Type": "text/html" },
      }),
    );

    await expect(api.getUsers()).rejects.toThrow(/didn't answer.*restarting/i);
    await expect(api.getUsers()).rejects.not.toThrow(/Apache/);
  });

  it("says what the person can do about it, not the status code alone", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html><body>504</body></html>", {
        status: 504,
        headers: { "Content-Type": "text/html" },
      }),
    );

    await expect(api.getUsers()).rejects.toThrow(/try again/i);
  });

  it("still surfaces a real JSON detail from our own API", async () => {
    // The HTML guard must not swallow the messages that ARE ours.
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Support mode is off." }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(api.getUsers()).rejects.toThrow("Support mode is off.");
  });
});
