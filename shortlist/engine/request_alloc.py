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
    claims: list[tuple[str, MissingTitle]] = []
    taken: dict[str, int] = {slug: 0 for slug, _ in per_row}
    # Each row's own remaining queue, consumed as titles are claimed or found already claimed.
    queues: dict[str, list[MissingTitle]] = {slug: list(titles) for slug, titles in per_row}
    order = [slug for slug, _ in per_row]

    while len(claims) < budget:
        live = [slug for slug in order if _can_take(slug, queues, taken, row_caps, claimed)]
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
            _drain(slug, allowance, queues, taken, row_caps, claimed, claims, budget)
            if len(claims) >= budget:
                break
    return claims


def _can_take(
    slug: str,
    queues: dict[str, list[MissingTitle]],
    taken: dict[str, int],
    row_caps: dict[str, int],
    claimed: set[tuple[int, MediaType]],
) -> bool:
    """Whether this row has both an unclaimed title left and room under its own cap."""
    if taken[slug] >= row_caps.get(slug, len(queues[slug]) + taken[slug]):
        return False
    return any(_key(title) not in claimed for title in queues[slug])


def _drain(
    slug: str,
    allowance: int,
    queues: dict[str, list[MissingTitle]],
    taken: dict[str, int],
    row_caps: dict[str, int],
    claimed: set[tuple[int, MediaType]],
    claims: list[tuple[str, MissingTitle]],
    budget: int,
) -> None:
    """Take up to ``allowance`` titles for one row, skipping any an earlier row already claimed."""
    row_cap = row_caps.get(slug)
    while allowance > 0 and queues[slug] and len(claims) < budget:
        if row_cap is not None and taken[slug] >= row_cap:
            return
        title = queues[slug].pop(0)
        key = _key(title)
        if key in claimed:
            continue  # an earlier row got it; this row's slot is still free for its next pick
        claimed.add(key)
        claims.append((slug, title))
        taken[slug] += 1
        allowance -= 1


def _key(title: MissingTitle) -> tuple[int, MediaType]:
    return (title.tmdb_id, title.media_type)
