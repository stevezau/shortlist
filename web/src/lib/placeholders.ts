/**
 * The placeholders a row's name, description and poster text can carry. The SPA's copy of the token list
 * in `shortlist/engine/placeholders.py`: keep the two in step. The engine fills them in `delivery._fill`,
 * `delivery.render_description`, `rows._record_demand` and the report's `_RowNamer.label`.
 *
 * One list, because the tokens were typed out by hand in the name hint, the row card's chips, the
 * season helpers and two preview renderers, and a new placeholder had to find all of them.
 */

export const USER = "{user}";
export const LIBRARY_NAME = "{library_name}";
export const TOP_SEED = "{top_seed}";
export const SEASON = "{season}";
export const SEASON_EMOJI = "{season_emoji}";
export const THEME = "{theme}";
export const THEME_EMOJI = "{theme_emoji}";

/** Every placeholder, with what it stands for in the words the editor's hints use. `seasonal` ones only mean
 *  something on a row that follows seasons. */
export const PLACEHOLDERS = [
  { token: USER, meaning: "the person’s name", seasonal: false },
  { token: LIBRARY_NAME, meaning: "the Plex library the row lands in", seasonal: false },
  { token: TOP_SEED, meaning: "a title they recently watched", seasonal: false },
  { token: SEASON, meaning: "the season it’s in (Christmas)", seasonal: true },
  { token: SEASON_EMOJI, meaning: "that season’s emoji (🎄)", seasonal: true },
] as const;

/** The name placeholders only a seasonal row can fill — `SEASON_PLACEHOLDERS` in engine/placeholders.py. */
export const SEASON_TOKENS = [SEASON, SEASON_EMOJI] as const;

export function usesSeason(text: string): boolean {
  return SEASON_TOKENS.some((token) => text.includes(token));
}

/** An AI row's placeholders (#138): filled from the row's theme, and refused on any other row.
 *  Apart from `PLACEHOLDERS` so the list every row shows stays as it was. */
export const THEME_PLACEHOLDERS = [
  { token: THEME, meaning: "the theme’s name (Twist endings)" },
  { token: THEME_EMOJI, meaning: "that theme’s emoji (🌀)" },
] as const;

export function usesTheme(text: string): boolean {
  return THEME_PLACEHOLDERS.some(({ token }) => text.includes(token));
}

// EXACTLY the tokens the engine substitutes, matched case-sensitively — not `\{[a-z_]+\}`. A loose pattern
// would dress "Best of {genre}" up as a resolved placeholder while Plex receives the literal braces.
const NAMES = [...PLACEHOLDERS, ...THEME_PLACEHOLDERS].map((p) => p.token.slice(1, -1)).join("|");

/** Splits a name around its placeholders, keeping them (for `String.split`). */
export const PLACEHOLDER_SPLIT = new RegExp(`(\\{(?:${NAMES})\\})`);

/** Whether a piece of a split name IS a placeholder. */
export const PLACEHOLDER_EXACT = new RegExp(`^\\{(?:${NAMES})\\}$`);

/** The values a preview fills the placeholders with. */
export interface PlaceholderValues {
  topSeed: string;
  user: string;
  libraryName: string;
  season: { name: string; emoji: string };
  /** An AI row's theme; a sample stands in when a preview has none. */
  theme?: { name: string; emoji: string };
}

const SAMPLE_THEME = { name: "Twist endings", emoji: "🌀" };

/** Every placeholder filled, and nothing else changed: whitespace is the caller's business, since a one-line
 *  title collapses it and a description keeps its line breaks. */
export function fillPlaceholders(template: string, values: PlaceholderValues): string {
  const theme = values.theme ?? SAMPLE_THEME;
  return template
    .replaceAll(TOP_SEED, values.topSeed)
    .replaceAll(USER, values.user)
    .replaceAll(LIBRARY_NAME, values.libraryName)
    .replaceAll(SEASON_EMOJI, values.season.emoji)
    .replaceAll(SEASON, values.season.name)
    .replaceAll(THEME_EMOJI, theme.emoji)
    .replaceAll(THEME, theme.name);
}
