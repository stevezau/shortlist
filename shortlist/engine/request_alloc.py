"""Divide a run's request slots between the rows that want them.

``max_per_run`` is the run's ceiling and the thing that protects the library from ballooning, so it
is never divided *away* — rows compete for it. They split it evenly; a row that cannot fill its share
(because its own ``max_per_row`` binds, or because it simply has fewer qualifying titles) hands the
surplus back to the rows that can use it, repeatedly, until the ceiling is met or nothing is left.

Before this, one flat demand-ranked list decided everything, so whichever row happened to hold the
highest-demand titles took every slot — every night, since the ranking barely moves. A row could be
starved indefinitely with nothing anywhere saying so.
"""

from __future__ import annotations

from shortlist.engine.models import MediaType, MissingTitle

# One row's qualifying titles, already ranked best-first by its own gate.
RowTitles = tuple[str, list[MissingTitle]]


def allocate(
    per_row: list[RowTitles],
    *,
    cap: int,
    row_caps: dict[str, int],
) -> list[tuple[str, MissingTitle]]:
    """Claim at most ``cap`` titles across ``per_row``, evenly, and never the same title twice.

    Args:
        per_row: ``(row_slug, ranked titles)`` in RUN ORDER. The order is load-bearing twice: it
            breaks the even-split remainder, and it decides which row claims a title several rows
            want. Both are the owner's own row ordering, which they control by dragging rows.
        cap: The run ceiling (``max_per_run``).
        row_caps: ``row_slug -> max_per_row``. A missing key means "no row limit of its own".

    Returns:
        ``(row_slug, title)`` claims, at most ``cap`` of them, each title appearing exactly once.
        A title is claimed by the FIRST row in run order that offered it, so it consumes one slot in
        total rather than one per row that wanted it — N slots always yield N distinct titles.
    """
    budget = max(0, cap)
    if not per_row or budget == 0:
        return []

    # Keyed on (id, media_type), never the bare id: movie 550 and show 550 are different titles, the
    # same rule `filter_candidates` and the demand map follow.
    claimed: set[tuple[int, MediaType]] = set()
    # Which row OWNS each contested title, decided up front by run order rather than by whichever
    # row's per-round allowance happens to reach it first. Those are not the same thing: with
    # `main=[100, 9]` and `kids=[9]` at cap 2, main's single slot went to 100 and `kids` took 9 —
    # so a title the main row wanted was filed into the kids folder at the kids quality profile.
    # The docstring, `_dedupe_queued` and the shipped guide all promise the earlier row, and the
    # owner sees the result in Radarr, so the code is what moves.
    # ...and the fall-through matters: a row that cannot take anything (its own cap is 0, or it is
    # full) must not hold a title hostage, or a slot goes idle while a later row had it available.
    # "The first row in your row order whose settings it passes" — a row at its cap does not pass.
    offered_by: dict[tuple[int, MediaType], list[str]] = {}
    for slug, titles in per_row:
        for title in titles:
            offered_by.setdefault(_key(title), []).append(slug)
    claims: list[tuple[str, MissingTitle]] = []
    taken: dict[str, int] = {slug: 0 for slug, _ in per_row}
    # Each row's own remaining queue, consumed as titles are claimed or found already claimed.
    queues: dict[str, list[MissingTitle]] = {slug: list(titles) for slug, titles in per_row}
    order = [slug for slug, _ in per_row]

    while len(claims) < budget:
        live = [slug for slug in order if _can_take(slug, queues, taken, row_caps, claimed, offered_by)]
        if not live:
            break
        # Re-derived every round rather than computed once: a row that just ran dry returns its share
        # to whoever is still live, which is the redistribution this whole function exists for.
        remaining = budget - len(claims)
        share, extra = divmod(remaining, len(live))
        for position, slug in enumerate(live):
            # The remainder goes to the earlier rows, so a 10/3 split is a deterministic 4/3/3.
            allowance = share + (1 if position < extra else 0)
            if allowance == 0:
                continue
            _drain(slug, allowance, queues, taken, row_caps, claimed, offered_by, claims, budget)
            if len(claims) >= budget:
                break
    return claims


def _can_take(
    slug: str,
    queues: dict[str, list[MissingTitle]],
    taken: dict[str, int],
    row_caps: dict[str, int],
    claimed: set[tuple[int, MediaType]],
    offered_by: dict[tuple[int, MediaType], list[str]],
) -> bool:
    """Whether this row has both an unclaimed title left and room under its own cap."""
    cap = row_caps.get(slug)
    if cap is not None and taken[slug] >= cap:
        return False
    return any(_key(t) not in claimed and _owner(_key(t), offered_by, taken, row_caps) == slug for t in queues[slug])


def _drain(
    slug: str,
    allowance: int,
    queues: dict[str, list[MissingTitle]],
    taken: dict[str, int],
    row_caps: dict[str, int],
    claimed: set[tuple[int, MediaType]],
    offered_by: dict[tuple[int, MediaType], list[str]],
    claims: list[tuple[str, MissingTitle]],
    budget: int,
) -> None:
    """Take up to ``allowance`` titles for one row, skipping any an earlier row owns.

    Scans rather than pops, and that distinction is load-bearing: ownership is not fixed. A title
    belongs to the earliest row with ROOM, so when that row fills up the title falls to the next row
    that offered it — and discarding it on the way past would lose it for good. Only a title that is
    actually claimed, by anyone, leaves the queue.
    """
    row_cap = row_caps.get(slug)
    queue = queues[slug]
    index = 0
    while allowance > 0 and index < len(queue) and len(claims) < budget:
        if row_cap is not None and taken[slug] >= row_cap:
            return
        title = queue[index]
        key = _key(title)
        if key in claimed:
            queue.pop(index)  # gone for good — someone claimed it
            continue
        if _owner(key, offered_by, taken, row_caps) != slug:
            index += 1  # an earlier row owns it FOR NOW; keep it in case that changes
            continue
        queue.pop(index)
        claimed.add(key)
        claims.append((slug, title))
        taken[slug] += 1
        allowance -= 1


def _key(title: MissingTitle) -> tuple[int, MediaType]:
    return (title.tmdb_id, title.media_type)


def _owner(
    key: tuple[int, MediaType],
    offered_by: dict[tuple[int, MediaType], list[str]],
    taken: dict[str, int],
    row_caps: dict[str, int],
) -> str:
    """Which row gets this title: the earliest that offered it AND still has room under its own cap.

    Earliest-in-run-order is the contract three separate texts promise, and it is what decides which
    root folder and quality profile the title lands in. The room check is what keeps that from
    wasting a slot: a row capped at 0 offered the title but can never take it, so holding it there
    would leave the run short while a later row had it ready.
    """
    for slug in offered_by[key]:
        cap = row_caps.get(slug)
        if cap is None or taken[slug] < cap:
            return slug
    return offered_by[key][-1]  # nobody has room; the loop's cap checks will skip it anyway
