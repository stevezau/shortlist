import type { ReactNode } from "react";

import { renderRowName } from "@/lib/format";
import type { CollectionInput, User } from "@/lib/types";

/** How often the row swaps titles, in words, from the same 0..1 scale the engine reads.
 *
 * Mirrors `_refresh_period_days` in engine/rows.py: 1.0 is nightly, 0.0 never, and everything
 * between is `round(1 + (1 - f) * 13)` days. Said in days rather than as a percentage because
 * "50% fresh" tells nobody when their row will change.
 */
function updateFrequency(
  input: CollectionInput,
  followsAWatch: boolean,
  globalFreshness: number | null,
): string {
  if (followsAWatch) return "Every night";
  // An inheriting row is resolved against the real global, not reported as "the default" — the panel
  // exists to answer "what will this row do", and naming the setting instead of its effect is the
  // one answer it must not give. Only unresolvable while settings are still loading.
  const f = input.freshness ?? globalFreshness;
  if (f === null) return "Following the global default";
  if (f >= 1) return "Every night";
  if (f <= 0) return "Never — built once, then left alone";
  const days = Math.max(1, Math.round(1 + (1 - f) * 13));
  return days === 7 ? "About once a week" : `About every ${days} days`;
}

/** Where the row turns up, in the words someone would use about their own Plex. */
function whereItShows(input: CollectionInput): string {
  const names: Record<string, string> = {
    both: "Home and the library",
    home: "Home only",
    library: "The library only",
    off: "Nowhere — the Collections tab only",
  };
  const mine = names[input.placement] ?? "Home and the library";
  const theirs = names[input.placement_friends] ?? "Home and the library";
  if (input.build === "shared" || mine === theirs) return mine;
  return `You: ${mine.toLowerCase()} · Everyone else: ${theirs.toLowerCase()}`;
}

/** Who ends up with this row, counting only people a run will actually build for. */
function whoSeesIt(input: CollectionInput, users: User[]): string {
  const active = users.filter((u) => u.enabled && !u.prefs?.paused);
  const reach =
    input.audience === "everyone"
      ? active
      : active.filter((u) => input.audience_user_ids.includes(u.id));
  const count = reach.length ? ` (${reach.length})` : "";
  if (input.build === "shared") {
    return input.audience === "everyone"
      ? `One row, shared with everyone${count}`
      : `One row, shared with the people you picked${count}`;
  }
  return input.audience === "everyone"
    ? `Their own copy, for everyone${count}`
    : `Their own copy, for the people you picked${count}`;
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex gap-3 py-2 text-sm">
      <dt className="w-28 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 flex-1">{value}</dd>
    </div>
  );
}

/**
 * What the row being edited will actually produce, in plain words, updating as the form changes.
 *
 * The settings on the left are abstractions — a cadence, a seed budget, a placement enum. This is
 * the only place someone can check the thing they actually care about: "what will Sarah see on her
 * Home screen tonight?". Every fact here is derived from the CURRENT form state rather than the
 * saved row, so it answers that question before anything is written to Plex.
 *
 * Deliberately not a Plex mock-up. A fake shelf of grey boxes would imply we know which titles land
 * in it, and we do not until the row runs.
 */
export function RowPreview({
  input,
  users,
  followsAWatch,
  globalFreshness,
  warnings,
}: {
  input: CollectionInput;
  users: User[];
  followsAWatch: boolean;
  /** The server-wide freshness, so an inheriting row still shows a real cadence. */
  globalFreshness: number | null;
  /** Anything wrong with the row as configured. Shown here as well as inline, because this panel is
   *  what someone reads before saving. */
  warnings?: ReactNode;
}) {
  const template = input.name_template || input.name;
  // The sample library has to match the row's media type, or a TV-only row previews as
  // "More Movies to watch" — a name it can never produce, on the one panel whose job is to show the
  // name it WILL produce.
  const sampleLibrary = input.media === "show" ? "TV Shows" : "Movies";
  const shown =
    renderRowName(template, "Fargo", "Sarah", sampleLibrary) ||
    "Picked for You";
  // What actually varies depends on the row. A per-person row renders a different name for each
  // person, from their own viewing; a SHARED row is one collection everybody sees, so only
  // {library_name} moves — telling someone their shared row is named per person is simply untrue.
  const perPerson = /\{(top_seed|user)\}/.test(template);
  const perLibrary = template.includes("{library_name}");
  const caption =
    input.build === "shared"
      ? perLibrary
        ? "Example only — the real library name fills in, so each library gets its own."
        : null
      : perPerson
        ? "Example only — each person gets their own name here, from their own viewing."
        : perLibrary
          ? "Example only — the real library name fills in, so each library gets its own."
          : null;

  return (
    <div className="space-y-4 rounded-lg border bg-card p-5">
      <div>
        <h2 className="text-sm font-medium">What this row will do</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Updates as you change things. Nothing is saved until you press save.
        </p>
      </div>

      <div className="rounded-md border bg-muted/30 p-4">
        <p className="text-xs uppercase tracking-wide text-muted-foreground">
          On Plex it reads
        </p>
        <p className="mt-1 break-words text-base font-medium">“{shown}”</p>
        {caption && (
          <p className="mt-2 text-xs text-muted-foreground">{caption}</p>
        )}
      </div>

      <dl className="divide-y">
        <Fact label="Who gets it" value={whoSeesIt(input, users)} />
        <Fact
          label="How many"
          value={`Up to ${input.size} title${input.size === 1 ? "" : "s"}`}
        />
        <Fact
          label="Updates"
          value={updateFrequency(input, followsAWatch, globalFreshness)}
        />
        <Fact label="Appears on" value={whereItShows(input)} />
      </dl>

      {warnings}
    </div>
  );
}
