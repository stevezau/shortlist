import { useMutation } from "@tanstack/react-query";
import { Check, Plus, Search, X } from "lucide-react";
import { useState } from "react";

import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api";
import { useBlockSeed, useUnblockSeed, useUserHistory } from "@/lib/queries";
import { blockedSeeds } from "@/lib/types";
import type { BlockedSeed, TitleMatch, User } from "@/lib/types";

function seedLabel(seed: TitleMatch): string {
  if (!seed.title) return `TMDB ${seed.tmdb_id}`;
  const kind =
    seed.media_type === "show"
      ? "Show"
      : seed.media_type === "movie"
        ? "Movie"
        : "";
  const parts = [kind, seed.year ? String(seed.year) : ""].filter(Boolean);
  return parts.length ? `${seed.title} · ${parts.join(" · ")}` : seed.title;
}

/**
 * Their own recent watches, one tap to block.
 *
 * A blocked seed is nearly always a reaction to something they just watched — the film someone put on
 * for a friend, the sport, the kids' thing. Making the owner type that title into a TMDB search to
 * find a title Shortlist ALREADY KNOWS they watched is busywork, and worse, TMDB search can return a
 * different edition than the one in their library.
 *
 * Deliberately not the whole history: a wall of hundreds of titles is its own kind of useless. It
 * shows the most recent handful, already-blocked ones are marked rather than hidden (so a second
 * click isn't wasted, and you can see it worked), and the search below stays for anything older.
 */
function RecentWatchPicker({
  userId,
  blocked,
}: {
  userId: number;
  blocked: BlockedSeed[];
}) {
  const [expanded, setExpanded] = useState(false);
  const history = useUserHistory(userId);
  const block = useBlockSeed(userId);

  const alreadyBlocked = new Set(blocked.map((seed) => seed.tmdb_id));
  // A watch with no tmdb:// GUID has nothing a block can key on, so it is not offered at all.
  const candidates = (history.data ?? []).filter(
    (item) => item.tmdb_id !== null,
  );
  const shown = expanded ? candidates.slice(0, 24) : candidates.slice(0, 8);

  if (candidates.length === 0) return null;

  return (
    <div className="space-y-2">
      <p className="text-sm text-muted-foreground">
        Recently watched &mdash; tap one to stop it shaping their picks.
      </p>
      <div className="flex flex-wrap gap-2">
        {shown.map((item) => {
          const isBlocked = alreadyBlocked.has(item.tmdb_id as number);
          return (
            <Button
              key={`${item.tmdb_id}-${item.watched_at}`}
              type="button"
              size="sm"
              variant="outline"
              disabled={isBlocked || block.isPending}
              className={isBlocked ? "opacity-50" : undefined}
              title={isBlocked ? "Already blocked" : `Block ${item.title}`}
              onClick={() =>
                block.mutate({
                  tmdbId: item.tmdb_id as number,
                  title: item.title,
                  mediaType: item.media_type,
                  year: item.year ?? undefined,
                })
              }
            >
              {isBlocked ? (
                <Check className="h-3 w-3" aria-hidden />
              ) : (
                <Plus className="h-3 w-3" aria-hidden />
              )}
              <span className="max-w-[16rem] truncate">{item.title}</span>
              {item.year && (
                <span className="text-muted-foreground">{item.year}</span>
              )}
            </Button>
          );
        })}
      </div>
      {candidates.length > 8 && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs text-muted-foreground"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Show fewer" : `Show more (${candidates.length - 8})`}
        </Button>
      )}
    </div>
  );
}

/** Find a title on TMDB and block it, for a seed you want gone but can't reach from a trace page. */
function AddBlockedSeed({ userId }: { userId: number }) {
  const [query, setQuery] = useState("");
  const [mediaType, setMediaType] = useState<"movie" | "show">("movie");
  const [found, setFound] = useState<BlockedSeed[] | null>(null);
  const [error, setError] = useState("");
  const block = useBlockSeed(userId);

  const search = useMutation({
    mutationFn: () => api.searchTitles(query.trim(), mediaType),
    onSuccess: (results) => {
      // TMDB can answer without an id. There is nothing a block could key on, and POSTing null
      // would 422 — so those matches are simply not offered, the same rule the recent-watch picker
      // applies to a watch with no tmdb:// GUID.
      setFound(
        results.flatMap((match) =>
          match.tmdb_id === null ? [] : [{ ...match, tmdb_id: match.tmdb_id }],
        ),
      );
      setError("");
    },
    onError: (err) =>
      setError(
        apiErrorMessage(
          err,
          "Couldn’t search TMDB. Check the API key in Settings.",
        ),
      ),
  });

  return (
    <div className="space-y-2 rounded-md border border-dashed p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && query.trim()) {
              event.preventDefault();
              search.mutate();
            }
          }}
          placeholder="Search a title to block…"
          aria-label="Search a title to block"
          className="h-8 w-56"
        />
        <Segmented
          value={mediaType}
          onChange={setMediaType}
          ariaLabel="What kind of title"
          options={[
            { value: "movie", label: "Movie" },
            { value: "show", label: "TV" },
          ]}
        />
        <Button
          variant="secondary"
          size="sm"
          disabled={!query.trim() || search.isPending}
          onClick={() => search.mutate()}
        >
          <Search className="h-3.5 w-3.5" aria-hidden />
          Search
        </Button>
      </div>

      {error && (
        <p role="alert" className="text-sm text-destructive-text">
          {error}
        </p>
      )}

      {found !== null && found.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">
          TMDB has no {mediaType === "show" ? "show" : "film"} by that name.
        </p>
      )}

      {found?.map((result) => (
        <div
          key={result.tmdb_id}
          className="flex items-center justify-between gap-3 text-sm"
        >
          <span className="truncate">{seedLabel(result)}</span>
          <Button
            size="sm"
            variant="outline"
            disabled={block.isPending}
            onClick={() =>
              block.mutate(
                {
                  tmdbId: result.tmdb_id,
                  title: result.title,
                  mediaType: result.media_type,
                  year: result.year ?? undefined,
                },
                {
                  onSuccess: () => {
                    setFound(null);
                    setQuery("");
                  },
                },
              )
            }
          >
            <Plus className="h-3.5 w-3.5" aria-hidden />
            Block
          </Button>
        </div>
      ))}
    </div>
  );
}

/**
 * Titles that must never shape this person's recommendations.
 *
 * The list used to render bare TMDB ids — "tmdb 346648", a number nobody recognises — and there was
 * no way to add one at all: the API existed, the frontend wrapper existed, and nothing ever called
 * it. The empty state even told you to block titles from a trace page that had no such button.
 */
export function BlockedSeedsList({ user }: { user: User }) {
  const blocked = blockedSeeds(user.prefs);
  const unblock = useUnblockSeed(user.id);

  return (
    <div className="space-y-4">
      <RecentWatchPicker userId={user.id} blocked={blocked} />
      <AddBlockedSeed userId={user.id} />

      {blocked.length === 0 ? (
        <p className="text-sm text-muted-foreground/70">
          Nothing blocked. Pick one of their recent watches above, search for an
          older title, or use <strong>Don&rsquo;t seed</strong> on the
          &ldquo;How we picked&rdquo; page of any run &mdash; that page shows
          which of their watches drove each suggestion, so it is usually where
          an odd one gives itself away.
        </p>
      ) : (
        <ul className="space-y-1">
          {blocked.map((seed) => (
            <li
              key={seed.tmdb_id}
              className="flex items-center justify-between rounded-md border px-3 py-2 text-sm"
            >
              <span className="truncate">{seedLabel(seed)}</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => unblock.mutate(seed.tmdb_id)}
                title={`Let ${seed.title || "this title"} shape their picks again`}
                aria-label={`Unblock ${seed.title || `TMDB ${seed.tmdb_id}`}`}
              >
                <X className="h-3.5 w-3.5" aria-hidden="true" />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
