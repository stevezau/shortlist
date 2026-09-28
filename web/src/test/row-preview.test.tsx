/**
 * "What this row will do" (design §8): every setting the editor shows for the row's kind has exactly
 * one line, and nothing the editor hides gets one — held to the same kind→settings map the editor
 * uses, over the same fixtures as the editor's own kind tests.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RowPreview } from "@/components/rows/row-preview";
import { toInput } from "@/lib/collections";
import {
  hiddenButRead,
  NO_FACT_LINE,
  ROW_SETTING_KEYS,
  visibleSettings,
  type RowKindContext,
  type RowSettingKey,
} from "@/lib/row-kinds";
import type { CollectionInput, Settings } from "@/lib/types";
import { BYW_NAME, CTX, EVERY_OVERRIDE, FIXTURES, named, row } from "@/test/row-kind-fixtures";

/** A real server's globals, as `GET /settings` serves them. */
const SETTINGS = {
  "row.size": 15,
  "recommendations.watched_pct": 0.4,
  "recommendations.refresh_days": 8,
  "recommendations.idle_hold_days": 0,
  "recommendations.recency": 0.5,
  "recommendations.recent_count": 10,
  "recommendations.max_seeds": 30,
  "recommendations.rating_source": "tmdb",
  "recommendations.min_history": 10,
  "recommendations.cold_start": "popular",
  "candidates.sources": ["tmdb_similar", "tmdb_discover"],
  "requests.enabled": true,
  "requests.target": "arr",
  "requests.max_per_run": 5,
  "requests.auto_send": true,
} as unknown as Settings;

function renderPreview(
  input: CollectionInput,
  {
    ctx = CTX,
    settings = SETTINGS,
    enabled,
    rowNames,
  }: {
    ctx?: RowKindContext;
    settings?: Settings;
    enabled?: boolean;
    rowNames?: Record<string, string>;
  } = {},
) {
  render(
    <RowPreview
      input={input}
      ctx={ctx}
      enabled={enabled}
      rowNames={rowNames}
      users={[]}
      libraries={[]}
      settings={settings}
      seasons={[]}
    />,
  );
}

const factKeys = (): string[] =>
  [...document.querySelectorAll("[data-fact]")].flatMap((el) =>
    (el.getAttribute("data-fact") ?? "").split(" ").filter(Boolean),
  );

/** The line labelled `label`, whole: its label, value and the keys it carries. */
const line = (label: string) => screen.getByText(label, { selector: "dt" }).parentElement as HTMLElement;
const valueOf = (label: string) => line(label).querySelector("dd") as HTMLElement;

describe("every setting the editor shows has one line", () => {
  it.each(FIXTURES)("%s", (_, collection, ctx) => {
    const input = toInput(collection);
    renderPreview(input, { ctx });

    const expected = new Set<string>([...visibleSettings(input, ctx), ...hiddenButRead(input, ctx)]);
    const covered = factKeys();
    const exempt = Object.keys(NO_FACT_LINE);

    expect([...expected].filter((key) => !covered.includes(key) && !exempt.includes(key))).toEqual([]);
    expect(covered.filter((key) => !expected.has(key))).toEqual([]);
    // Exactly one line each, and none for a setting the Plex card already shows.
    expect(covered).toEqual([...new Set(covered)]);
    expect(covered.filter((key) => exempt.includes(key))).toEqual([]);
  });

  it("exercises every setting in one fixture or another, so no line escapes by never being asked for", () => {
    const seen = new Set<RowSettingKey>();
    for (const [, collection, ctx] of FIXTURES) {
      for (const key of visibleSettings(toInput(collection), ctx)) seen.add(key);
    }
    expect(ROW_SETTING_KEYS.filter((key) => !seen.has(key))).toEqual([]);
  });
});

describe("the lines that are easy to get wrong", () => {
  it("gives the default row's size from Settings, not the row's own column", () => {
    const collection = row({ slug: "picked", name: "✨ {library_name} Picked for You", size: 15 });
    renderPreview(toInput(collection), {
      ctx: { ...CTX, isDefault: true },
      settings: { ...SETTINGS, "row.size": 25 } as unknown as Settings,
    });

    expect(line("How many")).toHaveTextContent("Up to 25 titles (global default)");
    // The default row has no size control of its own, so the line carries no setting.
    expect(line("How many")).not.toHaveAttribute("data-fact");
  });

  it("gives any other row its own size", () => {
    renderPreview(toInput(row({ size: 12 })), {
      settings: { ...SETTINGS, "row.size": 25 } as unknown as Settings,
    });
    expect(valueOf("How many")).toHaveTextContent(/^Up to 12 titles$/);
    expect(line("How many")).toHaveAttribute("data-fact", "size");
  });

  it("says why a row that follows a watch changes every night, without a line for the hidden cadence", () => {
    renderPreview(toInput(row({ ...named(BYW_NAME), max_seeds: 2, refresh_days: 0 })));

    expect(valueOf("Changes")).toHaveTextContent(/^Every night — .*its name follows their latest watch$/);
    expect(line("Changes")).not.toHaveAttribute("data-fact");
    expect(factKeys()).not.toContain("refresh_days");
  });

  it("gives rotation as the reason when a row takes turns", () => {
    renderPreview(toInput(row({ ...named("Picks"), max_seeds: 2, seed_window: 3 })));
    expect(line("Changes")).toHaveTextContent(/it takes turns between their last 3 watches/);
    expect(line("Takes turns")).toHaveTextContent("Between their last 3 watches");
  });

  it("names a seasonal row's own list first, drops AI web search, and says only the season's titles stay", () => {
    renderPreview(toInput(row({ seasons: ["christmas"] })), {
      ctx: { ...CTX, globalSources: ["tmdb_similar", "llm_web"] },
    });

    const sources = line("Found via");
    expect(sources).toHaveTextContent(/^Found viaSeasonal list, TMDB similar/);
    expect(sources).not.toHaveTextContent(/AI web search/);
    expect(sources).toHaveTextContent(/Only titles on that season's list are kept/);
  });

  it("names every source in full on an ordinary row", () => {
    renderPreview(toInput(row({ candidate_sources: ["tmdb_similar", "trakt", "llm_web"] })));
    expect(line("Found via")).toHaveTextContent(/^Found viaTMDB similar, Trakt, AI web search$/);
  });

  it("says (global default) and the global's value on an inheriting setting, and neither on an override", () => {
    renderPreview(toInput(row({ watched_pct: null, refresh_days: null, idle_hold_days: 9 })));

    expect(line("Contents")).toHaveTextContent("Up to 40% things they've already seen (global default)");
    expect(line("Changes")).toHaveTextContent("Every 8 days (global default)");
    expect(line("Not watching")).toHaveTextContent(/^Not watchingWaits up to 9 days for them to watch something new$/);
  });

  it("says Picked for You's watch count, with the global it inherits", () => {
    renderPreview(toInput(row()));
    expect(line("Built from")).toHaveTextContent("Their last 30 watches, blended (global default)");
  });

  it("names the rating service beside Highest rated", () => {
    renderPreview(toInput(row({ pick_order: "rating" })), {
      settings: { ...SETTINGS, "recommendations.rating_source": "imdb" } as unknown as Settings,
    });
    expect(line("Order")).toHaveTextContent("Highest rated first, by IMDb score");
    expect(line("Order")).toHaveAttribute("data-fact", "pick_order rated_by");
  });

  it("says what someone new gets under a {top_seed} name, with the fallback name when there is one", () => {
    renderPreview(toInput(row({ ...named(BYW_NAME), max_seeds: 2, fallback_name: "✨ Picked for {user}" })));
    expect(line("Someone new")).toHaveTextContent("named “✨ Picked for {user}”");
    expect(line("Someone new")).toHaveAttribute("data-fact", "cold_start fallback_name");
  });

  it("says someone new gets no row when the fallback name is empty", () => {
    renderPreview(toInput(row({ ...named(BYW_NAME), max_seeds: 2 })));
    expect(line("Someone new")).toHaveTextContent(/No row until they've watched 10 titles/);
  });

  it.each([
    ["{top_seed}", "✨ {top_seed} for {user}"],
    ["{season}", "{season} picks for {user}"],
    ["{season_emoji}", "{season_emoji} Picked for {user}"],
  ])("says someone new gets no row when the fallback name uses %s, which the engine can't fill for them", (token, fallback) => {
    // `delivery.render_row_name` drops a fallback that itself needs a watch or a season.
    renderPreview(toInput(row({ ...named(BYW_NAME), max_seeds: 2, fallback_name: fallback })));
    expect(valueOf("Someone new")).toHaveTextContent(
      `No row until they've watched 10 titles: the name for someone new uses ${token}, which can't be filled in for them either`,
    );
    expect(valueOf("Someone new")).not.toHaveTextContent("named");
  });

  it("describes Watch it again's skips and what fills the rest", () => {
    renderPreview(toInput(row({ rewatch: true, watched_pct: 1, rewatch_cooldown_days: 45, max_seeds: 12 })));
    expect(line("Skips")).toHaveTextContent("Anything they finished in the last 45 days");
    expect(line("When they run out")).toHaveTextContent("New picks, matched to their last 12 watches");
  });

  it("says which requests a Your requests row shows, with its window and own tags", () => {
    renderPreview({ ...toInput(row()), requests_row: true, requests_window_days: 90, requests_tag_pattern: "req-{username}" });
    expect(valueOf("Which requests")).toHaveTextContent(/landed in the last 90 days, newest first/);
    expect(valueOf("Which requests")).toHaveTextContent(/req-\{username\}/);
    // No pick order to choose, so the line says what the engine does instead.
    expect(valueOf("Order")).toHaveTextContent("Newest arrival first, always");
    renderPreview({ ...toInput(row()), requests_row: true, requests_window_days: 0 });
    expect(screen.getAllByText("Which requests", { selector: "dt" })[1]?.parentElement).toHaveTextContent(
      /Everything they asked for/,
    );
  });

  it("counts a Popular row's watchers", () => {
    renderPreview(toInput(row({ build: "shared", min_watchers: 3 })));
    expect(line("Counts")).toHaveTextContent("Only titles at least 3 people here have watched");
  });

  it("gives the rebuild cadence with its time, and says Off means only by hand", () => {
    renderPreview(toInput(row({ schedule: "30 3 * * *" })));
    expect(line("Rebuilds")).toHaveTextContent("Every day at 3:30 AM");

    document.body.innerHTML = "";
    renderPreview(toInput(row({ schedule: "" })));
    expect(line("Rebuilds")).toHaveTextContent("Off — only when you run it by hand");
  });

  it("gives a sort prefix only when one is set", () => {
    renderPreview(toInput(row({ sort_title_prefix: "!010_" })));
    expect(line("Sort prefix")).toHaveTextContent("“!010_”");

    document.body.innerHTML = "";
    renderPreview(toInput(row({ sort_title_prefix: "" })));
    expect(screen.queryByText("Sort prefix", { selector: "dt" })).toBeNull();
  });
});

describe("the row's status", () => {
  it("says so when the row is switched off", () => {
    renderPreview(toInput(row()), { enabled: false });
    // Switching a row off removes its collections at save (`row_changes.py`, RECONCILE collection.disable),
    // not at the next run, and a run skips a row that is off.
    expect(line("Status")).toHaveTextContent(
      /^StatusOff — switching it off takes it off Plex straight away, and nothing is built until you turn it back on\.$/,
    );
    expect(line("Status")).toHaveAttribute("data-fact", "enabled");
  });

  it("adds no status line for a row that is on", () => {
    renderPreview(toInput(row()), { enabled: true });
    expect(screen.queryByText("Status", { selector: "dt" })).toBeNull();
  });
});

describe("requests, in one line", () => {
  it("gives the row's own limit, the send rule and its tag", () => {
    renderPreview(toInput(row(EVERY_OVERRIDE)));
    expect(line("Requests")).toHaveTextContent(
      /^RequestsUp to 4 a run, sent to Radarr\/Sonarr automatically, tagged “family”$/,
    );
  });

  it("gives the run's limit and the global send rule when the row inherits both", () => {
    renderPreview(toInput(row()), {
      settings: { ...SETTINGS, "requests.auto_send": false } as unknown as Settings,
    });
    expect(line("Requests")).toHaveTextContent(
      "Its share of the run's 5 (global default), held in Requests for your approval (global default)",
    );
  });

  it("says a row capped at 0 never asks on its own", () => {
    renderPreview(toInput(row({ req_max_per_row: 0 })));
    expect(line("Requests")).toHaveTextContent("Never on its own — its picks wait in Requests for you to approve");
  });

  it("names Overseerr and drops the tag it ignores", () => {
    renderPreview(toInput(row({ request_tag: "family" })), {
      settings: { ...SETTINGS, "requests.target": "overseerr" } as unknown as Settings,
    });
    expect(line("Requests")).toHaveTextContent(/sent to Overseerr automatically/);
    expect(line("Requests")).not.toHaveTextContent(/family/);
  });

  it("says requests are off when Settings turns them off", () => {
    renderPreview(toInput(row(EVERY_OVERRIDE)), {
      settings: { ...SETTINGS, "requests.enabled": false } as unknown as Settings,
    });
    expect(line("Requests")).toHaveTextContent(/^RequestsNone — requests are off in Settings$/);
    expect(line("Requests")).toHaveAttribute("data-fact", "requests");
  });

  it("says a Your requests row asks for nothing, in its own words", () => {
    // It is per-person, not shared, and the shared-row sentence explained the wrong thing: this row
    // never searches, so there is nothing missing for it to ask for.
    renderPreview({ ...toInput(row()), requests_row: true });
    expect(line("Requests")).toHaveTextContent(/^RequestsNone — this row only shows what they already asked for$/);
    expect(line("Requests")).not.toHaveAttribute("data-fact");
  });

  it("says a shared row asks for nothing, without claiming the hidden setting", () => {
    renderPreview(toInput(row({ build: "shared", request_tag: "family" })));
    expect(line("Requests")).toHaveTextContent(/^RequestsNone — shared rows never ask for missing titles$/);
    expect(line("Requests")).not.toHaveAttribute("data-fact");
  });
});

describe("the Shelf position line", () => {
  const anchored = (): CollectionInput => ({
    ...toInput(row({})),
    hub_anchor: { "1": { row: "because_you_watched_top_seed", before: false } },
  } as CollectionInput);

  it("names the row it sits next to, not that row's internal slug", () => {
    renderPreview(anchored(), {
      rowNames: { because_you_watched_top_seed: "🎯 Because you watched {top_seed}" },
    });

    expect(valueOf("Shelf position")).toHaveTextContent(
      "right after “🎯 Because you watched {top_seed}”",
    );
    expect(valueOf("Shelf position")).not.toHaveTextContent("because_you_watched_top_seed");
  });

  it("lets a long value wrap, so a phone-width page never scrolls sideways", () => {
    renderPreview(anchored());

    expect(valueOf("Shelf position").className).toContain("[overflow-wrap:anywhere]");
  });
});
