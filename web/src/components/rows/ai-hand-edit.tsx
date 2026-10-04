import { useId } from "react";
import { Link } from "react-router";

import { ChosenItem } from "@/components/rows/seasons/season-search";
import { SeasonPicksPicker } from "@/components/rows/seasons/season-picks-picker";
import { SeasonTagPicker } from "@/components/rows/seasons/season-tag-picker";
import { SELECT_CLASS } from "@/components/rows/seasons/select-class";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TMDB_MOVIE_GENRES } from "@/lib/season-draft";
import { CONNECTIONS_SETTINGS } from "@/lib/row-kinds";
import { selectsNothing, type PendingTheme } from "@/lib/themes";
import type { CollectionInput, Theme, ThemeRules } from "@/lib/types";

/** The API's limits on a theme's name and emoji (`ThemeIn`). */
const MAX_NAME = 60;
const MAX_EMOJI = 8;

function blankTheme(media: CollectionInput["media"]): Theme {
  return {
    id: null,
    slug: "",
    name: "",
    emoji: null,
    brief: "",
    origin: "manual",
    media: media === "both" ? ["movie", "show"] : [media],
    tags: [],
    genres: [],
    excluded_genres: [],
    collections: [],
    picks: [],
    rules: {},
    content_hash: "",
    ai_tokens: 0,
    stats: {},
  };
}

/** A number box's text as the value it stands for: blank is "no limit". */
function numberOrNull(text: string): number | null {
  if (text.trim() === "") return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

type RuleKey = "max_runtime" | "min_year" | "max_year" | "min_rating" | "min_votes";

/** A limit's number, or null for none: the response type is open, so the value is checked, not assumed. */
function ruleValue(rules: ThemeRules, key: RuleKey): number | null {
  const value = rules[key];
  return typeof value === "number" ? value : null;
}

const RULE_FIELDS: { key: RuleKey; label: string; step?: string }[] = [
  { key: "max_runtime", label: "Longest it can run (minutes)" },
  { key: "min_year", label: "Earliest release year" },
  { key: "max_year", label: "Latest release year" },
  { key: "min_rating", label: "Lowest rating (0 to 10)", step: "0.1" },
  { key: "min_votes", label: "Fewest votes on TMDB" },
];

/**
 * An AI row's list, written by hand: for a server with no AI provider, where "Build the list" is not on
 * offer. The same fields the AI fills — a name, TMDB tags, genres, limits and titles — saved with the row
 * and costing nothing.
 */
export function AiHandEdit({
  input,
  saved,
  pending,
  onPending,
}: {
  input: CollectionInput;
  saved: Theme | null;
  pending: PendingTheme | null;
  onPending: (next: PendingTheme) => void;
}) {
  const nameId = useId();
  const emojiId = useId();
  const genreId = useId();
  const draft = pending?.draft ?? saved ?? blankTheme(input.media);
  const update = (patch: Partial<Theme>) =>
    onPending({ draft: { ...draft, ...patch, origin: "manual" }, stats: null, origin: "manual" });

  return (
    <div className="space-y-5">
      <p className="rounded-md bg-muted/60 px-3 py-2 text-sm text-muted-foreground">
        No AI provider is set, so this list is built by hand. Add one in{" "}
        <Link to={CONNECTIONS_SETTINGS} className="underline underline-offset-2 hover:text-foreground">
          Settings › Connections
        </Link>{" "}
        and the AI can write it from a description instead.
      </p>

      <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_8rem]">
        <div className="space-y-2">
          <Label htmlFor={nameId}>Theme name</Label>
          <Input
            id={nameId}
            value={draft.name}
            maxLength={MAX_NAME}
            onChange={(event) => update({ name: event.target.value })}
            placeholder="e.g. Heist films"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={emojiId}>Emoji</Label>
          <Input
            id={emojiId}
            value={draft.emoji ?? ""}
            maxLength={MAX_EMOJI}
            onChange={(event) => update({ emoji: event.target.value || null })}
          />
        </div>
      </div>

      <SeasonTagPicker tags={draft.tags} onChange={(tags) => update({ tags })} perTag={undefined} counting={false} />

      <section className="space-y-3">
        <h4 className="text-sm font-semibold">Genres</h4>
        <div className="space-y-2">
          <Label htmlFor={genreId}>Add a genre</Label>
          <select
            id={genreId}
            className={SELECT_CLASS}
            value=""
            onChange={(event) => {
              const genre = event.target.value;
              if (genre && !draft.genres.includes(genre)) update({ genres: [...draft.genres, genre] });
            }}
          >
            <option value="">Choose a genre</option>
            {TMDB_MOVIE_GENRES.map((genre) => (
              <option key={genre.id} value={genre.name.toLowerCase()}>
                {genre.name}
              </option>
            ))}
          </select>
        </div>
        {draft.genres.length === 0 ? (
          <p className="text-sm text-muted-foreground">No genres yet.</p>
        ) : (
          <ul aria-label="Chosen genres" className="flex flex-wrap gap-1.5">
            {draft.genres.map((genre) => (
              <ChosenItem
                key={genre}
                removeLabel={`Remove genre ${genre}`}
                onRemove={() => update({ genres: draft.genres.filter((g) => g !== genre) })}
              >
                {TMDB_MOVIE_GENRES.find((g) => g.name.toLowerCase() === genre)?.name ?? genre}
              </ChosenItem>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-3">
        <h4 className="text-sm font-semibold">Limits</h4>
        <p className="text-sm text-muted-foreground">Leave a box empty for no limit.</p>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {RULE_FIELDS.map(({ key, label, step }) => (
            <RuleField
              key={key}
              label={label}
              step={step}
              value={ruleValue(draft.rules, key)}
              onChange={(value) => update({ rules: { ...draft.rules, [key]: value } })}
            />
          ))}
        </div>
      </section>

      <SeasonPicksPicker
        picks={draft.picks.map((pick) => ({
          tmdb_id: pick.tmdb_id,
          media_type: pick.media,
          title: pick.title,
          year: pick.year ?? null,
        }))}
        onChange={(picks) =>
          update({
            picks: picks.map((pick) => ({
              tmdb_id: pick.tmdb_id,
              media: pick.media_type,
              origin: "owner",
              reason: null,
              title: pick.title,
              year: pick.year ?? null,
            })),
          })
        }
      />

      {selectsNothing(draft) && (
        <p className="text-sm text-muted-foreground">
          Add at least one tag, genre or title, or the row would have nothing to pick from.
        </p>
      )}
    </div>
  );
}

function RuleField({
  label,
  step,
  value,
  onChange,
}: {
  label: string;
  step?: string;
  value: number | null;
  onChange: (value: number | null) => void;
}) {
  const id = useId();
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="number"
        min={0}
        step={step}
        value={value ?? ""}
        onChange={(event) => onChange(numberOrNull(event.target.value))}
      />
    </div>
  );
}
