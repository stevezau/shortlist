import { ArrowUpDown, SlidersHorizontal } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { useSearchParams } from "react-router";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary } from "@/components/query-boundary";
import {
  AcquisitionClaims,
  RejectedSection,
  RequestsEmptyState,
  SendingSummary,
  SentSection,
  WaitingSection,
} from "@/components/requests/request-sections";
import {
  ANY_LANGUAGE,
  FilterChip,
  FilterSelect,
  MAX_LOADED,
  PeopleFilter,
  RATING_OPTIONS,
  RequestTabs,
  RequestsOffBanner,
  RequestsSkeleton,
  SORT_OPTIONS,
  VOTES_OPTIONS,
  arrViewFor,
  languageOptions,
  peopleOn,
  sortRequests,
  type ArrView,
  type MediaFilter,
  type RequestSort,
  type RequestView,
} from "@/components/requests/request-parts";
import { Button } from "@/components/ui/button";
import { settingBool, settingString } from "@/lib/format";
import { languageName } from "@/lib/request-language";
import {
  useArrStatus,
  useClearRequests,
  useDeleteRequests,
  useRejectRequests,
  useRequests,
  useRestoreRequests,
  useSendRequests,
  useSettings,
  useUsers,
} from "@/lib/queries";
import type { RequestCandidate } from "@/lib/types";
import { displayNameLookup } from "@/lib/user-names";

export function RequestsPage() {
  const requestsQuery = useRequests();
  const settingsQuery = useSettings();
  const arrStatusQuery = useArrStatus();
  // `isPending` is the FIRST load only — a background refetch keeps the last answer on screen, so a
  // settled badge never flickers back to "Checking…" every time the poll comes round.
  const arrView = (item: RequestCandidate): ArrView =>
    arrViewFor(item, arrStatusQuery.data, arrStatusQuery.isPending);
  // `wanters` and `why[].user` are bare Plex usernames; the users list is what turns them into the
  // names the Users page shows. Nothing here waits on it — until it arrives (or if it fails) every
  // name resolves to itself, which is exactly what this page showed before.
  const usersQuery = useUsers();
  const send = useSendRequests();
  const reject = useRejectRequests();
  const del = useDeleteRequests();
  const restore = useRestoreRequests();
  const clear = useClearRequests();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Opens on Waiting, but a `?tab=sent` deep-link (e.g. the dashboard's "View the full send log")
  // lands straight on that view. `?tab=dismissed` is an accepted alias for the renamed Rejected tab.
  const [searchParams] = useSearchParams();
  const initialTab = searchParams.get("tab");
  const [view, setView] = useState<RequestView>(
    initialTab === "sent"
      ? "sent"
      : initialTab === "rejected" || initialTab === "dismissed"
        ? "rejected"
        : "waiting",
  );
  const [media, setMedia] = useState<MediaFilter>("all");
  const [sort, setSort] = useState<RequestSort>("recent");
  const [minRating, setMinRating] = useState("0");
  const [minVotes, setMinVotes] = useState("0");
  // An original-language code, "" for titles with none recorded, or ANY_LANGUAGE.
  const [language, setLanguage] = useState(ANY_LANGUAGE);
  // Whose requests to show. Empty = everyone's, so the page opens unfiltered.
  const [people, setPeople] = useState<Set<string>>(new Set());
  // Free text from the search: narrows the list to titles, or wanters, containing it.
  const [query, setQuery] = useState("");
  // The rating, vote and language filters live behind one Filters button rather than as always-on
  // dropdowns.
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filtersId = useId();
  const tabsId = useId();

  const togglePerson = (name: string) =>
    setPeople((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const clearFilters = () => {
    setMinRating("0");
    setMinVotes("0");
    setLanguage(ANY_LANGUAGE);
    setPeople(new Set());
    setQuery("");
  };

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const nameOf = useMemo(
    () => displayNameLookup(usersQuery.data),
    [usersQuery.data],
  );

  // `?? []` inline would be a fresh array every render, so both memos below would recompute on
  // every render (and eslint says so).
  const rows = useMemo(() => requestsQuery.data ?? [], [requestsQuery.data]);
  const pending = useMemo(
    () => rows.filter((r) => r.status === "pending"),
    [rows],
  );
  const sent = useMemo(() => rows.filter((r) => r.status === "sent"), [rows]);
  const rejected = useMemo(
    () => rows.filter((r) => r.status === "rejected"),
    [rows],
  );

  // Which tab is really on screen. Waiting and Sent are always offered; Rejected only exists once
  // something has been rejected, so a stale `?tab=rejected` — or the last rejected title being
  // allowed again while you look at it — falls back to Waiting rather than leaving the view blank.
  const active: RequestView =
    view === "rejected" && rejected.length === 0 ? "waiting" : view;
  const activeFull =
    active === "waiting" ? pending : active === "sent" ? sent : rejected;

  // Everyone on the server is offered; the counts beside them describe the tab you're on. Offering
  // only the names found on the page meant somebody whose requests were all older than the 500 this
  // page loads could never be picked — see `peopleOn`.
  const usernames = useMemo(
    () => (usersQuery.data ?? []).map((u) => u.username).filter(Boolean),
    [usersQuery.data],
  );
  const peopleOptions = useMemo(
    () => peopleOn(activeFull, nameOf, usernames),
    [activeFull, nameOf, usernames],
  );
  // A name whose titles have all been sent since you ticked it is no longer offered — filtering on
  // it would empty the list with no visible chip to un-tick. Dropping it is the same self-healing
  // the stale media filter does below.
  const activePeople = useMemo(() => {
    const offered = new Set(peopleOptions.map((p) => p.name));
    return new Set([...people].filter((name) => offered.has(name)));
  }, [people, peopleOptions]);

  // The names go to the SERVER, which applies them before its 500-row cap — so picking someone
  // reaches titles the read above never loaded, which is the whole point of the filter ("what does
  // this new person still need?"). With nobody picked this is the same query key as `requestsQuery`,
  // so the page still makes one request, not two.
  const filteredQuery = useRequests([...activePeople]);
  // What the lists on screen are built from. Until that answer lands — and if it fails — this stays
  // the loaded page, which `applyPeople` below narrows client-side exactly as it did before, so a
  // chip never blanks the list while it waits and never leaks someone else's titles into it.
  const listRows = filteredQuery.data ?? rows;

  const applyPeople = <T extends { wanters: string[] }>(list: T[]): T[] =>
    activePeople.size === 0
      ? list
      : list.filter((r) => (r.wanters ?? []).some((w) => activePeople.has(w)));

  // The media filter (Movies/Shows) narrows whichever list is on screen — and select-all/counts
  // follow what's visible, not the whole queue. It only applies to a list that actually mixes both
  // types: once a list is single-type its filter control is hidden, so a stale "Shows" (e.g. after
  // the shows were all sent) must fall back to "all" rather than strand the remaining movies.
  const applyMedia = <T extends { media_type: string }>(list: T[]): T[] => {
    if (media === "all") return list;
    const mixed =
      list.some((r) => r.media_type === "movie") &&
      list.some((r) => r.media_type === "show");
    return mixed ? list.filter((r) => r.media_type === media) : list;
  };
  // The rating and vote floors hide weaker titles; like the media filter they narrow what's on
  // screen (and so what select-all/counts act on).
  const ratingFloor = Number(minRating) || 0;
  const votesFloor = Number(minVotes) || 0;
  const applyThresholds = <T extends { rating: number; vote_count: number }>(
    list: T[],
  ): T[] =>
    list.filter((r) => r.rating >= ratingFloor && r.vote_count >= votesFloor);
  // Narrows the same way, to one original language. A title with none recorded is in no named
  // language, so it only shows under Any or Unknown.
  const applyLanguage = (list: RequestCandidate[]): RequestCandidate[] =>
    activeLanguage === ANY_LANGUAGE
      ? list
      : list.filter((r) => r.language === activeLanguage);
  // The search's free text. Matches the title, and the people who wanted it by username or by the name
  // the Users page shows — so "sarah" finds her titles before she is picked as a chip.
  const needle = query.trim().toLowerCase();
  const applyQuery = (list: RequestCandidate[]): RequestCandidate[] =>
    !needle
      ? list
      : list.filter(
          (r) =>
            r.title.toLowerCase().includes(needle) ||
            (r.wanters ?? []).some(
              (w) => w.toLowerCase().includes(needle) || nameOf(w).toLowerCase().includes(needle),
            ),
        );
  const narrow = (list: RequestCandidate[]) =>
    sortRequests(applyQuery(applyLanguage(applyThresholds(applyPeople(applyMedia(list))))), sort);
  // Built from the server's answer, not from `pending`/`sent`/`rejected` — those stay the loaded
  // page, because the tab counts and the "Wanted by" roster have to keep describing the whole inbox
  // rather than the slice a picked name narrowed it to.
  const pendingRows = listRows.filter((r) => r.status === "pending");
  const sentRows = listRows.filter((r) => r.status === "sent");
  const rejectedRows = listRows.filter((r) => r.status === "rejected");
  const activeShown =
    active === "waiting"
      ? pendingRows
      : active === "sent"
        ? sentRows
        : rejectedRows;
  // The languages offered describe the tab you're on: the loaded page, plus the server's answer once a
  // name is picked, which can reach titles the page never loaded. A language nothing on the tab is in
  // any more (its last title was just sent) is dropped rather than left filtering — the same
  // self-healing the people filter does.
  const languageChoices = languageOptions([...activeFull, ...activeShown]);
  const activeLanguage = languageChoices.some((o) => o.value === language)
    ? language
    : ANY_LANGUAGE;
  const pendingShown = narrow(pendingRows);
  // Only the tab on screen reads these two, so the others are not filtered and sorted every render.
  const sentShown = active === "sent" ? narrow(sentRows) : [];
  const rejectedShown = active === "rejected" ? narrow(rejectedRows) : [];

  // The count beside a PICKED name is re-read from the server's answer, which isn't capped to this
  // page — otherwise the chip could say "(12)" beside a list of forty of that person's titles.
  // Unpicked names keep the loaded page's count; nothing better exists until they're picked.
  const exactCounts = new Map(
    peopleOn(activeShown, nameOf, usernames).map((p) => [p.name, p.count]),
  );
  const peopleChips = peopleOptions.map((p) =>
    activePeople.has(p.name)
      ? { ...p, count: exactCounts.get(p.name) ?? p.count }
      : p,
  );
  // Whether anything is hiding titles right now — drives the "Clear filters" control and the
  // "nothing clears these filters" note. The media split is excluded on purpose: it only renders
  // when both types are present, so it can never be the reason a list is empty.
  const filtered =
    minRating !== "0" ||
    minVotes !== "0" ||
    activeLanguage !== ANY_LANGUAGE ||
    activePeople.size > 0 ||
    needle !== "";
  // How many of the Filters menu's own choices are set, for the count on its button.
  const menuFiltersSet =
    (minRating !== "0" ? 1 : 0) +
    (minVotes !== "0" ? 1 : 0) +
    (activeLanguage !== ANY_LANGUAGE ? 1 : 0);
  const languageLabel = activeLanguage ? languageName(activeLanguage) : "Unknown language";

  // Only visible pending rows are selectable, so an id lingering in the set after a send/reject or a
  // filter change is harmless, but scoping to what's shown keeps the count honest.
  const selectedPending = pendingShown
    .filter((r) => selected.has(r.id))
    .map((r) => r.id);
  const allChecked =
    pendingShown.length > 0 && selectedPending.length === pendingShown.length;
  const busy =
    send.isPending ||
    reject.isPending ||
    del.isPending ||
    restore.isPending ||
    clear.isPending;

  const toggleAll = () =>
    setSelected(
      allChecked ? new Set() : new Set(pendingShown.map((r) => r.id)),
    );

  const act = (mutate: () => void) => {
    mutate();
    setSelected(new Set());
  };

  // A per-row decision keeps the batch someone is assembling, but must still drop the title it just
  // decided. `useSendRequests.onSuccess` doesn't return its invalidation, so `busy` goes false when
  // the POST resolves — before the refetched list arrives. In that window the row is still listed as
  // pending, so leaving its id in `selected` lets the toolbar's Reject land on a title that was just
  // sent (unlike /delete, /reject doesn't exclude sent rows), stamping it rejected with a sent_at.
  const decide = (id: number, mutate: () => void) => {
    mutate();
    setSelected((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  };

  return (
    <div>
      <PageHeader
        title="Requests"
        subtitle="Titles your people want that aren’t in your library."
      />

      <SendingSummary />

      <AcquisitionClaims />

      {/* Whether requests are ON is a fact about the SETTING, never about whether the inbox happens
          to be empty — with the feature off and stale candidates on file, this page used to render
          the full inbox with a live Send button. Settings gets its own boundary so a cold load
          shows a skeleton rather than flashing "Requests are off" before the answer arrives. */}
      <QueryBoundary query={settingsQuery} skeleton={<RequestsSkeleton />}>
        {(settings) => {
          const requestsEnabled = settingBool(settings, "requests.enabled");
          // Whether the strongest picks go out on their own decides what the empty state can
          // promise: with this off, every qualifying title waits here instead (`requests.py`
          // queues them with the reason "auto-send is off").
          const autoSend = settingBool(settings, "requests.auto_send");
          const globalTag = settingString(settings, "requests.tag");
          // Which languages count as "the owner's". Read once here rather than per tile, so one bad
          // stored value cannot make every card render a chip it shouldn't.
          const rawLanguages = settings?.["requests.preferred_languages"];
          const preferredLanguages = Array.isArray(rawLanguages)
            ? rawLanguages
                .filter((c): c is string => typeof c === "string")
                .map((c) => c.trim().toLowerCase())
            : ["en"];
          // The chip explains a HOLD, so it only earns its place when a mode is actually holding
          // things back. On the default "any" server nothing is.
          const languageModeOn =
            settingString(settings, "requests.language_mode", "any") !== "any";
          // Read from the TARGET, not from whichever URLs happen to be filled in: an owner who tried
          // Radarr first and then switched still has its address saved, and passing it here would
          // deep-link Overseerr's sends into Radarr.
          const viaSeerr =
            settingString(settings, "requests.target", "arr") === "overseerr";
          const overseerrUrl = viaSeerr
            ? settingString(settings, "requests.overseerr.url")
            : "";
          const radarrUrl = viaSeerr
            ? ""
            : settingString(settings, "requests.radarr.url");
          const sonarrUrl = viaSeerr
            ? ""
            : settingString(settings, "requests.sonarr.url");
          return (
            <QueryBoundary
              query={requestsQuery}
              skeleton={<RequestsSkeleton />}
              isEmpty={(data) => data.length === 0}
              empty={
                <RequestsEmptyState requestsEnabled={requestsEnabled} autoSend={autoSend} />
              }
            >
              {() => {
                // Tabs, not a long stack: with a big queue the send log used to sit far below the
                // fold and read as missing. Waiting + Sent are always offered; Rejected appears
                // only once something's been rejected.
                const tabs: { value: RequestView; label: string; count: number }[] = [
                  { value: "waiting", label: "Waiting", count: pending.length },
                  { value: "sent", label: "Sent", count: sent.length },
                ];
                if (rejected.length > 0) {
                  tabs.push({ value: "rejected", label: "Rejected", count: rejected.length });
                }

                // The Movies/Shows split, scoped to the active tab's list — only offered when that list
                // actually mixes both types (splitting an all-movies queue helps no one).
                const movieCount = activeFull.filter(
                  (r) => r.media_type === "movie",
                ).length;
                const showCount = activeFull.length - movieCount;
                const showMediaFilter = movieCount > 0 && showCount > 0;

                // The page limit says one of two different things, and must not say the wrong one.
                // Unpicked, the read really is capped and some history is off the page. Once a name
                // is picked the server re-reads the whole history for those people BEFORE capping,
                // so the limit no longer describes what's on screen — unless their own titles fill
                // it. Until that answer arrives (or if it failed) the list is still the loaded page
                // narrowed here, so the unfiltered note stands.
                const namesServed =
                  activePeople.size > 0 && filteredQuery.data !== undefined;
                const picked =
                  activePeople.size === 1 ? "the name" : "the names";
                const capNote =
                  rows.length < MAX_LOADED
                    ? null
                    : !namesServed
                      ? `This page loads the first ${MAX_LOADED} titles — waiting ones first, then sent, then rejected — so some sent or rejected titles may not be on it at all. Pick a name under “Wanted by” to search every title on file for that person instead.`
                      : listRows.length >= MAX_LOADED
                        ? `Showing the first ${MAX_LOADED} titles for ${picked} you picked.`
                        : `Showing every title on file for ${picked} you picked — not just the ${MAX_LOADED} this page loads.`;

                return (
                  <div className="space-y-6">
                    {!requestsEnabled && <RequestsOffBanner />}

                    {/* Three layers, top to bottom: WHICH list (tabs), how to narrow it (one toolbar), and
                        what is narrowing it right now (removable chips). It used to be six stacked rows
                        before the first title — two sets of buttons, three dropdowns, a person search,
                        and two paragraphs, one of them repeating the page subtitle. */}
                    <div className="space-y-3">
                      <RequestTabs
                        idBase={tabsId}
                        value={active}
                        tabs={tabs}
                        // Switching tabs clears every refinement so a stale "Movies" (or a name
                        // nobody on this tab carries) can't hide the list with no visible control
                        // to reset it.
                        onChange={(next) => {
                          setView(next);
                          setMedia("all");
                          clearFilters();
                        }}
                      />
                      <div
                        role="tabpanel"
                        id={`${tabsId}-panel`}
                        aria-labelledby={`${tabsId}-${active}`}
                        className="space-y-3"
                      >
                      {(showMediaFilter || activeFull.length > 1) && (
                        <div className="flex flex-wrap items-center gap-2">
                          {showMediaFilter && (
                            <select aria-label="Filter by library" value={media} onChange={(event) => setMedia(event.target.value as MediaFilter)} className="h-9 rounded-md border bg-background px-3 text-xs"><option value="all">All types ({activeFull.length})</option><option value="movie">Movies ({movieCount})</option><option value="show">Shows ({showCount})</option></select>
                          )}
                          {activeFull.length > 1 && (
                            <>
                              <PeopleFilter
                                people={peopleChips}
                                selected={activePeople}
                                onToggle={togglePerson}
                                query={query}
                                onQuery={setQuery}
                                // One person wanting everything is no filter at all, so their name is
                                // not offered — the search still finds titles.
                                offerPeople={peopleOptions.length > 1}
                              />
                              <span className="relative inline-flex items-center">
                                <ArrowUpDown
                                  className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-muted-foreground"
                                  aria-hidden="true"
                                />
                                <select
                                  aria-label="Sort"
                                  value={sort}
                                  onChange={(e) => setSort(e.target.value as RequestSort)}
                                  className="h-9 rounded-md border bg-background pl-8 pr-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                >
                                  {SORT_OPTIONS.map((option) => (
                                    <option key={option.value} value={option.value}>
                                      {option.label}
                                    </option>
                                  ))}
                                </select>
                              </span>
                              <Button
                                variant="outline"
                                size="sm"
                                className="h-9 text-xs text-muted-foreground"
                                aria-expanded={filtersOpen}
                                aria-controls={filtersId}
                                onClick={() => setFiltersOpen((open) => !open)}
                              >
                                <SlidersHorizontal aria-hidden="true" />
                                Filters
                                {menuFiltersSet > 0 && (
                                  <span className="rounded-full bg-raised px-1.5 text-xs font-bold tabular-nums text-foreground">
                                    {menuFiltersSet}
                                  </span>
                                )}
                              </Button>
                            </>
                          )}
                        </div>
                      )}
                      {filtersOpen && activeFull.length > 1 && (
                        <div
                          id={filtersId}
                          className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border bg-card px-3 py-2"
                        >
                          <FilterSelect
                            label="Rating"
                            value={minRating}
                            onChange={setMinRating}
                            options={RATING_OPTIONS}
                          />
                          <FilterSelect
                            label="Votes"
                            value={minVotes}
                            onChange={setMinVotes}
                            options={VOTES_OPTIONS}
                          />
                          {/* One language is no choice at all: picking it would hide nothing. */}
                          {languageChoices.length > 1 && (
                            <FilterSelect
                              label="Language"
                              value={activeLanguage}
                              onChange={setLanguage}
                              options={[{ value: ANY_LANGUAGE, label: "Any" }, ...languageChoices]}
                            />
                          )}
                        </div>
                      )}
                      {/* Whenever ANYTHING is narrowing the list — even with one title left, when the
                          toolbar is no longer drawn. A search that emptied the list with no control
                          left to undo it pointed at a "Clear filters" that did not exist. */}
                      {filtered && (
                        <div className="flex flex-wrap items-center gap-2">
                          {needle && (
                            <FilterChip
                              label={`“${query.trim()}”`}
                              removeLabel="Clear the search"
                              onRemove={() => setQuery("")}
                            />
                          )}
                          {peopleChips
                            .filter((person) => activePeople.has(person.name))
                            .map((person) => (
                              <FilterChip
                                key={person.name}
                                label={person.count ? `${person.label} (${person.count})` : person.label}
                                removeLabel={`Stop filtering by ${person.label}`}
                                onRemove={() => togglePerson(person.name)}
                              />
                            ))}
                          {minRating !== "0" && (
                            <FilterChip
                              label={`Rating ${RATING_OPTIONS.find((o) => o.value === minRating)?.label ?? minRating}`}
                              removeLabel={`Remove the Rating ${RATING_OPTIONS.find((o) => o.value === minRating)?.label ?? minRating} filter`}
                              onRemove={() => setMinRating("0")}
                            />
                          )}
                          {minVotes !== "0" && (
                            <FilterChip
                              label={`Votes ${VOTES_OPTIONS.find((o) => o.value === minVotes)?.label ?? minVotes}`}
                              removeLabel={`Remove the Votes ${VOTES_OPTIONS.find((o) => o.value === minVotes)?.label ?? minVotes} filter`}
                              onRemove={() => setMinVotes("0")}
                            />
                          )}
                          {activeLanguage !== ANY_LANGUAGE && (
                            <FilterChip
                              label={languageLabel}
                              removeLabel={`Remove the ${activeLanguage ? languageName(activeLanguage) : "unknown"} language filter`}
                              onRemove={() => setLanguage(ANY_LANGUAGE)}
                            />
                          )}
                          <button
                            type="button"
                            onClick={clearFilters}
                            className="text-xs text-primary underline-offset-4 hover:underline focus-visible:underline"
                          >
                            Clear filters
                          </button>
                        </div>
                      )}
                      {/* Only when the cap is actually in play — otherwise it is a note about a
                          limit nobody has hit. Wording decided above. */}
                      {capNote && (
                        <p className="text-xs text-muted-foreground">
                          {capNote}
                        </p>
                      )}

                    {active === "waiting" && (
                      <WaitingSection
                        pending={pending}
                        pendingShown={pendingShown}
                        selected={selected}
                        selectedPending={selectedPending}
                        allChecked={allChecked}
                        requestsEnabled={requestsEnabled}
                        busy={busy}
                        viaSeerr={viaSeerr}
                        globalTag={globalTag}
                        preferredLanguages={preferredLanguages}
                        languageModeOn={languageModeOn}
                        arrView={arrView}
                        nameOf={nameOf}
                        send={send}
                        reject={reject}
                        del={del}
                        toggle={toggle}
                        toggleAll={toggleAll}
                        act={act}
                        decide={decide}
                        clearSelection={() => setSelected(new Set())}
                      />
                    )}

                    {active === "sent" && (
                      <SentSection
                        sent={sent}
                        sentShown={sentShown}
                        viaSeerr={viaSeerr}
                        radarrUrl={radarrUrl}
                        sonarrUrl={sonarrUrl}
                        overseerrUrl={overseerrUrl}
                        clear={clear}
                        busy={busy}
                        arrView={arrView}
                        nameOf={nameOf}
                      />
                    )}

                    {active === "rejected" && rejected.length > 0 && (
                      <RejectedSection
                        rejected={rejected}
                        rejectedShown={rejectedShown}
                        restore={restore}
                        busy={busy}
                        nameOf={nameOf}
                      />
                    )}
                      </div>
                    </div>
                  </div>
                );
              }}
            </QueryBoundary>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
