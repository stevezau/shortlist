"""Measure a ranking change against the real server's history, before trusting it.

Every dial in `EngineConfig` was chosen by reasoning. This is how one gets checked. It hides each
person's most recent watches one at a time, rebuilds the pool from only what they had watched before
each one, and reports where the hidden title would have landed under two configs.

    SHORTLIST_CONFIG=/config .venv/bin/python scripts/replay_eval.py

READ-ONLY. It imports nothing from `pipeline` beyond the pure eval module, calls no delivery, no
promotion and no privacy code, and touches no collection, label or share filter. The only writes
anywhere near it are TMDB's own response cache. Maintenance tooling, like
`recheck_pms_assumptions.py` — not shipped in the Docker image, not run by CI.

WHAT TO EDIT: the two `EngineConfig(...)` literals below. Deliberately not a CLI — this repo has no
CLI framework, and one built for this would be more code than the thing it configures.

HOW TO READ THE OUTPUT: the per-case table is the evidence. Scan it for names — a title that ranked
3rd and vanished is the finding, and it is legible in a way an aggregate is not. The summary at the
bottom is orientation, and it tells you when it is too small to mean anything.
"""

from __future__ import annotations

import os
from pathlib import Path

from shortlist.engine.eval.replay import Comparison, ReplayOutcome, holdout_cases, replay_case, summarise
from shortlist.engine.models import EngineConfig, MediaType
from shortlist.server.db.models import User
from shortlist.server.db.session import make_engine, make_session_factory
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_cache import WatchCache

# ── the experiment ────────────────────────────────────────────────────────────────────────────────
CONFIG_A = EngineConfig()
CONFIG_B = EngineConfig(recency=0.8)
LABEL_A = "baseline"
LABEL_B = "recency=0.8"
MAX_HOLDOUTS_PER_USER = 5
# ──────────────────────────────────────────────────────────────────────────────────────────────────


def _row(outcome_a: ReplayOutcome, outcome_b: ReplayOutcome) -> str:
    case = outcome_a.case

    def rank(outcome: ReplayOutcome) -> str:
        return str(outcome.rank) if outcome.rank else "—"

    delta = outcome_b.reciprocal_rank - outcome_a.reciprocal_rank
    note = ""
    if not outcome_a.gathered and not outcome_b.gathered:
        note = "not gathered"
    elif outcome_a.drop_reason or outcome_b.drop_reason:
        note = outcome_a.drop_reason or outcome_b.drop_reason
    elif delta < -0.01:
        note = "WORSE — check this"
    if not outcome_a.ratings_trusted:
        note = f"{note} (ratings not trusted)".strip()

    return (
        f"{case.user.username:<12}{case.held_out.title[:30]:<32}"
        f"{case.held_out.watched_at:%Y-%m-%d}  {rank(outcome_a):>6} {rank(outcome_b):>8}"
        f"  {delta:+.2f}  {note}"
    )


def main() -> None:
    config_dir = Path(os.environ["SHORTLIST_CONFIG"])
    engine = make_engine(config_dir)
    sessions = make_session_factory(engine)
    builder = ContextBuilder(sessions, SecretBox(config_dir), EventBus())
    # `dry_run=True` so nothing downstream could write even if this script grew a bug — the clients
    # are the real ones either way, and TMDB is the only network it needs.
    ctx = builder.build(dry_run=True)
    cache = WatchCache(sessions)

    # One read-only walk of the libraries, shared by every case. This is the same call the pipeline
    # makes to decide what is on the shelf; it never writes.
    library_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for section in ctx.plex.sections():
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        library_index[kind].update(ctx.plex.build_library_index(section))

    comparison = Comparison(LABEL_A, LABEL_B)
    print(f"Replay: {LABEL_A} vs {LABEL_B}\n")
    print(f"{'user':<12}{'title':<32}{'watched':<12}{'rank A':>6} {'rank B':>8}  {'Δrr':>5}  note")
    print("-" * 100)

    with sessions() as session:
        # `enabled_profiles` owns the eligibility rules — paused_all, parental profiles, the
        # restricted/profile pair that `privacy.py` matches exactly. Reused rather than re-queried,
        # so this script can never disagree with a real run about who is in scope. It returns
        # profiles, which carry no DB id, so the id comes from a lookup on the unique
        # `plex_account_id` rather than from a second, divergent eligibility query.
        user_ids = {u.plex_account_id: u.id for u in session.query(User).all()}
        for profile in builder.enabled_profiles(session):
            user_id = user_ids.get(profile.plex_account_id)
            if user_id is None:  # enabled but not yet synced — no cached history to replay
                continue
            history = cache.watched_set(session, user_id)
            for case in holdout_cases(profile, history, max_holdouts=MAX_HOLDOUTS_PER_USER):
                shared = {
                    "tmdb": ctx.tmdb,
                    "library_index": library_index,
                    # The cache stores `tmdb_id` on every row, so the identity resolver is the honest
                    # one here. The pipeline resolves via ratingKey only because it starts from a PMS
                    # read that has no tmdb_id yet.
                    "resolve_tmdb_id": lambda item: item.tmdb_id,
                    "run_year": case.held_out.watched_at.year,
                }
                outcome_a = replay_case(case, CONFIG_A, config_label=LABEL_A, **shared)
                outcome_b = replay_case(case, CONFIG_B, config_label=LABEL_B, **shared)
                comparison.outcomes_a.append(outcome_a)
                comparison.outcomes_b.append(outcome_b)
                print(_row(outcome_a, outcome_b))

    print()
    print(summarise(comparison))


if __name__ == "__main__":
    main()
