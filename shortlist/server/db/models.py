"""SQLAlchemy models — schema v1 per the architecture doc §3."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None) -> str | None:
    """Serialize a DB datetime with an explicit UTC offset.

    SQLite hands timezone-aware columns back as naive datetimes; without re-attaching UTC,
    `isoformat()` has no offset and browsers parse it as local time — shifting the audit
    trail by the viewer's UTC offset.
    """
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


# The seeded "Picked for You" row (migration 0003). It is the one row whose name, size and curation
# style come from the global Settings rather than its own columns, so that the wizard and Settings
# stay the single place to change them. Every module that special-cases it uses this constant.
DEFAULT_SLUG = "picked"


class Base(DeclarativeBase):
    type_annotation_map: ClassVar = {dict: JSON, list: JSON}


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Server(Base):
    __tablename__ = "server"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(String(512))
    token_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted; never stored in the clear
    version: Mapped[str] = mapped_column(String(64), default="")
    owner_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plex_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)


class User(Base):
    """A person on the server.

    **FK cascade policy for everything keyed to a user.** Five tables reference `users.id`, and they
    split on one question: *is this row regenerable from Plex?*

    * ``ondelete="CASCADE"`` — `watched_titles` and `watch_sync_state`. Both are a CACHE of what the
      PMS already knows; their own docstrings say so. Losing them costs one full re-read, nothing
      more, so they follow the person out.
    * ``ondelete="RESTRICT"`` — `picks`, `run_users` and `restriction_snapshots`. Each is the ONLY
      copy of something: the impact ledger behind every lifetime dashboard metric, the run history,
      and — the one that matters most — `restriction_snapshots`, which holds a person's share filters
      *as they were before Shortlist touched them* and is what uninstall restores from (plex-safety
      rule 2). Cascading those away would silently destroy an irreplaceable record.

    So a `DELETE FROM users` fails loudly today, and that is the designed outcome: nothing in the
    codebase deletes a user (they are disabled instead), and the first code that wants to must state,
    per table, what happens to the three records that cannot be rebuilt.

    **The one sanctioned exception**, stating it as that rule requires: `DELETE /api/users/{id}`
    ("Remove", for someone plex.tv no longer lists) deletes `picks` and `run_users` and KEEPS
    `restriction_snapshots`. It does not delete the users row at all — it sets `removed_at` — precisely
    so the snapshot keeps its anchor, since uninstall skips any snapshot whose user has gone. The cost
    of the two it does drop is real and deliberate: lifetime dashboard figures change retroactively for
    the whole server, and past run pages lose a user that `runs.stats` still counts. The app's engine sets
    `PRAGMA foreign_keys=ON` (`db/session.py`), so both halves of this policy are actually enforced.

    RESTRICT is spelled out rather than left to SQLite's default NO ACTION, which behaves the same
    here but says nothing: the policy above lived only in this docstring until 0055 put it in the
    schema, and a comment is not something a `DELETE` can trip over.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plex_account_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    avatar_url: Mapped[str] = mapped_column(String(512), default="")
    # What to call this person in a row title. `nickname` is the owner's own override and always
    # wins; `friendly_name` is whatever Tautulli knows them as, refreshed on each user sync. Neither
    # touches `slug`, so the `shortlist_<slug>` label every share filter excludes never moves —
    # renaming someone is cosmetic by construction and can't strand their privacy exclusions.
    nickname: Mapped[str] = mapped_column(String(255), default="")
    friendly_name: Mapped[str] = mapped_column(String(255), default="")
    user_type: Mapped[str] = mapped_column(String(16), default="shared")  # shared | managed | owner
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    # The Plex parental PRESET on a managed account — "little_kid" | "older_kid" | "teen", "" for none.
    # `restricted` alone cannot tell a parental-controlled Home user from a plain one (plex.tv sets it
    # for both), and the difference decides whether Plex will accept a label restriction at all.
    restriction_profile: Mapped[str] = mapped_column(String(32), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # May Shortlist edit this person's Plex sharing settings? A separate axis from `enabled`, not a
    # third value of it: `enabled` decides whether they get a ROW, this decides whether we touch THEIR
    # share filters, and the four combinations are all meaningful. False means the owner has told us to
    # leave the account alone — no excludes merged in, ours taken back out — which they do when the
    # account's own Plex restrictions conflict with ours (an "allow only" label list, discussion #92).
    # The cost is stated where it is set: the account can then see other people's rows.
    manage_sharing: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # plex.tv stopped listing this account — set by the roster sweep, CLEARED the moment it lists them
    # again, so a re-invite heals on the next sync. Distinct from `enabled=0`, which the owner also
    # sets by hand: without it the Users list cannot say why somebody is switched off.
    departed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The owner filed them away: picks and run history dropped, row hidden from the Users list. Never
    # a DELETE — `restriction_snapshots` is RESTRICT-keyed here and holds the only copy of their
    # pre-Shortlist filters, which uninstall restores from (plex-safety rule 2).
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cold_start: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(String(255), default="")  # as stored by Plex (title-cased)
    request_tag: Mapped[str] = mapped_column(String(64), default="")  # tag added to titles requested for them
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)

    run_users: Mapped[list[RunUser]] = relationship(back_populates="user")

    @property
    def display_name(self) -> str:
        """What to call this person in the UI: owner's nickname, else the Tautulli friendly name,
        else the bare Plex username. Same precedence `{user}` renders in a row title — keep the one
        source of truth so the Users page, the runs view, and Plex never disagree on someone's name."""
        return self.nickname or self.friendly_name or self.username


class Collection(Base):
    """A curated-row definition, combining a build mode, an audience, and a recipe.

    The default ``picked`` collection is seeded on migration and reproduces today's single
    per-user "Picked for You" row, so an upgrade changes nothing until the owner adds more.
    """

    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    build: Mapped[str] = mapped_column(String(16), default="per_person")  # per_person | shared
    audience: Mapped[str] = mapped_column(String(16), default="everyone")  # everyone | subset
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # This row's own run schedule as a 5-field cron string; "" = never runs on a schedule (only
    # manual "run now"). There is NO global schedule — each row runs on its own cron, or not at all.
    schedule: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=15)
    media: Mapped[str] = mapped_column(String(16), default="both")  # movie | show | both
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    name_template: Mapped[str] = mapped_column(String(255), default="")  # per_person display name
    # What to call this row for someone whose name cannot be filled in — a `{top_seed}` row for a
    # person with nothing watched. NULL/"" means there is no such name, and the row is simply NOT
    # built for them: Shortlist never invents one (issue #84). NOT backfilled — 0070 adds the column
    # and nothing else, and its docstring says why. So on upgrade a `{top_seed}` row stops being built
    # for anyone who cannot be named until the operator names it; nothing is deleted, their existing
    # collection keeps its label, stays hidden from everyone else, and simply stops being updated.
    fallback_name: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    # Per-row override of which discovery sources feed this row; [] -> inherit global candidates.sources.
    candidate_sources: Mapped[list] = mapped_column(JSON, default=list)
    # Per-row cap on already-finished titles, as a fraction (0.0 all fresh .. 1.0 no filtering).
    # NULL -> inherit the global recommendations.watched_pct.
    watched_pct: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    # A REWATCH row: already-finished titles lead it, unwatched ones only fill what's left. Not
    # expressible with `watched_pct`, which is a ceiling that never PROMOTES a finished title.
    rewatch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    # Shows only: drop any series this person has STARTED, however little. Stricter than the normal
    # filter, which only drops FINISHED ones — so this is what makes "a series to start" true.
    unstarted_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    # Per-row refresh cadence in DAYS: 0 = never once built, 1 = nightly, N = every N days. NULL ->
    # inherit the global recommendations.refresh_days. Was `freshness`, a 0..1 fraction a curve
    # stretched onto 1..14 days; migration 0065 converted every value through that same curve.
    refresh_days: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # Per-row weight on a title's RELEASE DATE when ranking it (0.0 ignore age .. 1.0 strongly prefer
    # new). NULL -> inherit the global recommendations.recency. Nullable rather than defaulting to
    # 0.0 because "never touched" and "deliberately off" must stay distinguishable: every row that
    # predates this column reads NULL and follows the global, while a Hidden Gems row can pin 0.0.
    recency: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    # How many of a person's most recent watches the web-search source searches for this row (one
    # cached search each). NULL -> inherit the global recommendations.recent_count.
    recent_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # How many watched titles SEED this row — what every source searches from, not just the web one.
    # NULL -> inherit the engine default (30).
    max_seeds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # What this row does for someone with too little watch history: "popular" (the server's top-rated
    # titles) or "skip" (don't build it for them; remove any copy they already have).
    # NULL -> inherit the global recommendations.cold_start.
    cold_start: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    # How many of a person's most recent watches this row may be built from, of which ONE is chosen per
    # run so the row advances instead of sitting on their newest watch for ever. 1 = always the most
    # recent (the original behaviour). Not nullable and not inheritable, like pick_order: whether a row
    # rotates belongs to what that row IS, not to a server-wide default.
    seed_window: Mapped[int] = mapped_column(Integer, default=1, nullable=False, server_default="1")
    # How the delivered collection is ORDERED: best | rating | newest | shuffle. Not nullable and not
    # inheritable — unlike refresh_days/max_seeds there is no global default to fall back to, because the
    # right order belongs to what a row IS rather than to the server.
    pick_order: Mapped[str] = mapped_column(String(16), default="best")
    # Specific Plex library section keys this row builds in; [] -> every library of its media type.
    library_keys: Mapped[list] = mapped_column(JSON, default=list)
    min_watchers: Mapped[int] = mapped_column(Integer, default=2)  # shared: aggregate-privacy threshold
    request_tag: Mapped[str] = mapped_column(String(64), default="")  # tag added to titles requested via this row
    # Where the row shows for the owner / home users: "both" (Home + Library), "home", or "library".
    placement: Mapped[str] = mapped_column(String(16), default="both")
    # Where the row shows for friends (shared users): "both" (Friends Home + Library), "home", or "library".
    placement_friends: Mapped[str] = mapped_column(String(16), default="both")
    # Pin the row to the TOP of its library's Recommended shelf (server-wide order, not per-user).
    pin_top: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-library override of where THIS row sits in the Recommended shelf: {sectionKey: {anchor, before}}.
    # {} -> inherit the global default (settings `rows.hub_anchor`). A library absent here inherits too.
    hub_anchor: Mapped[dict] = mapped_column(JSON, default=dict)
    # Dead as of the curate removal (migration 0036 clears it): the LLM no longer ranks a candidate
    # pool, so there is no per-row curation recipe. Column kept — dropping it would rebuild the whole
    # table (inbound FKs); a future migration can remove it.
    # This row's own Sonarr/Radarr request settings. NULL -> inherit the global `requests.*` setting,
    # the same convention `watched_pct` / `recency` / `refresh_days` / `cold_start` already use, so an
    # upgrade changes nothing until the owner sets one.
    #
    # Only PROFILE and ROOT FOLDER are per row; URL and API key stay global. The case this serves is
    # one Radarr filing a kids row into /data/Kids at a lower profile, not a second Radarr.
    #
    # `max_per_run` and the rating source are deliberately absent: they are the run's ceiling and its
    # one MDBList account, and a row able to raise either would make the global setting a suggestion.
    # `req_max_per_row` may only ever RESTRICT below it (`resolve_request_config` clamps).
    #
    # Meaningless on a shared row, which is built from titles people have already WATCHED and so are
    # already on the server — it surfaces nothing missing to request. The editor hides the section.
    req_min_rating: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    req_min_votes: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    req_min_demand: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    req_min_year: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    req_max_year: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    req_auto_send: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    req_auto_min_demand: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    req_auto_min_rating: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    req_max_per_row: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    req_radarr_quality_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    req_radarr_root_folder: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    req_sonarr_quality_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    req_sonarr_root_folder: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    # How much of a show Sonarr monitors for THIS row's requests (Sonarr's Add Series "Monitor"
    # choice). NULL -> inherit the global `requests.sonarr.monitor`. A kids row can take season 1
    # only while everything else keeps the whole run of a show.
    req_sonarr_monitor: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    # Tag this row's requests with the wanting person's slug, so the owner can see in Sonarr/Radarr
    # who a title was added for. NULL -> inherit the global `requests.auto_user_tag`.
    #
    # Meaningless on a shared row, which is built from what the whole server watched and belongs to
    # nobody in particular — there is no one person to name. The editor hides it there, exactly as it
    # already hides `request_tag`.
    req_auto_user_tag: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    prompt: Mapped[dict] = mapped_column(JSON, default=dict)
    # Custom collection poster for this row. {} -> Plex's own artwork. Shape:
    # {"mode": "upload"|"generate", "title", "subtitle", "style"}. No image bytes live here — an
    # uploaded/generated image is stored in the `poster_assets` table, keyed by collection id / prompt.
    poster: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CollectionAudience(Base):
    """Who a subset-audience collection is built for / visible to. Empty for audience='everyone'."""

    __tablename__ = "collection_audience"

    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class PosterAsset(Base):
    """Binary image storage for row posters: uploaded originals and cached generated images.

    Kept in the DB (which lives under /config) rather than on the filesystem so a poster survives a
    container recreate and travels with a config backup. ``key`` namespaces the two kinds:
    ``upload:<collection_id>`` for a user's uploaded image, ``gen:<prompt_hash>`` for a generated one
    (so an identical prompt across users/runs is generated once, not every night per person)."""

    __tablename__ = "poster_assets"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    image: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str] = mapped_column(String(64), default="image/png")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CollectionUserOverride(Base):
    """One person's tweaks to one row: mute it for them, resize it, or restyle its curation.

    Absence of a row means "use the collection's own settings". A row a person is not in the
    audience of has no override and is simply never built for them.
    """

    __tablename__ = "collection_user_overrides"

    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)  # this person doesn't get this row
    row_size: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None -> the row's own size
    # How many recent watches the AI web-search source searches for THIS person on THIS row (1..25).
    # None -> fall through to the row's own recent_count, then the global recommendations.recent_count.
    recent_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Dead as of the curate removal (migration 0036 clears it) — see Collection.prompt. Column kept.
    prompt: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16))  # schedule | manual | wizard
    #: When the run was QUEUED — this row is created the moment someone presses Run.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: When the engine actually began, which is not the same moment: a run waits here behind whatever
    #: holds the Plex writer lock. NULL means it never got that far — cancelled or reaped while still
    #: queued — and such a run has no duration, having done nothing.
    began_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | ok | error | aborted
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)

    users: Mapped[list[RunUser]] = relationship(back_populates="run", cascade="all, delete-orphan")
    shared_rows: Mapped[list[RunSharedRow]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RunUser(Base):
    __tablename__ = "run_users"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    # RESTRICT: run history is the only copy of what happened. See User's cascade policy.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why a non-failing outcome happened (a `skipped` row that could not build). NOT an error: the
    # UI counts every non-null `error` as a failed user, which is how "skipped" ended up on screen
    # with no explanation at all (issue #3). NULL on legacy rows.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # `llm_tokens` split by WHERE it went: {"llm_web": M}. {} on legacy rows (and on rows written
    # before the curate step was removed, which may still carry a "curate"/"llm_library" key). Exa is
    # counted apart from tokens — it bills per search request, not per token.
    llm_tokens_by_step: Mapped[dict] = mapped_column(JSON, default=dict)
    exa_searches: Mapped[int] = mapped_column(Integer, default=0)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-(row, library) delivery breakdown for the run detail UI; [] on legacy rows (falls back to
    # the merged `diff` + `picks`). Each entry: row_slug/row_title, library_key/library_title,
    # added/removed/kept/deleted, created, and that library's own picks.
    breakdown: Mapped[list] = mapped_column(JSON, default=list)
    # Full per-user pipeline trace (seeds, per-source queries+returns, web-search/RAG prompts) so the
    # UI can show "exactly what happened for this person". {} on legacy rows and skipped/cold users.
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
    # What this run decided about each per-person row FOR THIS PERSON:
    # {row_slug: "due" | "not_due" | "muted" | "not_in_audience"}. `reason` is one sentence about the
    # PERSON and cannot be attributed to a row, so without this a rows-first view had nowhere to put
    # a skipped user — and on a run where nothing was due, that is everybody. {} on legacy rows and
    # on a cold-start skip, which never reaches the decision; the UI must render that as "not
    # recorded", never as "no rows were considered".
    rows_considered: Mapped[dict] = mapped_column(JSON, default=dict)
    # What each ROW cost this person, and what the shared setup cost:
    # {"setup_ms": int, "rows": {slug: {"duration_ms": int, "blocked_ms": int}},
    #  "pools": [{"label", "tokens", "exa_searches", "duration_ms", "rows": [slug, ...]}]}.
    #
    # NULL on a legacy run — "not recorded", which the UI must never render as 0s. `duration_ms` is
    # wall clock INCLUDING `blocked_ms` (time waiting on the shared Plex write lock); work time is
    # the difference. Tokens live on the POOL, never on a row: pools are shared between rows, so a
    # per-row token figure would be an allocation invented by the UI rather than a measurement.
    cost: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    run: Mapped[Run] = relationship(back_populates="users")
    user: Mapped[User] = relationship(back_populates="run_users")


class RunSharedRow(Base):
    """One SHARED row's outcome in one run — the per-row twin of `RunUser`.

    A shared row is built once for the whole server from pooled history, so it belongs to no user and
    could not have a `run_users` row. It therefore had NO run record at all: `persist_report` files
    reports by user slug, a shared row's is `shared_<slug>`, and the lookup miss `continue`d. Its
    trace, breakdown, token spend and picks were all discarded, leaving only the `run.shared` audit
    event's status and diff-titles — so "why did this row pick that" was unanswerable, and a run whose
    only work was a shared row showed nothing but a wall of skipped people.

    Picks are JSON here rather than `picks` rows. `PickRow.user_id` is non-nullable and RESTRICT-keyed
    to a real account (see `User`'s cascade policy), and inventing nullable-user pick rows would put
    rows nobody watched into every per-user hit-rate and history query.

    Cascades with its run, unlike `run_users`: there is no irreplaceable account record on the other
    end of this key, so the policy that makes a user's history un-deletable does not apply.
    """

    __tablename__ = "run_shared_rows"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    #: The COLLECTION's own slug, not the `shared_`-prefixed report slug the engine files under.
    collection_slug: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: As rendered at run time — a row renamed later must not rewrite what a past run says it built.
    row_title: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why a non-failing outcome happened, same meaning as `RunUser.reason` (issue #3).
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_by_step: Mapped[dict] = mapped_column(JSON, default=dict)
    exa_searches: Mapped[int] = mapped_column(Integer, default=0)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    breakdown: Mapped[list] = mapped_column(JSON, default=list)
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
    #: The delivered picks, same field set the API renders for a user's picks.
    picks: Mapped[list] = mapped_column(JSON, default=list)
    #: Which plex account ids could SEE this row when it was delivered; NULL = everyone, and also what
    #: every pre-0076 row carries. `collection_audience` is current state with no history, so without
    #: this snapshot, adding someone to a subset row today would retroactively credit their older
    #: watches to a row they could not see at the time.
    audience: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    #: Who had this row switched OFF at delivery — a deny-list, kept separate from `audience`.
    #:
    #: Folding mutes into `audience` forced a PUBLIC row to stop saying "everyone" the moment one
    #: person muted it: the snapshot became a concrete list of whoever existed that night, so anyone
    #: invited afterwards was permanently outside it and could never be credited for that row. The
    #: miss is silent and unrecoverable, because credit is decided from the past and a watched title
    #: is never re-delivered.
    muted: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    #: When this row's contents actually landed on Plex — NOT `Run.started_at`.
    #:
    #: The per-person path learned this the hard way and wrote it down: a run persists each row as it
    #: finishes, so the run's start trails the delivery by minutes to tens of minutes (a TV collection
    #: write alone costs ~16.5s, times 47 people). Judging a play against the run's START means
    #: judging it against the row the run was BUILDING rather than the one Plex was still serving —
    #: which drops a credit for a title this run removed, and invents one for a title it added.
    #: `_load_per_person` derives its equivalent from `min(picks.created_at)`; a shared row writes no
    #: picks, so it has to be stamped here. NULL on rows written before this column existed, which
    #: fall back to `Run.started_at`.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="shared_rows")


class PickRow(Base):
    __tablename__ = "picks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), index=True, nullable=True)
    # RESTRICT: the impact ledger is the only copy of what was recommended. See User's cascade policy.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer)
    # A TMDB id is unique only within its namespace, so the pair (tmdb_id, media_type) is what
    # identifies a title — the staleness guard reads these back and would otherwise let a movie
    # suppress the show that shares its number.
    media_type: Mapped[str] = mapped_column(String(16))  # no default: a forgotten one is the bug
    rating_key: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int] = mapped_column(Integer)
    # Which row this pick belongs to (Collection.slug). Blank on pre-0004 rows and legacy single-row
    # runs; the user page groups a person's picks by this so each row shows its own titles.
    collection_slug: Mapped[str] = mapped_column(String(255), default="", index=True)
    # The library this pick was delivered into: `section_key` is the stable Plex section key,
    # `library` its display name ("Movies"). A row spanning >1 library is one Plex collection PER
    # library, so the effectiveness report splits it into one line per library. Blank on pre-0020 rows.
    section_key: Mapped[str] = mapped_column(String(64), default="")
    library: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    # Provenance, so "why is this here?" is answerable from the UI: which source(s) surfaced the
    # title, and how strongly they vouched for it (1.0 = top of TMDB's list for that seed, or a
    # source with no ranking of its own). Comma-separated source ids; blank on pre-0035 rows.
    sources: Mapped[str] = mapped_column(String(255), default="")
    affinity: Mapped[float] = mapped_column(Float, default=1.0)
    seed_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seed_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # TMDB score and release year, carried so a row ordered by either can sort its CARRIED-FORWARD
    # picks too — those are rebuilt from this table, so without them a "highest rated" row would sort
    # every surviving pick as unrated. NULL on rows delivered before 0056; such picks sort last and
    # keep their relative order until the row next rebuilds.
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The row_recipe (settings fingerprint) this pick was built under; NULL on picks written
    # before recipes existed, which reads as "unknown" and does not force a rebuild.
    recipe: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    # Both indexed: the effectiveness report is windowed, so every aggregate on it filters by one of
    # these two, over the largest table in this schema (retention prunes it, but only by whole runs).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)  # hit-rate
    # When they finished it, as opposed to merely starting it. `watched_at` is Plex's binary flag,
    # which for a SERIES flips on the first finished episode — so one episode of a 60-episode show
    # has always scored identically to a whole film, and 87% of this server's credited show picks
    # were people who had not finished the series (measured 2026-08-16: 21 of 158 finished).
    #
    # A movie has no middle state, so this is stamped with the same value as `watched_at`. A series
    # gets it only once every episode is watched — the wording the user page already uses. It is
    # deliberately NOT the engine's "already seen" bar: that one is `min(80%, max(3, 15%))` episodes
    # (rows.py `_watched_titles`), which answers "engaged enough not to re-recommend?", a different
    # question from "did they finish it?". Two thresholds, on purpose.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # The furthest they got, 0-100, from `watch_sessions`. Denormalised so the report does not join
    # sessions on every read, and NULL where we never saw a live session — which is not 0%: "we did
    # not watch them watch it" and "they bailed immediately" are different facts.
    max_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RestrictionSnapshotRow(Base):
    __tablename__ = "restriction_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # RESTRICT, and this is the one that must never become a cascade: these are the person's share
    # filters as they were BEFORE Shortlist, and uninstall restores from them (plex-safety rule 2).
    # There is no second copy anywhere. See User's cascade policy.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason: Mapped[str] = mapped_column(String(32), default="initial")  # initial | sync | uninstall_restore
    filters_before: Mapped[dict] = mapped_column(JSON, default=dict)
    filters_after: Mapped[dict] = mapped_column(JSON, default=dict)


class WatchStateSnapshot(Base):
    """One account's complete watch state, taken before a transfer changed it.

    The watching-account transfer MIRRORS: it un-marks whatever the source has not watched, so it is
    the only path in Shortlist that can remove someone's watch history. Rule 2 governs exactly this
    shape — snapshot before the first mutation, restore from the snapshot on undo — and it is here for
    the same reason it exists for share filters: there is no second copy anywhere, and Plex keeps no
    history of what a `viewCount` used to be.

    Restoring must put back the COUNTS and OFFSETS, not merely watched/unwatched. Re-marking a
    rewatched film once, or re-marking a part-watched episode as finished, produces a third state that
    never existed on either account — which is worse than not restoring at all, because it looks like
    it worked.

    `state` is a compact list of `[rating_key, view_count, view_offset_ms, media_type,
    show_rating_key]`, not a dict of objects: a heavy account runs to ~11,000 leaves and this row is
    read whole or not at all. The fifth element is what lets a restore tell a show it has emptied from
    one it still holds episodes of — rows written before it exists carry four, and `undo_transfer`
    withholds show clearing entirely for any snapshot that is not uniformly five.
    """

    __tablename__ = "watch_state_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # RESTRICT, for the same reason as `restriction_snapshots`: this is the only record of what the
    # account looked like before we touched it. See User's cascade policy.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: Which job wrote it, so an undo restores the snapshot for THAT transfer rather than the newest.
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: Set when this snapshot has been restored, so an undo cannot silently run twice and a second
    #: press reports "already restored" rather than replaying against a state it no longer describes.
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Whether the read behind this snapshot saw every library. False means a library 403'd, so the
    #: snapshot describes LESS than the account held — and since the restore is a mirror of it, acting
    #: on one would un-mark every watch it never recorded. `undo_transfer` refuses instead.
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    state: Mapped[list] = mapped_column(JSON, default=list)


class Delivery(Base):
    """Which Plex collection Shortlist built for one (row, user, library) — the delivery ledger.

    Answers the one question nothing else in the schema can: *which object on the server is this
    row, for this person, in this library?* Every reconcile needs it, and every other way of asking
    is a guess:

    * **By title from the row's template** — works for a static, `{library_name}` or `{user}` name,
      and cannot work for `{top_seed}`, which renders differently every single run.
    * **By the last run's breakdown** — what this replaced. Rows have their own crons, so the latest
      run is routinely scoped to ONE row; delete row B the morning after row A ran and there was
      nothing to find. `DELETE /api/runs` erased it outright while claiming to change nothing on Plex.

    Keyed by SLUG, not by foreign key, deliberately: the row it describes is usually being deleted
    when this is read, and a cascade would take the ledger with it. Rows are upserted per delivery and
    swept when their collection is removed, so the table stays the size of "collections that exist".
    """

    __tablename__ = "deliveries"

    # (row, user, library) is the identity — one collection per row per person per library.
    collection_slug: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_slug: Mapped[str] = mapped_column(String(255), primary_key=True)
    library_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    rating_key: Mapped[int] = mapped_column(Integer, index=True)
    # The title as delivered, marker-stripped. NOT an addressing key — `rating_key` is the only thing
    # read back — it is here so the ledger is legible in an audit ("which row was this?") without
    # joining anything. Deliberately not used as a fallback: a title match is exactly the mechanism
    # this table replaced, and having two answers would hide which one was wrong.
    title: Mapped[str] = mapped_column(String(512), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CacheRow(Base):
    __tablename__ = "caches"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)  # tmdb | trakt | library_index
    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[float] = mapped_column(Float)  # unix timestamp


class WatchedTitle(Base):
    """One title a person has watched, cached so it does not have to be re-read every night.

    The watched set drives every recommendation and the dashboard's hit rate, and it used to be read
    in FULL — per user, per library, 500 titles a page with full metadata and GUIDs — on the nightly
    sync AND again inside every run. On a 40-user server that is hundreds of large XML responses a
    night for a set that changes by a handful of items.

    Caching it makes the nightly read incremental — back to the cursor, applied client-side against
    `sort=lastViewedAt:desc` because the PMS silently ignores a `lastViewedAt>=` filter. The cache is
    not the source of truth: an incremental read that provably covered its window can notice an
    un-watch INSIDE it (`watch_cache._drop_vanished_since`), but nothing further back, so
    `watch_sync_state.last_full_at` still drives a periodic complete re-read.
    """

    __tablename__ = "watched_titles"
    __table_args__ = (
        UniqueConstraint("user_id", "section_key", "rating_key", name="uq_watched_title"),
        Index("ix_watched_titles_user_viewed", "user_id", "viewed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # CASCADE: this is a cache of what the PMS already knows. See User's cascade policy.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    section_key: Mapped[str] = mapped_column(String(64))
    # Plex's own id for the item in this library — the stable key within a section, and what an
    # incremental upsert matches on.
    rating_key: Mapped[int] = mapped_column(Integer)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    media_type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    watch_count: Mapped[int] = mapped_column(Integer, default=1)
    # A show's finished fraction, straight from Plex (viewedLeafCount / leafCount) rather than
    # reconstructed from play counts — so a bulk-marked season counts correctly. NULL for movies and
    # for anything that reports no episode totals: 0 would read as "none of it watched", which is a
    # different claim from "there are no episodes to count".
    viewed_leaf_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leaf_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # When this watch REALLY happened, for a row transferred from another account. NULL for
    # everything Plex reported directly, which is almost every row.
    #
    # This exists because Plex cannot backdate a watch. Marking a title played is a scrobble, and a
    # scrobble is stamped `now` — so transferring an owner's history onto their new watching account
    # leaves the PMS believing 2,000 titles were all watched today. `viewed_at` then faithfully
    # records that lie on the next sync, and every seed the engine picks comes from a set with one
    # timestamp, i.e. an arbitrary order. Recommendations quietly become noise.
    #
    # So the transfer writes the true date HERE and `watch_cache` protects it three ways: `_upsert`
    # never writes the column, the FULL read's replace skips rows that have one, and
    # `_drop_vanished_since` ignores them. That last pair matters most — a non-scrobbled transfer
    # leaves rows the PMS has never heard of, so a blind replace deletes the whole transfer on the
    # very first sync. Everything asking "how recently?" then reads this first.
    source_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What THIS person rated the title in Plex, 0..10, or NULL if they never rated it — which is the
    # overwhelming majority (0.27% of watched rows carried one on a real 50-account server). NULL is
    # therefore the load-bearing value and must stay distinguishable from 0.0, an actual rating.
    #
    # A rating change does NOT move `lastViewedAt`, which decides how soon it lands — but only for an
    # OLD title. The incremental walk is ordered by that stamp and stops at the cursor, and every row
    # it does return is upserted with its current `userRating`, so rating something watched since the
    # last sync arrives on the next one. It is a rating on a title older than the cursor that waits
    # for the full re-read (`sync.watch_full_days`).
    #
    # Measured on a live server after the 1.2.0 upgrade: of three accounts with ratings, two landed
    # on an ordinary incremental sync and only the third — an older watch — needed the full pass. An
    # earlier version of this comment claimed the incremental read "cannot see one", which is wrong
    # and undersold the common case: people rate what they just finished watching.
    user_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WatchSyncState(Base):
    """How far the watched-title cache has been read for one (person, library).

    `cursor_viewed_at` is advanced ONLY after a complete successful page walk, and deliberately set
    slightly behind the newest thing seen — see `WatchCache` for why. `last_full_at` is what makes
    the incremental read safe: an incremental read sees an un-watch only inside the window it
    covered, never one further back or a title deleted from the library, so a complete re-read has
    to happen on a schedule regardless.
    """

    __tablename__ = "watch_sync_state"
    __table_args__ = (UniqueConstraint("user_id", "section_key", name="uq_watch_sync_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # CASCADE: this is a cursor into a cache, rebuilt by one full read. See User's cascade policy.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    section_key: Mapped[str] = mapped_column(String(64))
    # None means "never read" — which always forces a full read, never a guess at where to resume.
    cursor_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_full_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_incremental_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)


class RunLogLine(Base):
    """One line of a run's activity feed, kept.

    Deliberately NOT the `events` table. `events` is the audit trail — "what changed on whose share
    at 03:31" (rule 10) — with its own retention and a scope-indexed query shape. This is narration:
    high-volume, per-stage, only ever read as one run's chronological feed. Merging them makes both
    queries worse and the retention rules contradictory.

    This used to live only in a bounded in-memory deque for the last 10 runs, wiped on restart, so
    opening any older run's log showed nothing at all.
    """

    __tablename__ = "run_log_lines"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_log_line_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    # Monotonic within a run. The client merges a seeded fetch with the live SSE tail, and a
    # timestamp is not unique enough to dedupe on — several lines land in the same millisecond.
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # The user slug, or "Shortlist" for the server-wide phases. Blank never happens; the engine
    # always names a subject.
    user_slug: Mapped[str] = mapped_column(String(255), default="")
    stage: Mapped[str] = mapped_column(String(64), default="")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(String(1024), default="")
    level: Mapped[str] = mapped_column(String(8), default="info")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    scope: Mapped[str] = mapped_column(String(64), index=True)  # e.g. run.user, privacy.sync, collection
    message: Mapped[dict] = mapped_column(JSON, default=dict)  # structured diff/audit payload


class RequestCandidate(Base):
    """A wanted-but-missing title in the approval inbox: surfaced by a run, awaiting the owner's call.

    One row per (tmdb_id, media_type): a title re-surfaced by a later run refreshes its demand and
    rating in place rather than duplicating. ``status`` is pending (waiting on the owner), sent (asked
    of Sonarr/Radarr), or rejected (dismissed — never re-queued, so a "no" can't nag every night).
    """

    __tablename__ = "request_candidates"
    __table_args__ = (UniqueConstraint("tmdb_id", "media_type", name="uq_request_candidate_title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    media_type: Mapped[str] = mapped_column(String(16))  # movie | show
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_id: Mapped[str] = mapped_column(String(16), default="")  # "tt…" -> inbox deep-links to IMDb
    # TMDB's poster path ("/abc.jpg"), NOT a URL: the image host and size buckets are TMDB's to
    # change, so the UI builds the URL. Empty on pre-0044 rows and for titles TMDB has no art for —
    # the inbox draws a placeholder tile rather than a broken image.
    poster_path: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # TMDB's synopsis, so the inbox can be triaged without opening a tab per unfamiliar title
    # (discussion #87). Text, not String(n): TMDB does not publish a length bound, and a truncated
    # synopsis is worse than a long one the UI clamps. Empty on pre-0071 rows and for titles TMDB has
    # no synopsis for — the inbox omits the paragraph rather than drawing an empty gap.
    overview: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Which ROW claimed this title, so an approval months later can resolve that row's Sonarr/Radarr
    # target. `why[].row` is the rendered name ("Because you watched Bluey") and carries the person's
    # display name and their own seed, so it identifies nothing stable. NULL on rows queued before
    # per-row settings existed; those fall back to the global config, which is what they were queued
    # under anyway.
    row_slug: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    rating: Mapped[float] = mapped_column(Float, default=0.0)  # on the chosen source (TMDB, or IMDb)
    vote_count: Mapped[int] = mapped_column(Integer, default=0)  # vote count on that same source
    demand: Mapped[int] = mapped_column(Integer, default=1)  # distinct users whose picks wanted it
    tags: Mapped[list] = mapped_column(JSON, default=list)  # per-user/per-row tags to apply when sent
    wanters: Mapped[list] = mapped_column(JSON, default=list)  # usernames whose picks wanted it (the "who")
    # Full provenance: [{user, row, seed, source}] — which person, in which row, and why (the seed
    # "because you watched …") each request got here. Richer than `wanters`; drives the inbox detail.
    why: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | sent | rejected
    detail: Mapped[str] = mapped_column(String(512), default="")  # send outcome, or why it's queued
    # The arr's titleSlug, captured when the title is sent, so the inbox deep-links straight to its
    # Sonarr/Radarr page (Sonarr has only `/series/<slug>`, no id URL). None for titles queued/sent
    # before this was recorded — the inbox falls back to the arr's home page for those.
    arr_slug: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # On Sonarr/Radarr's import-exclusion list (usually from a past delete): surfaced in the inbox so
    # the owner knows approving it is a no-op until they remove the exclusion in the Arr.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # Owner cleared this from the Sent log. The row STAYS `status="sent"` — a load-bearing tombstone
    # that stops a still-downloading title being re-requested (see delete_requests / _persist_request_queue)
    # — so we hide it from the UI instead of deleting it. Excluded from the inbox list; engine unaffected.
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    first_seen_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # which run first surfaced it
    # When this title was actually asked of Sonarr/Radarr. Stamped ONCE, when status flips to "sent".
    #
    # `updated_at` was used as a proxy and is wrong in both directions: it has `onupdate`, so clearing
    # an old title from the Sent log bumps it and pulls a months-old request into a recent window,
    # while an edit after the send pushes it out. The dashboard's "watched since sent" needs a
    # timestamp that means what it says. NULL on rows sent before this column existed — the report
    # falls back to `updated_at` for those, which is exactly as good as it used to be.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Job(Base):
    """One unit of background maintenance — a cleanup, a filter write, a sync check.

    Runs are NOT jobs: a run is a long, rich, user-facing operation with its own page, live progress,
    per-user results and a cancel button. A job is a short mechanical fix-up. They stay separate on
    purpose; what they share is that neither may write to Plex while the other is.

    The table exists so background work survives a restart. Before this, every maintenance action was
    a fire-and-forget executor call: if the container died — or Plex was simply down — the work was
    lost with no record and nothing ever retried it. A user disabled during a Plex outage kept their
    rows on Plex for ever, because no later run revisits a disabled user.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    # What the job needs to do its work — a user slug, a row slug, a set of account ids. Kept as
    # data, never as a closure, so a job is still runnable after the process that queued it is gone.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued -> running -> done | failed. `running` at boot means the process died mid-job; startup
    # recovery requeues those (every job kind is written to be idempotent, so a partial replay is safe).
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    # Human-readable one-liner for the Tools list ("Removed 2 rows for sarah"), set on completion.
    detail: Mapped[str] = mapped_column(String(512), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # redacted (rule 9)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WatchEvent(Base):
    """One play Plex's own history log recorded — who, what, and exactly when.

    The log (`/status/sessions/history/all`) is the signal Shortlist never read. It is server-side and
    deep (101,604 rows back to 2020-10-26 on the maintainer's box), it carries the plex.tv
    `accountID` rather than a display name, and `viewedAt>` filtering works on it — which the library
    read's equivalent does not. So it survives our downtime completely: whatever we miss is still
    there on the next sweep, with the right timestamps.

    It records COMPLETIONS, not starts. Verified against a live server: an episode being played at 73%
    with no `viewCount` had no entry. Starts live in :class:`WatchSession`.
    """

    __tablename__ = "watch_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The plex.tv account id, deliberately NOT a FK to `users.id`: an event can arrive for an account
    #: with no user row yet, and dropping it would lose history the moment someone is invited.
    plex_account_id: Mapped[int] = mapped_column(Integer, index=True)
    #: The movie, or the EPISODE that was played.
    rating_key: Mapped[int] = mapped_column(Integer, index=True)
    #: The SHOW, for an episode — parsed out of `grandparentKey`'s path, because history entries carry
    #: no `grandparentRatingKey` attribute. This is what actually matches a pick: a pick for a series
    #: stores the show's key, and over 30 days of real history 46 of 78 matches were reachable ONLY
    #: through this column.
    show_rating_key: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    media_type: Mapped[str] = mapped_column(String(16))
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(16), default="history")
    #: Plex's own row id. Unique, because the log repeats itself — the same item for the same account
    #: seconds apart, and twice within one second on one device (both observed live). Deduping on this
    #: needs no time-window heuristic and cannot drop a genuine rewatch.
    history_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SharedRowWatch(Base):
    """One person's outcome for one title on one SHARED row.

    The shared-row twin of the `watched_at`/`finished_at`/`max_percent` stamps on `picks`. A shared row
    is built once for the whole server and has no per-user pick row to stamp, so without this a title
    that lived only on a shared row credited nothing, and the feature quietly measured per-person rows
    only. See migration 0078 for why it is neither a `picks` row nor a field on `run_shared_rows`.

    Keyed by SLUG rather than by foreign key, like `deliveries` and for the same reason: the row this
    describes may be deleted while the watch remains true.
    """

    __tablename__ = "shared_row_watches"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    collection_slug: Mapped[str] = mapped_column(String(255), primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Part of the key: TMDB ids are namespaced per type, so movie 1399 is not show 1399.
    media_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    # `timezone=True` to match every other DateTime in this file. SQLite ignores the flag, so this is
    # not a behaviour change — but `_recent_watches` now sorts one list whose keys come from BOTH this
    # column and `picks.watched_at`, and `deleted_rows` takes min/max across this and
    # `picks.created_at`. Those are correct only because the two columns deserialise identically, and
    # the odd one out reads as deliberate to the next editor.
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Films only, same rule as `picks.max_percent` — an episode's progress is not the series'.
    max_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WatchSession(Base):
    """One playback session as it happened — the only place a PARTIAL watch exists.

    Plex publishes no partial-play API. The notification websocket pushes position
    (`PlaySessionStateNotification`: `sessionKey`, `ratingKey`, `viewOffset`, `state`) and nothing
    else — no user, no runtime — so this row is assembled: identity by resolving `session_key` against
    `/status/sessions` on the first PLAYING event, runtime from metadata, progress from `viewOffset`.
    That assembly is why Tautulli keeps its own database, and it is what we are doing here.

    Measured on a live server before this existed: events arrive per session about every 10s, roughly
    one a second across the whole server, and `viewOffset` advances 1:1 with wall clock. So state is
    held in memory and flushed on a throttle — a write per event would be a write per second to record
    that ten seconds passed.
    """

    __tablename__ = "watch_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plex_account_id: Mapped[int] = mapped_column(Integer, index=True)
    #: Plex's session key — unique only while the session is LIVE, and reused afterwards. The open
    #: session is the one with `ended_at IS NULL`, never "the newest row with this key".
    session_key: Mapped[str] = mapped_column(String(32), index=True)
    rating_key: Mapped[int] = mapped_column(Integer, index=True)
    show_rating_key: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    media_type: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_offset_ms: Mapped[int] = mapped_column(Integer, default=0)
    #: NULL until the runtime is known. A percentage of an unknown runtime is worse than none.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: stopped | timeout | replaced. `timeout` is recorded rather than dressed up as a stop: a client
    #: that crashes or drops off the network never sends one, which is why Tautulli schedules a
    #: force-stop instead of waiting for it, and why we do too.
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)

    @property
    def percent(self) -> int | None:
        """How far they got, 0-100, or None when the runtime is unknown.

        Capped at 100: a re-scanned library or a bulk mark can leave an offset past the runtime, and
        `test_a_series_watched_beyond_its_episode_count_is_finished` records the same shape being
        observed live at 145%.
        """
        if not self.duration_ms:
            return None
        return min(100, round(100 * self.max_offset_ms / self.duration_ms))
