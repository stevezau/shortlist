"""Live DRY-RUN against the connected Plex — writes NOTHING.

Runs the full engine (real Plex + Tautulli + TMDB + LLM) with dry_run=True, so every Plex/plex.tv
write is logged as a would-be diff instead of performed. Prints, per user and per row, the pipeline
counts and the actual picks + reasons — so you can see whether the candidate sources produce good
titles from the real library. Also prints what it WOULD deliver/exclude/promote and would request.

Usage (on the plex host):  docker exec rowarr python /config/live_dryrun.py
(The scripts/ dir isn't in the image, so copy it into the mounted /config volume first — the
one-liner the assistant provides does exactly that.)
"""

from __future__ import annotations

from pathlib import Path

from shortlist.engine.pipeline import run as engine_run
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus

CONFIG = Path("/config")


def main() -> None:
    run_migrations(CONFIG)
    sessions = make_session_factory(make_engine(CONFIG))
    builder = ContextBuilder(sessions, SecretBox(CONFIG), EventBus())

    ctx = builder.build(dry_run=True)  # dry_run=True => no Plex/plex.tv writes happen, ever
    with sessions() as session:
        profiles = builder.enabled_profiles(session)

    print(f"\n=== DRY-RUN vs real Plex — {len(profiles)} enabled user(s), writes NOTHING ===")
    print(f"candidate sources: {ctx.config.candidate_sources}")
    print(f"curator: {type(ctx.curator).__name__}\n")

    report = engine_run(ctx, profiles)

    for u in report.users:
        c = u.counts
        print(f"--- {u.username} [{u.status}] ---")
        print(
            f"    history={c.history} seeds={c.seeds} candidates={c.candidates} "
            f"in_library={c.in_library} pre_ranked={c.pre_ranked} picks={len(u.picks)}"
        )
        if u.error:
            print(f"    ERROR: {u.error}")
        # Group the picks by the row that produced them.
        by_row: dict[str, list] = {}
        for p in u.picks:
            by_row.setdefault(p.collection_slug or "picked", []).append(p)
        for slug, picks in by_row.items():
            print(f"    row '{slug}':")
            for p in picks[:10]:
                because = f"  (because you watched {p.seed_title})" if p.seed_title else ""
                print(f"      {p.rank:>2}. {p.title}{because}")
                if p.reason:
                    print(f"          → {p.reason[:110]}")
        if u.diff:
            d = u.diff
            if d.added or d.removed:
                print(f"    WOULD add={d.added[:5]} removed={d.removed[:5]}")
        print()

    if report.requests and (report.requests.outcomes or report.requests.queued):
        print("=== requests (dry-run — nothing sent) ===")
        for o in report.requests.outcomes:
            print(f"    would {o.status}: {o.title} ({o.detail})")
        for m in report.requests.queued[:10]:
            print(f"    would queue for approval: {m.title} (wanted by {m.demand})")

    ok = sum(1 for u in report.users if u.status in ("ok", "cold_start"))
    print(f"\n=== {ok}/{len(report.users)} users produced picks — dry-run complete, no writes made ===")


if __name__ == "__main__":
    main()
