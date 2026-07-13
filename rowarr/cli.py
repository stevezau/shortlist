"""Rowarr CLI — thin adapter over the engine; what the Phase-1 cron runs nightly.

Config lives in <config-dir>/config.yml (chmod 600 — it holds tokens):

    plex:
      url: http://plex.local:32400
      token: "..."
    tautulli:            # optional; Plex history API is the fallback
      url: http://tautulli.local:8181
      apikey: "..."
    tmdb:
      apikey: "..."
    curator:
      provider: anthropic   # anthropic | openai | google | ollama | none
      api_key: "..."
      # model: claude-haiku-4-5-20251001
    users: all              # or a list of usernames
    canary: home-canary     # optional Home user for `verify` T2
    row_size: 15
    row_name_template: "✨ Picked for You"
    schedule_note: nightly cron drives this; the CLI itself runs once and exits
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import click
import yaml
from loguru import logger

from rowarr.engine.clients.plex_pms import MIN_PMS_VERSION, PlexClient, parse_pms_version
from rowarr.engine.clients.plextv import PlexTvClient, PlexTvUser
from rowarr.engine.clients.tautulli import TautulliClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.curator import make_curator
from rowarr.engine.history import FallbackHistorySource, PlexHistorySource, TautulliSource
from rowarr.engine.models import EngineConfig, FilterSnapshot, MediaType, UserProfile, dedupe_slug, slugify
from rowarr.engine.pipeline import EngineContext
from rowarr.engine.pipeline import run as engine_run
from rowarr.engine.privacy import restore_user_restrictions
from rowarr.engine.verify import check_t1, check_t2
from rowarr.logging_config import configure_logging


class FileSnapshotStore:
    """One JSON file per user under <config-dir>/snapshots/ — uninstall restores from these."""

    def __init__(self, directory: Path):
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, plex_account_id: int) -> Path:
        return self._dir / f"{plex_account_id}.json"

    def get(self, plex_account_id: int) -> FilterSnapshot | None:
        path = self._path(plex_account_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return FilterSnapshot(
            plex_account_id=data["plex_account_id"],
            username=data["username"],
            taken_at=datetime.fromisoformat(data["taken_at"]),
            filters=data["filters"],
        )

    def save(self, snapshot: FilterSnapshot) -> None:
        payload = {**asdict(snapshot), "taken_at": snapshot.taken_at.isoformat()}
        self._path(snapshot.plex_account_id).write_text(json.dumps(payload, indent=2))

    def all(self) -> list[FilterSnapshot]:
        return [s for p in sorted(self._dir.glob("*.json")) if (s := self.get(int(p.stem)))]


class FileCache:
    """Naive JSON TTL cache for TMDB responses; good enough for a nightly CLI run."""

    def __init__(self, path: Path):
        self._path = path
        self._data = json.loads(path.read_text()) if path.exists() else {}

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry and entry["expires_at"] > time.time():
            return entry["value"]
        return None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        self._data[key] = {"value": value, "expires_at": time.time() + ttl_s}
        self._path.write_text(json.dumps(self._data))


def _load_recent_picks(path: Path) -> dict[str, set[tuple[int, MediaType]]]:
    """Staleness guard state, keyed on (tmdb_id, media_type).

    Files written before TV rows existed hold bare ints. Everything in them was a movie — that is
    why the leak happened — so reading them as movies is accurate, not a guess.
    """
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    recent: dict[str, set[tuple[int, MediaType]]] = {}
    for slug, entries in raw.items():
        recent[slug] = {
            (entry, MediaType.MOVIE) if isinstance(entry, int) else (entry[0], MediaType(entry[1])) for entry in entries
        }
    return recent


def roster_slugs(
    remote_users: list[PlexTvUser], path: Path, *, existing_rows: bool = False, reseed: bool = False
) -> dict[int, str]:
    """plex account id -> the slug Rowarr gave it. Assigned ONCE, never reassigned.

    The slug is what a user's row label is built from, so it has to belong to the ACCOUNT for as
    long as the account exists — the server keeps it in its users table, and this is the CLI's
    equivalent. Recomputing it from the live roster each run would let it move:

    * Plex display names are free text, so two of them can slugify to the same string. Whoever
      resolves the collision second gets `name_2` — and if that were decided fresh every run, a
      newly invited account with a LOWER plex id (ids come from account creation, not from when
      you invited them) would take the base slug, i.e. the incumbent's row and their private
      picks along with it.
    * When a colliding account leaves, the survivor would revert from `name_2` to `name` — and
      since `merge_label_excludes` only ever adds, their old label stays excluded on their own
      filter forever, so they permanently lose sight of their own row.

    A rename never moves the slug either, for the same reason.

    The map cannot be rebuilt from the server: a `rowarr_bob_smith` label does not say WHICH
    account owns it. So if it is missing while rows already exist, seeding from scratch would be a
    guess — and a wrong guess hands one person's row, and their private picks, to someone else. We
    stop instead, and say so.
    """
    if not path.exists() and existing_rows and not reseed:
        raise click.ClickException(
            f"{path} is missing but this server already has Rowarr rows. That file records which "
            "Plex account owns which row, and it cannot be reconstructed from the labels alone — "
            "re-deriving it could hand one user's row to another. Restore it from your /config "
            "backup, or pass --reseed to accept that rows may be reassigned."
        )
    stored = {int(k): v for k, v in json.loads(path.read_text()).items()} if path.exists() else {}
    taken = set(stored.values())
    for remote in sorted(remote_users, key=lambda u: u.id):  # stable regardless of response order
        if remote.id in stored:
            continue
        slug = dedupe_slug(slugify(remote.username), taken.__contains__)
        taken.add(slug)
        stored[remote.id] = slug
    # Atomically: a half-written map is a map that could reassign somebody's row.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({str(k): v for k, v in sorted(stored.items())}, indent=2))
    tmp.replace(path)
    return stored


def load_context(
    config_dir: Path, dry_run: bool, *, reseed: bool = False
) -> tuple[EngineContext, dict, list[PlexTvUser]]:
    config_path = config_dir / "config.yml"
    if not config_path.exists():
        raise click.ClickException(f"no config at {config_path} — create it first (see docs)")
    raw = yaml.safe_load(config_path.read_text())

    plex = PlexClient(raw["plex"]["url"], raw["plex"]["token"])
    plextv = PlexTvClient(raw["plex"]["token"], plex.machine_id)
    cache = FileCache(config_dir / "tmdb_cache.json")
    tmdb = TmdbClient(raw["tmdb"]["apikey"], cache=cache)

    if raw.get("tautulli", {}).get("url"):
        # Tautulli preferred, but only knows sessions it observed live — fall back per-user
        # to the PMS history API when Tautulli's answer is thin.
        tautulli = TautulliSource(TautulliClient(raw["tautulli"]["url"], raw["tautulli"]["apikey"]))
        history = FallbackHistorySource(tautulli, PlexHistorySource(plex))
    else:
        history = PlexHistorySource(plex)

    curator_cfg = dict(raw.get("curator") or {"provider": "none"})
    provider = curator_cfg.pop("provider", "none")
    curator = make_curator(provider, **curator_cfg)

    config = EngineConfig(
        row_size=int(raw.get("row_size", 15)),
        row_name_template=raw.get("row_name_template", "✨ Picked for You"),
        dry_run=dry_run,
    )
    recent_path = config_dir / "recent_picks.json"
    recent = _load_recent_picks(recent_path)
    remote_users = plextv.list_users()
    ctx = EngineContext(
        config=config,
        plex=plex,
        plextv=plextv,
        tmdb=tmdb,
        history_source=history,
        curator=curator,
        snapshots=FileSnapshotStore(config_dir / "snapshots"),
        recent_picks=recent,
        known_slugs=roster_slugs(
            remote_users,
            config_dir / "slugs.json",
            existing_rows=bool(plex.owned_collections()),
            reseed=reseed,
        ),
    )
    return ctx, raw, remote_users


def select_users(
    ctx: EngineContext, raw: dict, only: str | None, remote: list[PlexTvUser] | None = None
) -> list[UserProfile]:
    remote = remote if remote is not None else ctx.plextv.list_users()
    wanted = raw.get("users", "all")
    profiles = []
    overrides = raw.get("user_overrides") or {}
    for r in remote:
        # The slug comes from the context's map, never re-derived: it is what the row's label was
        # built from, and two of these usernames may slugify identically.
        profile = UserProfile(
            username=r.username, plex_account_id=r.id, user_type=r.user_type, slug=ctx.known_slugs.get(r.id, "")
        )
        if wanted != "all" and r.username not in wanted and profile.slug not in wanted:
            continue
        for key, value in (overrides.get(r.username) or overrides.get(profile.slug) or {}).items():
            if key == "excluded_genres":
                value = set(value)
            setattr(profile, key, value)
        profiles.append(profile)
    if only:
        profiles = [p for p in profiles if p.slug == only or p.username == only]
        if not profiles:
            raise click.ClickException(f"user {only!r} not found among enabled users")
    return profiles


PRIVACY_GATE_MAX_AGE_DAYS = 7  # design: weekly scheduled re-verification


def require_privacy_gate(config_dir: Path) -> None:
    """Refuse real writes without a recent passing Privacy Check (plex-safety rule 1).

    `rowarr verify` records its result to <config-dir>/privacy_check.json, including the
    PMS version it verified against. Dry runs are always allowed.
    """
    gate_path = config_dir / "privacy_check.json"
    if not gate_path.exists():
        raise click.ClickException(
            "no Privacy Check on record — run `rowarr verify` first (or use --dry-run). "
            "Rowarr never writes to Plex until your server has proven rows stay private."
        )
    gate = json.loads(gate_path.read_text())
    if not gate.get("passed"):
        raise click.ClickException("last Privacy Check FAILED — fix it and re-run `rowarr verify`")
    age = datetime.now(UTC) - datetime.fromisoformat(gate["ran_at"])
    if age.days > PRIVACY_GATE_MAX_AGE_DAYS:
        raise click.ClickException(
            f"last passing Privacy Check is {age.days} days old "
            f"(max {PRIVACY_GATE_MAX_AGE_DAYS}) — re-run `rowarr verify`"
        )
    version = tuple(gate.get("pms_version") or ())
    if version < MIN_PMS_VERSION:
        raise click.ClickException(
            f"PMS {'.'.join(map(str, version))} predates the label-restriction privacy fix "
            f"({'.'.join(map(str, MIN_PMS_VERSION))}) — upgrade Plex before running"
        )


@click.group()
@click.option("--config-dir", type=click.Path(path_type=Path), default=Path("/config"), show_default=True)
@click.option("--log-level", default="INFO", show_default=True)
@click.pass_context
def main(ctx: click.Context, config_dir: Path, log_level: str) -> None:
    """Rowarr — a private, AI-curated 'Picked for You' row for every user on your Plex server."""
    configure_logging(log_level, str(config_dir / "logs" / "rowarr.log"))
    ctx.obj = config_dir


@main.command("run")
@click.option("--user", "only", default=None, help="Run for a single user (slug or username).")
@click.option("--dry-run", is_flag=True, help="Log every would-be write instead of writing.")
@click.option(
    "--reseed",
    is_flag=True,
    help="Rebuild slugs.json from scratch. Only when it is lost AND you accept that rows may be "
    "reassigned between users — the labels alone cannot say which account owns which row.",
)
@click.pass_obj
def run_cmd(config_dir: Path, only: str | None, dry_run: bool, reseed: bool) -> None:
    """Run the nightly pipeline for all enabled users (or one user)."""
    ctx, raw, remote_users = load_context(config_dir, dry_run, reseed=reseed)
    if not dry_run:
        try:
            require_privacy_gate(config_dir)
        except click.ClickException:
            # Refused — so nothing gets BUILT. But everything that makes the server MORE private
            # still runs: rows Plex cannot hide are removed, and the excludes for every row that
            # exists are merged into every account's share filter. Neither can expose anything.
            #
            # Gating those would be a trap. A row nobody can hide, or an account missing an
            # exclude, FAILS the Privacy Check; the failed check closes the gate; and a closed
            # gate that blocked the remedy would stop the only thing that fixes it. The check
            # could never pass again, and the leak would be permanent.
            try:
                remedy = engine_run(ctx, [])
                for slug, titles in remedy.swept_rows.items():
                    click.echo(f"{slug:24} removed {len(titles)} row(s) Plex could not hide: {', '.join(titles)}")
                if remedy.filter_writes:
                    click.echo(f"{len(remedy.filter_writes)} share filter(s) updated so existing rows stay private")
            except Exception:
                # The gate refusal is what the operator needs to act on; a PMS blip in the remedy
                # must not replace it with a stack trace.
                logger.exception("the remedy pass failed while the privacy gate was closed")
            raise
    users = select_users(ctx, raw, only, remote_users)
    logger.info("running for {} user(s): {}", len(users), [u.slug for u in users])
    report = engine_run(ctx, users)

    if not dry_run:
        staleness = ctx.config.staleness_runs
        recent_path = config_dir / "recent_picks.json"
        history_path = config_dir / "picks_history.jsonl"
        with history_path.open("a") as fh:
            for user_report in report.users:
                ids = [[p.tmdb_id, p.media_type.value] for p in user_report.picks]
                previous = [[i, t.value] for i, t in ctx.recent_picks.get(user_report.slug, set())]
                merged = (previous + ids)[-staleness * ctx.config.row_size :]
                ctx.recent_picks[user_report.slug] = {(i, MediaType(t)) for i, t in merged}
                fh.write(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "user": user_report.slug,
                            "status": user_report.status,
                            "picks": [
                                {
                                    "tmdb_id": p.tmdb_id,
                                    "media_type": p.media_type.value,
                                    "title": p.title,
                                    "reason": p.reason,
                                }
                                for p in user_report.picks
                            ],
                        }
                    )
                    + "\n"
                )
        recent_path.write_text(
            json.dumps({slug: sorted([i, t.value] for i, t in picks) for slug, picks in ctx.recent_picks.items()})
        )

    if report.error:
        click.echo(f"RUN FAILED: {report.error}", err=True)
    for slug, titles in report.swept_rows.items():
        # Loud on purpose: this is us deleting someone's row, and the reason is a privacy fault.
        verb = "would remove" if dry_run else "removed"
        click.echo(f"{slug:24} {verb} {len(titles)} row(s) Plex could not hide: {', '.join(titles)}")
    for user_report in report.users:
        click.echo(
            f"{user_report.slug:24} {user_report.status:10} picks={user_report.counts.picks:3} "
            f"{'ERR ' + (user_report.error or '') if user_report.error else ''}"
        )
    sys.exit(0 if report.ok else 1)


@main.command("verify")
@click.option("--probe", is_flag=True, help="Full probe: throwaway collection + canary view check (~90s).")
@click.pass_obj
def verify_cmd(config_dir: Path, probe: bool) -> None:
    """Privacy verification: T1 filter read-back for all users, T2 canary view if configured.

    Records the result to privacy_check.json — `rowarr run` refuses real writes without a
    recent passing record.
    """
    ctx, raw, remote_users = load_context(config_dir, dry_run=True)
    users = select_users(ctx, raw, None, remote_users)
    if probe:
        canary_name = raw.get("canary")
        canary = next((u for u in users if u.username == canary_name or u.slug == canary_name), None)
        if canary is None:
            raise click.ClickException("--probe needs a `canary:` Home user in config.yml")
        from rowarr.engine.probe import run_privacy_probe

        result = run_privacy_probe(
            ctx.plex, ctx.plextv, canary, ctx.snapshots, on_step=lambda m: click.echo(f"  … {m}")
        )
        if result.passed:
            outcome = "PASS — " + str(result.detail.get("message", ""))
        else:
            outcome = "FAIL " + json.dumps(result.detail)
        click.echo(f"PROBE: {outcome}")
        (config_dir / "privacy_check.json").write_text(
            json.dumps(
                {
                    "ran_at": datetime.now(UTC).isoformat(),
                    "passed": result.passed,
                    "pms_version": list(parse_pms_version(ctx.plex.version)),
                    "tiers": {"PROBE": result.passed},
                },
                indent=2,
            )
        )
        sys.exit(0 if result.passed else 1)
    collections = ctx.plex.owned_collections("rowarr")
    stored = {slug: row.label for slug, row in collections.items()}  # real casing from the PMS
    t1 = check_t1(ctx.plextv, ctx.known_slugs, stored)
    click.echo(f"T1 filter read-back: {'PASS' if t1.passed else 'FAIL ' + json.dumps(t1.detail)}")
    ok = t1.passed
    t2 = None
    canary_name = raw.get("canary")
    if canary_name:
        canary = next((u for u in users if u.username == canary_name or u.slug == canary_name), None)
        if canary is None:
            raise click.ClickException(f"canary {canary_name!r} is not an enabled user")
        t2 = check_t2(ctx.plex, ctx.plextv, canary, collections)
        click.echo(f"T2 canary view ({canary.username}): {'PASS' if t2.passed else 'FAIL ' + json.dumps(t2.detail)}")
        ok = ok and t2.passed
    (config_dir / "privacy_check.json").write_text(
        json.dumps(
            {
                "ran_at": datetime.now(UTC).isoformat(),
                "passed": ok,
                "pms_version": list(parse_pms_version(ctx.plex.version)),
                "tiers": {"T1": t1.passed, **({"T2": t2.passed} if t2 else {})},
            },
            indent=2,
        )
    )
    sys.exit(0 if ok else 1)


@main.command("uninstall")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.option("--dry-run", is_flag=True, help="Show what would be restored/deleted.")
@click.pass_obj
def uninstall_cmd(config_dir: Path, yes: bool, dry_run: bool) -> None:
    """Restore every snapshot and delete every Rowarr collection — server as we found it."""
    ctx, _raw, _remote = load_context(config_dir, dry_run)
    snapshots = ctx.snapshots.all()
    click.echo(f"{len(snapshots)} filter snapshot(s) to restore; scanning for rowarr collections…")
    owned = []
    for section in ctx.plex.sections():
        for collection in section.collections():
            if any(label.tag.lower().startswith("rowarr_") for label in collection.labels):
                owned.append(collection)
    click.echo(f"{len(owned)} rowarr collection(s) to delete: {[c.title for c in owned]}")
    if not yes and not dry_run and not click.confirm("Proceed with full uninstall?"):
        raise SystemExit(1)
    for snapshot in snapshots:
        restore_user_restrictions(ctx.plextv, snapshot, dry_run=dry_run)
    for collection in owned:
        if dry_run:
            logger.info("[dry-run] would delete collection '{}'", collection.title)
        else:
            ctx.plex.delete_owned_collection(collection, "rowarr")
    click.echo("[dry-run] no changes made" if dry_run else "uninstall complete — server restored")


if __name__ == "__main__":
    main()
