import { useId, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { User } from "@/lib/types";

/** Search and paging change only the displayed people; selection stays with the caller. */
export function PeopleBrowser({ users, children, empty }: {
  users: User[];
  children: (visible: User[]) => ReactNode;
  empty?: ReactNode;
}) {
  const [query, setQuery] = useState("");
  const [pageSize, setPageSize] = useState("10");
  const [requestedPage, setPage] = useState(0);
  const searchId = useId();
  const search = useRef<HTMLInputElement>(null);
  const term = query.trim().toLocaleLowerCase();
  const matches = users.filter((user) =>
    [user.display_name, user.username].some((name) => name.toLocaleLowerCase().includes(term)),
  );
  const size = pageSize === "all" ? Math.max(1, matches.length) : Number(pageSize);
  const pages = Math.max(1, Math.ceil(matches.length / size));
  const page = Math.min(requestedPage, pages - 1);
  // A refreshed roster can remove the current page; keep it clamped even if the roster grows again.
  if (requestedPage !== page) setPage(page);
  const visible = matches.slice(page * size, (page + 1) * size);
  const clearSearch = () => {
    setQuery("");
    setPage(0);
    search.current?.focus();
  };

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex min-w-0 flex-wrap items-center gap-3">
        <label htmlFor={searchId} className="sr-only">Search people</label>
        <Input
          ref={search}
          id={searchId}
          type="search"
          placeholder="Search people…"
          className="min-w-0 basis-full sm:basis-48 sm:flex-1"
          value={query}
          onChange={(event) => { setQuery(event.target.value); setPage(0); }}
        />
        {query && matches.length > 0 && <Button type="button" variant="ghost" size="sm" onClick={clearSearch}>Clear search</Button>}
        <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
          Show
          <select
            aria-label="People per page"
            className="h-9 rounded-md border border-input bg-elevated px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={pageSize}
            onChange={(event) => { setPageSize(event.target.value); setPage(0); }}
          >
            <option value="10">10</option><option value="25">25</option><option value="50">50</option><option value="all">All</option>
          </select>
          per page
        </label>
      </div>
      {matches.length > 0 ? children(visible) : term ? (
        <div className="space-y-2 border-y py-6 text-center">
          <p className="text-sm font-medium">No people found</p>
          <p className="text-xs text-muted-foreground">Try another name or clear your search.</p>
          <Button type="button" variant="outline" size="sm" onClick={clearSearch}>Clear search</Button>
        </div>
      ) : empty ?? <p className="text-sm text-muted-foreground">No users yet — bring your Plex users in with “Sync users” on the Users page first.</p>}
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
        <p role="status" aria-live="polite">
          {matches.length > 0 ? `${page * size + 1}–${Math.min((page + 1) * size, matches.length)} of ${matches.length}` : "0"}
          {term ? " matching people" : " people"}
        </p>
        <div className="flex max-w-full flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="sm" aria-label="Previous page" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</Button>
          <span>Page {page + 1} of {pages}</span>
          <Button type="button" variant="outline" size="sm" aria-label="Next page" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next</Button>
        </div>
      </div>
    </div>
  );
}
