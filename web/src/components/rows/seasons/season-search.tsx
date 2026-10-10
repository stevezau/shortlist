import type { UseQueryResult } from "@tanstack/react-query";
import { Search, X } from "lucide-react";
import { useId, type ReactNode, type Ref } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";

/** The editor's searches answer nothing under this many characters (`api/seasons.py` `_MIN_QUERY`). */
const MIN_QUERY = 2;

/** One of the season editor's three searches: a labelled box, then what it found in all four states. */
export function SeasonSearch<T>({
  inputRef,
  label,
  placeholder,
  value,
  onValue,
  searched,
  search,
  empty,
  resultsLabel,
  children,
}: {
  /** The search box, so a picker can put focus back in it after an Add: the button that had it is
   *  disabled ("Added") or gone, and focus would otherwise fall out of the dialog. */
  inputRef: Ref<HTMLInputElement>;
  label: string;
  placeholder: string;
  value: string;
  onValue: (value: string) => void;
  /** The (debounced) text the results are for. */
  searched: string;
  search: UseQueryResult<T[]>;
  /** What to say when nothing matches `searched`. */
  empty: (searched: string) => string;
  resultsLabel: string;
  /** Each result, as an `<li>`. */
  children: (item: T) => ReactNode;
}) {
  const id = useId();
  const typed = value.trim();
  const query = searched.trim();

  let results: ReactNode = null;
  if (typed.length > 0 && typed.length < MIN_QUERY) {
    results = <p className="text-sm text-muted-foreground">Type at least {MIN_QUERY} letters to search.</p>;
  } else if (query.length >= MIN_QUERY) {
    if (search.isPending) {
      results = (
        <div className="space-y-1">
          <p className="sr-only">Searching…</p>
          <Skeleton aria-hidden="true" className="h-9 w-full" />
          <Skeleton aria-hidden="true" className="h-9 w-full" />
        </div>
      );
    } else if (search.isError) {
      results = (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/40 p-3 text-sm">
          <p>{apiErrorMessage(search.error, "The search didn’t answer. Try again in a moment.")}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void search.refetch()}>
            Retry
          </Button>
        </div>
      );
    } else if (search.data.length === 0) {
      results = <p className="rounded-md bg-muted/60 p-3 text-sm">{empty(query)}</p>;
    } else {
      results = (
        <ul aria-label={resultsLabel} className="max-h-64 divide-y overflow-y-auto rounded-md border">
          {search.data.map(children)}
        </ul>
      );
    }
  }

  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <div className="relative">
        <Search aria-hidden="true" className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          ref={inputRef}
          id={id}
          type="search"
          autoComplete="off"
          value={value}
          placeholder={placeholder}
          onChange={(event) => onValue(event.target.value)}
          className="pl-8"
        />
      </div>
      {results}
    </div>
  );
}

/** A result row: what it is on the left, Add on the right. */
export function SearchResultRow({
  children,
  addLabel,
  added,
  full,
  onAdd,
}: {
  children: ReactNode;
  /** The Add button's full name, e.g. "Add Free Birds (2013)". */
  addLabel: string;
  added: boolean;
  /** No room for another: the season's limit is reached. */
  full: boolean;
  onAdd: () => void;
}) {
  return (
    <li className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
      <span className="min-w-0 flex-1">{children}</span>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="shrink-0"
        aria-label={added ? undefined : addLabel}
        disabled={added || full}
        onClick={onAdd}
      >
        {added ? "Added" : "Add"}
      </Button>
    </li>
  );
}

/** A chosen source with its remove button. */
export function ChosenItem({
  children,
  removeLabel,
  onRemove,
}: {
  children: ReactNode;
  removeLabel: string;
  onRemove: () => void;
}) {
  return (
    <li className="flex items-start justify-between gap-2 rounded-md border bg-muted/30 py-1.5 pl-3 pr-1 text-sm">
      <span className="min-w-0 flex-1 py-0.5">{children}</span>
      <button
        type="button"
        aria-label={removeLabel}
        onClick={onRemove}
        // Drawn at 28px so the chip stays small; the ::after reaches 4px further each way, for a 36px target.
        className="relative inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground after:absolute after:-inset-1 after:content-[''] hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X aria-hidden="true" className="h-4 w-4" />
      </button>
    </li>
  );
}
