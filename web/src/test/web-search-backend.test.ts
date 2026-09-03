import { describe, expect, it } from "vitest";

import {
  SOURCES,
  hasSearxng,
  hasWebSearch,
  sourceBlockedReason,
} from "@/lib/sources";
import type { Settings } from "@/lib/types";

/** Settings carrying only what a case is about — everything else stays unset. */
function settings(values: Record<string, string>): Settings {
  return values as unknown as Settings;
}

const LLM_WEB = SOURCES.find((s) => s.id === "llm_web")!;
const CLAUDE = { "curator.provider": "anthropic" }; // can search natively
const OLLAMA = { "curator.provider": "ollama" }; // cannot
const SEARX = { "searxng.url": "http://searx.local:8080" };
const EXA = { "exa.apikey": "key" };

describe("hasSearxng", () => {
  it("is true only once an address is on file", () => {
    expect(hasSearxng(settings({}))).toBe(false);
    expect(hasSearxng(settings(SEARX))).toBe(true);
  });
});

describe("hasWebSearch across the backend × configuration matrix", () => {
  it("lets a local model search once SearXNG is configured", () => {
    // The whole point of issue #78: Ollama has no native search, and this is the local way to give
    // it one — no paid vendor anywhere in the path.
    expect(
      hasWebSearch(
        settings({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" }),
      ),
    ).toBe(true);
  });

  it("blocks the searxng backend until an address is entered", () => {
    expect(
      hasWebSearch(
        settings({ ...OLLAMA, "llm_web.search_provider": "searxng" }),
      ),
    ).toBe(false);
  });

  it("does not let an Exa key satisfy the searxng backend", () => {
    // Explicit means explicit — the UI must not report "ready" via a backend the owner didn't pick.
    expect(
      hasWebSearch(
        settings({ ...OLLAMA, ...EXA, "llm_web.search_provider": "searxng" }),
      ),
    ).toBe(false);
  });

  it("does not let a SearXNG address satisfy the exa backend", () => {
    expect(
      hasWebSearch(
        settings({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "exa" }),
      ),
    ).toBe(false);
  });

  it("is satisfied by the chosen backend alone", () => {
    // `auto` (which accepted either) was removed in 1.3 — a backend is now always named.
    expect(
      hasWebSearch(
        settings({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" }),
      ),
    ).toBe(true);
    expect(
      hasWebSearch(
        settings({ ...OLLAMA, ...EXA, "llm_web.search_provider": "exa" }),
      ),
    ).toBe(true);
    expect(hasWebSearch(settings(OLLAMA))).toBe(false);
  });

  it("requires an AI provider for SearXNG, which only returns snippets", () => {
    // Named for SearXNG on purpose: this used to claim "whatever the backend", which stopped being
    // true when Exa began extracting titles itself.
    expect(
      hasWebSearch(
        settings({ ...SEARX, "llm_web.search_provider": "searxng" }),
      ),
    ).toBe(false);
  });

  it("requires NO AI provider for Exa, which extracts titles itself", () => {
    // The cell this matrix existed to pin and did not cover.
    expect(
      hasWebSearch(
        settings({
          "exa.apikey": "•••••",
          "llm_web.search_provider": "exa",
          "curator.provider": "none",
        }),
      ),
    ).toBe(true);
  });

  it("is satisfied by a native-capable provider on the default backend", () => {
    expect(hasWebSearch(settings(CLAUDE))).toBe(true);
  });
});

describe("sourceBlockedReason names the fix for the chosen backend", () => {
  it("points at the SearXNG address when that backend is chosen but unset", () => {
    const reason = sourceBlockedReason(
      LLM_WEB,
      settings({ ...OLLAMA, "llm_web.search_provider": "searxng" }),
    );
    expect(reason).toMatch(/SearXNG/);
  });

  it("mentions SearXNG as an option for a provider that cannot search itself", () => {
    // Before #78 this sentence offered Exa as the only way out for Ollama, which is exactly the
    // dead end a fully self-hosted owner hit.
    const reason = sourceBlockedReason(
      LLM_WEB,
      settings({ ...OLLAMA, "llm_web.search_provider": "native" }),
    );
    expect(reason).toMatch(/SearXNG/);
  });

  it("reports the missing AI provider ahead of any backend detail", () => {
    const reason = sourceBlockedReason(LLM_WEB, settings(SEARX));
    expect(reason).toMatch(/AI provider/);
  });

  it("is null once the source can actually run", () => {
    expect(
      sourceBlockedReason(
        LLM_WEB,
        settings({ ...OLLAMA, ...SEARX, "llm_web.search_provider": "searxng" }),
      ),
    ).toBeNull();
  });
});
