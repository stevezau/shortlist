/**
 * How Shortlist treats a title's original language when deciding what to ask Sonarr/Radarr for.
 *
 * The problem this exists for: the request pool is by definition what your library LACKS, and a
 * library that already holds the popular English titles is left with a missing pool that skews
 * non-English before any floor is applied — then the rating floor favours it further, because TMDB's
 * audience rates anime and K-drama generously.
 *
 * Shared by the global setting (Settings → Requests) and the per-row override (Row editor), so the
 * two can never drift into naming the same choice differently.
 *
 * Mirrors `LANGUAGE_MODES` and `OTHER_LANGUAGE_BAR_GAP` in `shortlist/engine/models.py`.
 */
export const LANGUAGE_MODES = ["any", "prefer", "only"] as const;

export type LanguageMode = (typeof LANGUAGE_MODES)[number];

/** A row stores `null` on any of the three to mean "inherit the global". */
export type RowLanguageMode = LanguageMode | null;

export const LANGUAGE_MODE_LABELS: Record<LanguageMode, string> = {
  any: "Any language",
  prefer: "Prefer these",
  only: "Only these",
};

/** What each mode actually does — a label names the setting, a hint names the consequence. */
export const LANGUAGE_MODE_HINTS: Record<LanguageMode, string> = {
  any: "Every language is judged on the same bars. This is how Shortlist has always worked.",
  prefer:
    "Titles in another language have to be rated higher before Shortlist asks for them on its own. Anything that misses that bar waits in your inbox instead, so you can still say yes.",
  only: "Shortlist will never ask for a title in another language.",
};

/**
 * How far above the owner's own minimum rating the other-language bar sits when they haven't set
 * one. Kept in step with `OTHER_LANGUAGE_BAR_GAP` in the engine — the UI only ever PREVIEWS this
 * number so the field can show what will be used; the engine is what actually applies it.
 */
export const OTHER_LANGUAGE_BAR_GAP = 1.5;

export function asLanguageMode(value: unknown): LanguageMode {
  return LANGUAGE_MODES.includes(value as LanguageMode)
    ? (value as LanguageMode)
    : "any";
}

/**
 * The bar a non-preferred title must clear, given the owner's own floor and whatever they typed.
 *
 * `null` means "follow the minimum rating", which is the shipped default — so the field shows a real
 * number rather than an empty box, and the number moves when they change their minimum rating.
 * Rounded because 6.1 + 1.5 is 7.6000000000000005 in binary floating point, and a settings field is
 * the last place anyone wants to see that.
 */
export function otherLanguageBar(
  minRating: number,
  explicit: number | null,
): number {
  if (explicit !== null) return explicit;
  return Math.round(Math.min(10, minRating + OTHER_LANGUAGE_BAR_GAP) * 10) / 10;
}

/**
 * A language code as people read it. Falls back to the raw code, uppercased, for anything the
 * browser can't name — an inbox chip reading "KO" is still useful, where an empty one is not.
 */
export function languageName(code: string): string {
  const cleaned = (code || "").trim().toLowerCase();
  if (!cleaned) return "";
  try {
    const names = new Intl.DisplayNames(undefined, { type: "language" });
    return names.of(cleaned) ?? cleaned.toUpperCase();
  } catch {
    return cleaned.toUpperCase();
  }
}

/**
 * The languages offered in the picker. Deliberately a short list of what Plex libraries actually
 * carry rather than all ~180 ISO 639-1 codes: the field accepts any two-letter code the API takes,
 * so this is a convenience, not a limit.
 */
export const COMMON_LANGUAGES: readonly string[] = [
  "en",
  "es",
  "fr",
  "de",
  "it",
  "pt",
  "nl",
  "sv",
  "da",
  "no",
  "fi",
  "pl",
  "ru",
  "uk",
  "tr",
  "ar",
  "he",
  "hi",
  "ta",
  "te",
  "th",
  "vi",
  "id",
  "ja",
  "ko",
  "zh",
];
