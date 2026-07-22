"""Heuristic pre-rank. Also the full ranking in None-mode (no AI curator).

Two rules, both learned the hard way:

1. Seed provenance ADDS to a title's score, it does not multiply it. When the score was
   ``seed_frequency x rating x weight``, every candidate from a seedless source — tmdb_discover,
   llm_library, llm_web — scored exactly 0 and sorted below the worst seeded title on the list.
2. Rating is not similarity. Without `affinity` the only thing separating two single-seed
   candidates was TMDB's average vote — so a well-rated but unrelated show beat an obviously
   similar one, and a row seeded by a medical drama filled up with fantasy and sci-fi. Affinity
   carries "how near the top of TMDB's list for THIS seed was it", which is the actual claim.
3. Each source gets a fair SHARE of the pool handed to the curator. Ranking alone cannot fix this:
   30 seeds x TMDB suggestions is hundreds of seeded candidates, so a single global sort fills
   every slot with tmdb_similar however good the rest are, and the other sources — including the
   LLM calls we paid for — never reach the curator at all.
"""

from __future__ import annotations

from shortlist.engine.models import Candidate

_UNATTRIBUTED = "_unattributed"  # a hand-built candidate carrying no source tag


def score(candidate: Candidate) -> float:
    """How promising a candidate is, before the curator sees it.

    ``1 + seed_frequency`` (not ``seed_frequency``): "three of your seeds suggested this" is a real
    signal and should win ties, but a title nobody's history pointed at is not worthless — surfacing
    those is exactly what the "widen my taste" sources exist for.
    """
    seed_weight = max((s.weight for s in candidate.seeds), default=0.0)
    rating = candidate.rating or 5.0  # unrated titles get a neutral prior, not zero
    return (1 + candidate.seed_frequency) * rating * (1.0 + seed_weight) * candidate.affinity


def _sort_key(candidate: Candidate) -> tuple:
    return (-score(candidate), -candidate.rating, candidate.title)


def pre_rank(candidates: list[Candidate], keep: int) -> list[Candidate]:
    """Top `keep` candidates, giving every source a turn (best-first within each).

    Round-robin, not a global sort: each source offers its best remaining candidate in turn until
    `keep` is full. A source with few candidates runs out and the others take up the slack, so
    enabling a narrow source never costs you the breadth of a wide one — and a wide one can never
    shut a narrow one out.
    """
    ranked = sorted(candidates, key=_sort_key)
    if len(ranked) <= keep:
        return ranked

    queues: dict[str, list[Candidate]] = {}
    for candidate in ranked:  # already best-first, so each queue is too
        for source in candidate.sources or {_UNATTRIBUTED}:
            queues.setdefault(source, []).append(candidate)

    picked: list[Candidate] = []
    taken: set[tuple[int, object]] = set()
    while len(picked) < keep:
        progressed = False
        for source in sorted(queues):
            queue = queues[source]
            while queue:
                candidate = queue.pop(0)
                key = (candidate.tmdb_id, candidate.media_type)
                if key in taken:
                    continue  # another source already offered this exact title
                taken.add(key)
                picked.append(candidate)
                progressed = True
                break
            if len(picked) >= keep:
                break
        if not progressed:  # every queue is exhausted
            break
    return sorted(picked, key=_sort_key)
