import { Search } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { searchSettings, type SettingsSearchEntry } from "@/components/settings/sections";
import { cn } from "@/lib/utils";

const MAX_RESULTS = 8;

/** Typing into a field must keep its "/"; only a bare page press means "search". */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/**
 * "Search settings…": finds a setting on any of the three tabs by its name or what it does, and
 * jumps to it. A setting that moved out of Settings (the disabled-users switch, now on Privacy)
 * still turns up, labelled as moved, and goes to where it lives now.
 */
export function SettingsSearch() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const listId = useId();
  const results = searchSettings(query).slice(0, MAX_RESULTS);
  const open = query.trim() !== "";

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return;
      event.preventDefault();
      input.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const go = (entry: SettingsSearchEntry) => {
    setQuery("");
    setHighlight(0);
    void navigate(entry.to);
  };

  return (
    <div className="relative w-full sm:w-72">
      <label className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3 text-sm focus-within:ring-2 focus-within:ring-ring">
        <Search className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <input
          ref={input}
          type="search"
          role="combobox"
          aria-label="Search settings on all three tabs"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={open && results[highlight] ? `${listId}-${highlight}` : undefined}
          placeholder="Search settings…"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setHighlight(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setQuery("");
            } else if (event.key === "ArrowDown" && results.length) {
              event.preventDefault();
              setHighlight((i) => (i + 1) % results.length);
            } else if (event.key === "ArrowUp" && results.length) {
              event.preventDefault();
              setHighlight((i) => (i - 1 + results.length) % results.length);
            } else if (event.key === "Enter" && results[highlight]) {
              event.preventDefault();
              go(results[highlight]);
            }
          }}
          className="min-w-0 flex-1 bg-transparent placeholder:text-muted-foreground focus-visible:outline-none [&::-webkit-search-cancel-button]:hidden"
        />
        <kbd aria-hidden="true" className="rounded border border-border-strong px-1.5 font-mono text-[11px] text-muted-foreground">
          /
        </kbd>
      </label>
      {open && (
        <div className="absolute right-0 top-full z-30 mt-1 w-full min-w-[16rem] overflow-hidden rounded-md border bg-popover shadow-lg">
          {results.length === 0 ? (
            <p role="status" className="px-3 py-2.5 text-sm text-muted-foreground">
              No setting matches &ldquo;{query.trim()}&rdquo;.
            </p>
          ) : (
            <ul id={listId} role="listbox" aria-label="Matching settings" className="max-h-80 overflow-y-auto py-1">
              {results.map((entry, index) => (
                <li
                  key={`${entry.to}-${entry.label}`}
                  id={`${listId}-${index}`}
                  role="option"
                  aria-selected={index === highlight}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setHighlight(index)}
                  onClick={() => go(entry)}
                  className={cn(
                    "flex cursor-pointer items-baseline justify-between gap-3 px-3 py-2 text-sm",
                    index === highlight ? "bg-raised text-foreground" : "text-foreground/90",
                  )}
                >
                  <span className="min-w-0 truncate">{entry.label}</span>
                  <span className={cn("shrink-0 text-xs", entry.moved ? "text-warning" : "text-muted-foreground")}>{entry.where}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
