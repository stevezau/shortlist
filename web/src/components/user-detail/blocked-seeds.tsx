import { useMutation } from "@tanstack/react-query";
import { Plus, Search, X } from "lucide-react";
import { useState } from "react";

import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api";
import { useBlockSeed, useUnblockSeed } from "@/lib/queries";
import { blockedSeeds } from "@/lib/types";
import type { BlockedSeed, User } from "@/lib/types";

function seedLabel(seed: BlockedSeed): string {
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
      setFound(results);
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
        <p role="alert" className="text-sm text-destructive">
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
    <div className="space-y-3">
      <AddBlockedSeed userId={user.id} />

      {blocked.length === 0 ? (
        <p className="text-sm text-muted-foreground/70">
          Nothing blocked. Search above, or use{" "}
          <strong>Don&rsquo;t seed</strong> on the &ldquo;How we picked&rdquo;
          page of any run — that&rsquo;s where a bad seed usually shows itself.
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
