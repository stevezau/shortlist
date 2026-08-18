"""Proof, per template, that the app can actually deliver what each row template promises.

The gallery is a set of claims — "rewatches come first", "needs 3 watchers", "never started". A tile
that produces an ordinary row is worse than no tile: it teaches the owner that the settings are
decorative. Two of them WERE hollow (the engine had no mode for a rewatch shelf, and none for
"unstarted"), which is what this file exists to stop recurring.

Two layers, because "supported" means two different things:

1. :class:`TestEveryTemplateSaves` — every field a template sets survives a real POST and comes back
   unchanged. Proves the OPTIONS exist end to end (schema, column, serialiser), not just in the UI.
2. :class:`TestEveryTemplateDelivers` — the template's values drive the REAL engine against the fake
   PMS, and the delivered row is asserted against that template's specific claim. Proves the DATA and
   behaviour exist, which is the half a round-trip test cannot see.

Each behavioural claim below was checked by BREAKING the mechanism it depends on and confirming this
file catches it: disabling the rewatch ordering, the started-shows filter, the min_watchers floor and
the per-row seed cap each fails exactly one test here and nothing else.

The two media claims ("Movies only", "TV only") are the exception, and honestly so: media is enforced
at five independent points — the history is narrowed before seeds are derived, the row targets only
libraries of its type, libraries no row targets are never even indexed, the pool is filtered, and the
per-section loop filters again. Disabling two of them at once still produced no leak, so these two
assertions are correct but their teeth are unproven; the redundancy is the actual guarantee.

The template values are parsed out of ``web/src/lib/row-templates.ts`` rather than restated here, so
this cannot silently drift from what actually ships. If that file's shape changes, the parser fails
loudly (see :func:`_load_templates`) instead of quietly testing nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shortlist.engine.context import EngineContext
from shortlist.engine.models import EngineConfig, MediaType, RowSpec
from tests.conftest import MemorySnapshotStore, fake_media_item, make_profile, make_watched, plextv_user

# The `client` fixture comes from tests/integration/conftest.py — the same app fixture the
# `test_api_*.py` files use, so the two can never drift apart.

TEMPLATES_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "row-templates.ts"

#: Every template in the gallery. Kept here so a NEW template cannot be added without either
#: extending the proofs below or deliberately deleting its name from this list.
EXPECTED_IDS = {
    "picked-for-you",
    "because-you-watched",
    "seen-it-already",
    "fresh-finds",
    "from-the-vault",
    "popular-here",
    "movie-night",
    "more-tv",
}


def _load_templates() -> dict[str, dict]:
    """`id -> values` straight out of the TypeScript source.

    A deliberately strict little parser: the file is a static literal, so anything it cannot read is a
    change in shape that should FAIL rather than yield an empty dict a test would happily pass on.
    """
    source = TEMPLATES_TS.read_text(encoding="utf-8")
    # Strip `//` comments (none of the literal's string values contain "//").
    source = re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)

    out: dict[str, dict] = {}
    for block in re.finditer(r'id:\s*"([^"]+)",(.*?)values:\s*\{(.*?)\n    \},', source, re.DOTALL):
        template_id, _meta, values_src = block.group(1), block.group(2), block.group(3)
        # JS object literal -> JSON: quote the bare keys and drop the trailing comma. Values are left
        # exactly as written — the file double-quotes every string, and normalising quote style would
        # corrupt the apostrophes inside them ("you've already seen", "Tonight's").
        body = re.sub(r"^(\s*)(\w+):", r'\1"\2":', values_src, flags=re.MULTILINE)
        body = re.sub(r",\s*$", "", body.strip())
        try:
            out[template_id] = json.loads("{" + body + "}")
        except json.JSONDecodeError as e:  # pragma: no cover - only on a shape change
            raise AssertionError(f"could not parse template {template_id!r} from {TEMPLATES_TS}: {e}") from e

    assert set(out) == EXPECTED_IDS, (
        f"parsed {sorted(out)} but expected {sorted(EXPECTED_IDS)} — the gallery changed, so these "
        "proofs must be updated to match rather than left asserting a stale set"
    )
    return out


TEMPLATES = _load_templates()


#: API field -> RowSpec field, for the two the layers deliberately name differently.
def _spec(template_id: str, **overrides) -> RowSpec:
    """The template's values as the RowSpec the engine actually runs.

    `build: "shared"` is the API's spelling of the engine's `shared: True`; everything else is
    one-for-one. `context_builder` does the same translation for real rows, so an unmapped key here
    means a field the engine cannot honour — which is exactly what this file is for.
    """
    values = {k: v for k, v in TEMPLATES[template_id].items() if k != "name"}
    build = values.pop("build", "per_person")

    unsupported = [k for k in values if not hasattr(RowSpec, k) and k not in RowSpec.__annotations__]
    assert not unsupported, f"{template_id}: the engine has no setting for {unsupported}"

    return RowSpec(
        slug=template_id.replace("-", "_"),
        name_template=TEMPLATES[template_id]["name"],
        shared=(build == "shared"),
        **values,
        **overrides,
    )


class TestEveryTemplateSaves:
    """Layer 1: every option a template sets exists all the way through the API."""

    @pytest.mark.parametrize("template_id", sorted(EXPECTED_IDS))
    def test_the_api_accepts_it_and_gives_it_back_unchanged(self, template_id: str, client):
        values = TEMPLATES[template_id]

        created = client.post("/api/collections", json=values)
        assert created.status_code == 201, f"{template_id}: {created.text}"

        body = created.json()
        for field, expected in values.items():
            assert body[field] == expected, f"{template_id}: {field} came back as {body[field]!r}, sent {expected!r}"

    @pytest.mark.parametrize("template_id", sorted(EXPECTED_IDS))
    def test_it_survives_a_reload(self, template_id: str, client):
        """Round-tripping through the response object alone would pass even if the column didn't
        exist — the value could be echoed from the request. Re-reading proves it was persisted."""
        values = TEMPLATES[template_id]
        cid = client.post("/api/collections", json=values).json()["id"]

        reloaded = next(c for c in client.get("/api/collections").json() if c["id"] == cid)
        for field, expected in values.items():
            assert reloaded[field] == expected, f"{template_id}: {field} did not persist"


@pytest.fixture
def engine_ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    """A context whose library holds two movies and two shows, so any template can be exercised."""
    plex = MagicMock()
    movie_section, show_section = MagicMock(), MagicMock()
    movie_section.type, movie_section.title, movie_section.key = "movie", "Movies", "1"
    show_section.type, show_section.title, show_section.key = "show", "TV Shows", "2"
    for sec in (movie_section, show_section):
        sec.collections.return_value = []
    plex.sections.return_value = [movie_section, show_section]
    plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section, MediaType.SHOW: show_section}
    plex.build_library_index.return_value = {
        900: 999,
        800: 998,  # the SHOW seed — without this no show is ever searched from
        901: 9901,
        902: 9902,
        903: 9903,
        10: 1010,
        20: 1020,
        30: 1030,
        40: 1040,
    }
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = []
    plex.stored_label.side_effect = lambda collection, label: label.replace("shortlist", "Shortlist", 1)
    # (items, missing) — see PlexClient.fetch_items: a partial batch drops dead keys silently.
    plex.fetch_items.side_effect = lambda keys: ([fake_media_item(k, f"item{k}") for k in keys], [])

    history = MagicMock()
    mock_tmdb.genre_names.return_value = {}

    def put(account_id, fields):
        for u in mock_plextv.users:
            if u.id == account_id:
                u.filters.update(fields)

    mock_plextv.update_user_filters.side_effect = put

    return EngineContext(
        config=engine_config,
        plex=plex,
        plextv=mock_plextv,
        tmdb=mock_tmdb,
        history_source=history,
        curator=mock_curator,
        snapshots=MemorySnapshotStore(),
    )


def _movies(*ids):
    return [
        ({"id": i, "title": f"Movie {i}", "genre_ids": [], "vote_average": 8.0 - n}, 1.0) for n, i in enumerate(ids)
    ]


def _shows(*ids):
    return [({"id": i, "name": f"Show {i}", "genre_ids": [], "vote_average": 8.0 - n}, 1.0) for n, i in enumerate(ids)]


def _both_types(tmdb_id: int, media_type: MediaType):
    """`suggestions` keyed by the SEED's type — candidates inherit it, so this is the only way to get
    movies AND shows into one pool."""
    return _movies(10, 20) if media_type is MediaType.MOVIE else _shows(30, 40)


def _mixed_history():
    """Enough history to clear cold start, seeded by one MOVIE and one SHOW."""
    return [
        *[make_watched(f"Film {i}", days_ago=i, tmdb_id=900 + i) for i in range(1, 4)],
        make_watched("A Show", days_ago=5, media_type=MediaType.SHOW, tmdb_id=800, leaf_count=10),
    ]


def _picks_by_row(report) -> dict[str, list]:
    out: dict[str, list] = {}
    for pick in sorted(report.users[0].picks, key=lambda p: p.rank):
        out.setdefault(pick.collection_slug, []).append(pick)
    return out


class TestEveryTemplateDelivers:
    """Layer 2: the template's own values, through the real engine, produce what the tile claims."""

    def test_picked_for_you_gives_each_person_their_own_row(self, engine_ctx, mock_plextv):
        """Claim: "One row each"."""
        import shortlist.engine.pipeline as pipeline_mod

        engine_ctx.history_source.fetch.return_value = [
            make_watched("Seed", days_ago=i, rating_key=999) for i in range(1, 5)
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(10, 20)
        engine_ctx.config.rows = [_spec("picked-for-you")]
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]

        report = pipeline_mod.run(
            engine_ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)]
        )

        assert [u.username for u in report.users] == ["sarah", "mike"]
        assert all(u.picks for u in report.users), "each person gets their own picks, not a shared row"

    def test_because_you_watched_is_built_from_exactly_one_watch_and_named_after_it(self, engine_ctx, mock_plextv):
        """Claims: "Built from 1 watch" and "Named after that title" — the two that must agree, or the
        row names one film and fills itself from thirty others."""
        import shortlist.engine.pipeline as pipeline_mod
        from shortlist.engine.delivery import render_row_name

        engine_ctx.config.max_seeds = 30  # the global default the template must override
        # FOUR distinct watches: uncapped this is four searches, so "one" can only mean max_seeds=1.
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Watch {i}", days_ago=i, tmdb_id=900 + i) for i in range(1, 5)
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(10, 20)
        spec = _spec("because-you-watched")
        engine_ctx.config.rows = [spec]
        mock_plextv.users = [plextv_user(100, "sarah")]
        profile = make_profile("sarah", account_id=100)

        report = pipeline_mod.run(engine_ctx, [profile])

        # ONE seed searched, not the whole history: one suggestions() call.
        assert engine_ctx.tmdb.suggestions.call_count == 1, (
            "the row must be built from ONE watch — four were available, so anything more means the "
            "title names one film while the contents come from the others"
        )
        # And the title renders that seed's REAL name — the same call delivery makes, so this is the
        # string Plex would actually show, not a reconstruction of it.
        picks = report.users[0].picks
        assert picks, "the row delivered nothing"
        rendered = render_row_name(spec.name_template, profile, picks, "Movies")
        assert "{top_seed}" not in rendered, f"the placeholder must resolve, got {rendered!r}"
        assert picks[0].seed_title and picks[0].seed_title in rendered, (
            f"the row must be named after the watch it was built from — {rendered!r} vs seed {picks[0].seed_title!r}"
        )

    def test_happy_to_see_again_leads_with_something_already_watched(self, engine_ctx, mock_plextv):
        """Claim: "Rewatches come first". This is the one `watched_pct` could never deliver — it is a
        ceiling, so it permits finished titles but never PROMOTES them."""
        import shortlist.engine.pipeline as pipeline_mod

        engine_ctx.config.max_seeds = 1
        engine_ctx.history_source.fetch.return_value = [
            *[make_watched("Seed", days_ago=i, rating_key=999) for i in range(1, 5)],
            # Already finished, and the LOWEST rated — only the rewatch preference can put it first.
            make_watched("Movie 20", days_ago=8, tmdb_id=20),
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(10, 20)
        engine_ctx.config.rows = [_spec("seen-it-already")]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(engine_ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in _picks_by_row(report)["seen_it_already"]]
        assert delivered, "the row delivered nothing"
        assert delivered[0] == 20, f"an already-watched title must LEAD the row, got {delivered}"

    def test_fresh_finds_delivers_nothing_already_watched_and_rebuilds_nightly(self, engine_ctx, mock_plextv):
        """Claims: "Rebuilds nightly" and "Nothing already watched"."""
        import shortlist.engine.pipeline as pipeline_mod
        from shortlist.engine.rows import _is_refresh_night

        engine_ctx.config.max_seeds = 1
        engine_ctx.history_source.fetch.return_value = [
            *[make_watched("Seed", days_ago=i, rating_key=999) for i in range(1, 5)],
            make_watched("Movie 20", days_ago=8, tmdb_id=20),  # finished
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(10, 20)
        spec = _spec("fresh-finds")
        engine_ctx.config.rows = [spec]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(engine_ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in _picks_by_row(report)["fresh_finds"]]
        assert 20 not in delivered, "a finished title reached a row that promises nothing already watched"
        # "Rebuilds nightly" is a cadence of 1 -> a refresh EVERY day, whatever the day number.
        assert all(_is_refresh_night(spec.slug, "sarah", day, spec.refresh_days) for day in range(1, 30))

    def test_from_the_vault_never_rebuilds_on_its_own(self, engine_ctx):
        """Claim: "Never rebuilds on its own"."""
        from shortlist.engine.rows import _is_refresh_night

        spec = _spec("from-the-vault")
        assert spec.refresh_days == 0
        assert not any(_is_refresh_night(spec.slug, "sarah", day, spec.refresh_days) for day in range(1, 400))

    def test_popular_on_this_server_is_shared_and_needs_several_watchers(self, engine_ctx, mock_plextv):
        """Claims: "Shared with everyone" and "Needs 3 watchers" — a title one person watched must not
        reach a public row."""
        import shortlist.engine.pipeline as pipeline_mod

        spec = _spec("popular-here")
        assert spec.shared, "the tile says shared with everyone"
        assert spec.min_watchers == 3, "the tile says it needs 3 watchers"

        engine_ctx.tmdb.suggestions.return_value = _movies(10, 20)
        # THREE people watched title 900; only sarah watched 555. At min_watchers=3 exactly one title
        # clears the floor, so the count itself proves the floor was applied.
        everyone = [make_watched("Everyone Watched", days_ago=1, tmdb_id=900)]
        engine_ctx.history_source.fetch.side_effect = lambda user, **kw: (
            [*everyone, make_watched("Only Sarah", days_ago=2, tmdb_id=555)]
            if user.username == "sarah"
            else list(everyone)
        )
        engine_ctx.config.rows = [spec]
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike"), plextv_user(300, "amy")]

        report = pipeline_mod.run(
            engine_ctx,
            [
                make_profile("sarah", account_id=100),
                make_profile("mike", account_id=200),
                make_profile("amy", account_id=300),
            ],
        )

        shared_reports = [u for u in report.users if u.slug.startswith("shared")]
        assert shared_reports, "a shared row must build ONE 'Everyone' row, not a row per person"
        assert shared_reports[0].counts.history == 1, (
            "exactly one title had 3 watchers; sarah's solo watch must not have reached a public row"
        )

    def test_movie_night_is_movies_only_ten_picks_and_weekly(self, engine_ctx, mock_plextv):
        """Claims: "Movies only", "10 picks", "Weekly"."""
        import shortlist.engine.pipeline as pipeline_mod

        spec = _spec("movie-night")
        assert spec.media == "movie"
        assert spec.size == 10
        assert spec.refresh_days == 7, "the tile says weekly, so the cadence must BE weekly"

        # The pool holds movies AND shows, so "movies only" has something real to exclude.
        engine_ctx.history_source.fetch.return_value = _mixed_history()
        engine_ctx.tmdb.suggestions.side_effect = _both_types
        engine_ctx.config.rows = [spec]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(engine_ctx, [make_profile("sarah", account_id=100)])

        picks = _picks_by_row(report)["movie_night"]
        assert picks, "the row delivered nothing"
        assert all(p.media_type is MediaType.MOVIE for p in picks), (
            f"a shows title reached a movies-only row: {[(p.tmdb_id, p.media_type) for p in picks]}"
        )

    def test_more_tv_to_watch_excludes_a_series_already_started(self, engine_ctx, mock_plextv):
        """Claims: "TV only" and "Never started" — the second is stricter than the normal filter, which
        only drops shows a person has FINISHED."""
        import shortlist.engine.pipeline as pipeline_mod

        engine_ctx.history_source.fetch.return_value = [
            *_mixed_history(),
            # One episode of forty: STARTED, nowhere near finished, so only `unstarted_only` excludes it.
            make_watched(
                "Show 30", days_ago=8, media_type=MediaType.SHOW, tmdb_id=30, viewed_leaf_count=1, leaf_count=40
            ),
        ]
        engine_ctx.tmdb.suggestions.side_effect = _both_types
        engine_ctx.config.rows = [_spec("more-tv")]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(engine_ctx, [make_profile("sarah", account_id=100)])

        picks = _picks_by_row(report)["more_tv"]
        delivered = [p.tmdb_id for p in picks]
        assert delivered, "the row delivered nothing"
        assert all(p.media_type is MediaType.SHOW for p in picks), "a movie reached a TV-only row"
        assert 30 not in delivered, "a series they had already started reached a 'to start' row"
        assert 40 in delivered
