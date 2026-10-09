"""Shelf placement and sequencing, and library scoping of a run."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from collections import Counter
from unittest.mock import MagicMock

from hypothesis import given
from hypothesis import strategies as st

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.clients.tmdb import NullCache
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import (
    render_row_name,
    resolve_row_template,
    row_marker,
)
from shortlist.engine.models import (
    MediaType,
    RowSpec,
)
from tests.conftest import make_profile, make_watched, plextv_user
from tests.unit.pipeline_support import (
    _ranked,
    ctx,  # noqa: F401
)


class TestPlacement:
    """Per-row placement (Home / Library / Both) and pin-to-top reach promote() with the right flags."""

    def _pick(self, slug: str):
        from shortlist.engine.models import MediaType, Pick

        return Pick(
            tmdb_id=1, rating_key=10, title="t1", rank=1, reason="", media_type=MediaType.MOVIE, collection_slug=slug
        )

    def test_library_placement_promotes_recommended_only(self, ctx: EngineContext):
        from shortlist.engine.models import RowSpec
        from shortlist.engine.pipeline import _promote_one

        _promote_one(ctx, MagicMock(), RowSpec(slug="x", name_template="", size=10, placement="library"))
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": True,
        }

    def test_a_legacy_pinned_row_is_promoted_without_being_positioned(self, ctx: EngineContext):
        """`pin_top` no longer reaches `promote`.

        It used to add one `move(after=None)` per collection per run — the "to the top" primitive
        `place_rows` documents as unusable alone, because a built-in stuck at the minimum float makes
        everything sent above it land ON that value. It also fired with shelf ordering switched OFF,
        contradicting that setting. Placement owns position now, and an unconfigured library already
        defaults to the top, so nothing is lost by ignoring the flag.
        """
        from shortlist.engine.models import RowSpec
        from shortlist.engine.pipeline import _promote_one

        _promote_one(ctx, MagicMock(), RowSpec(slug="x", name_template="", size=10, placement="home", pin_top=True))

        assert ctx.plex.promote.call_args.kwargs == {"shared": True, "home": True, "recommended": False}
        assert "pin_top" not in ctx.plex.promote.call_args.kwargs

    def test_recommended_is_chosen_per_collection_not_ored_across_audiences(self, ctx: EngineContext):
        """The Recommended flag comes from WHOSE row the collection is (issue #6).

        Everyone gets their OWN collection, so Plex's single `promotedToRecommended` is set per
        collection and the owner/friends split is real. The old code OR'd both placements into one
        flag, so "friends: Recommended on" silently dragged the owner's row onto the shelf too —
        and left the owner with no way to un-clutter their own shelf.
        """
        from shortlist.engine.models import RowSpec, UserType
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, placement="home", placement_friends="both")

        _promote_one(ctx, MagicMock(), spec, UserType.OWNER)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": True,
            "recommended": False,
        }

        _promote_one(ctx, MagicMock(), spec, UserType.SHARED)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": True,
            "home": False,
            "recommended": True,
        }

    def test_owner_keeps_the_shelf_while_friends_rows_stay_off_it(self, ctx: EngineContext):
        """The inverse split: the owner's row on their Recommended shelf, friends' rows only on
        Friends' Home. This is the config that keeps the owner's shelf to just their own row."""
        from shortlist.engine.models import RowSpec, UserType
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, placement="both", placement_friends="home")

        _promote_one(ctx, MagicMock(), spec, UserType.OWNER)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": True,
            "recommended": True,
        }

        _promote_one(ctx, MagicMock(), spec, UserType.SHARED)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": True,
            "home": False,
            "recommended": False,
        }

    def test_a_managed_user_collection_uses_the_friends_side(self, ctx: EngineContext):
        """A managed user takes the FRIENDS-side flags, never the owner's.

        Plex's docs are explicit: Home (``promotedToOwnHome``) "applies to the server owner", while
        Shared Users' Home (``promotedToSharedHome``) "applies to all shared users, including
        managed users" — https://support.plex.tv/articles/manage-recommendations/. Routing a managed
        user through the owner flag hides their row from them and puts it on the OWNER's Home.

        The owner side is deliberately "off" here, so reading the wrong side is unmissable.
        """
        from shortlist.engine.models import RowSpec, UserType
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, placement="off", placement_friends="both")

        _promote_one(ctx, MagicMock(), spec, UserType.MANAGED)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": True,
            "home": False,
            "recommended": True,
        }

        # The owner, on the same spec, gets nothing — proving the two sides really are independent.
        _promote_one(ctx, MagicMock(), spec, UserType.OWNER)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": False,
        }

    def test_an_unmapped_managed_collection_never_lands_on_the_owners_home(self, ctx: EngineContext):
        """The no-spec fallback must respect the same split — it used to hand MANAGED `home=True`,
        which is the owner's shelf."""
        from shortlist.engine.models import UserType
        from shortlist.engine.pipeline import _promote_one

        collection = MagicMock()
        _promote_one(ctx, collection, None, UserType.MANAGED)
        ctx.plex.promote.assert_called_with(collection, shared=True, home=False, recommended=False)

    def test_promotion_reaches_a_library_this_run_no_longer_targets(self, ctx: EngineContext):
        """`delivery_sections` is narrowed to libraries some row currently targets, so a row whose
        `library_keys` was narrowed left its old collection stranded in the dropped library — never
        re-promoted (out of scope) and never demoted either, keeping its surfaces indefinitely."""
        from datetime import UTC, datetime

        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        movies = MagicMock(type="movie", key="1", title="Movies")
        dropped = MagicMock(type="movie", key="2", title="4K Movies")  # no row targets this any more
        ctx.plex.sections.return_value = [movies, dropped]
        ctx.delivery_sections = [movies]
        ctx.config.rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=5, library_keys=["1"])]
        ctx.config.dry_run = False
        stranded = MagicMock(title="Hidden Gems (left behind)")
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [stranded] if s is dropped else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once()  # reached despite living outside delivery_sections
        assert ctx.plex.promote.call_args.args[0] is stranded  # promoted the STRANDED collection, not a fallback

    def test_a_stranded_collection_resolves_to_its_row_rather_than_the_fallback(self, ctx: EngineContext):
        """Regression: widening promotion to every library made this WORSE before it made it better.

        The fallback title map used to be rendered only for the libraries a row targets NOW, so a
        collection left in a de-targeted library could be reached but never identified — and took the
        no-spec fallback on EVERY run, turning Friends' Home on for a row switched fully off. The map
        is now rendered across every library of the row's media type.
        """
        from datetime import UTC, datetime

        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        movies = MagicMock(type="movie", key="1", title="Movies")
        dropped = MagicMock(type="movie", key="2", title="4K Movies")  # row no longer targets this
        ctx.plex.sections.return_value = [movies, dropped]
        ctx.delivery_sections = [movies]
        ctx.config.rows = [
            RowSpec(
                slug="gems",
                name_template="{library_name} Gems",
                size=5,
                library_keys=["1"],
                placement="off",
                placement_friends="off",
            )
        ]
        ctx.config.dry_run = False
        stranded = MagicMock(title="4K Movies Gems" + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [stranded] if s is dropped else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        # Its real spec is off/off, so it claims nothing — NOT the fallback's shared=True.
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": False,
        }

    def test_a_helper_a_stopped_run_left_behind_is_never_promoted(self, ctx: EngineContext):
        """It carries the person's label and matches no row, so the no-spec fallback below would put it on
        their Home every night until the sweep removes it."""
        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import UserType
        from shortlist.engine.pipeline import _promote_one

        helper = MagicMock()
        helper.title = f"Shortlist freed name 0123456789ab{row_marker(7)}"
        for user_type in (UserType.OWNER, UserType.SHARED, None):
            _promote_one(ctx, helper, None, user_type)

        ctx.plex.promote.assert_not_called()

    def test_the_no_spec_fallback_never_forces_a_row_onto_the_recommended_shelf(self, ctx: EngineContext):
        """A row whose title can't be mapped back to its spec takes the fallback, which used to
        default `recommended=True`.

        That is the one surface where the OWNER sees every row — no share filter can hide it from
        them — so defaulting it on put rows the operator had switched fully off onto the owner's
        shelf. The Home flags stay on by design: this branch is common, not rare, and turning them
        off there made every row in the full-stack suite disappear.
        """
        from shortlist.engine.models import UserType
        from shortlist.engine.pipeline import _promote_one

        collection = MagicMock()
        for user_type in (UserType.OWNER, UserType.MANAGED, UserType.SHARED, None):
            _promote_one(ctx, collection, None, user_type)
            assert ctx.plex.promote.call_args.kwargs.get("recommended") is False, user_type

        # And it still never puts someone else's row on the owner's Home.
        for user_type in (UserType.MANAGED, UserType.SHARED):
            _promote_one(ctx, collection, None, user_type)
            assert ctx.plex.promote.call_args.kwargs.get("home") is False, user_type

    def test_off_placement_claims_no_surface(self, ctx: EngineContext):
        """ "off" turns every surface off for that audience. The collection still exists and is still
        browse-hidden by promote()'s unconditional modeUpdate, so it lives in the Collections tab."""
        from shortlist.engine.models import RowSpec, UserType
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, placement="off", placement_friends="off")

        for user_type in (UserType.OWNER, UserType.MANAGED, UserType.SHARED):
            _promote_one(ctx, MagicMock(), spec, user_type)
            assert ctx.plex.promote.call_args.kwargs == {
                "shared": False,
                "home": False,
                "recommended": False,
            }, user_type

    def test_a_shared_row_unions_both_audiences(self, ctx: EngineContext):
        """A SHARED row is ONE public collection rather than one per person, so there is no "whose
        row is this" to split on — it takes both Home flags and either side's Recommended."""
        from shortlist.engine.models import RowSpec
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, shared=True, placement="home", placement_friends="library")

        _promote_one(ctx, MagicMock(), spec)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": True,
            "recommended": True,
        }

    def test_an_unmatched_collection_is_hidden_from_browse_and_claims_nothing_else(self, ctx: EngineContext):
        """A collection whose title we can't map to a row must still be browse-hidden — never left
        half-promoted and visible to everyone.

        It claims NO other surface: the fallback used to default `recommended=True`, which forced a
        row onto the Recommended shelf regardless of its placement, so a row the operator had
        switched fully off reappeared there. Under-showing a row for one run is recoverable;
        silently overriding "off" is not.
        """
        from shortlist.engine.pipeline import _promote_one

        collection = MagicMock()
        _promote_one(ctx, collection, None)
        ctx.plex.promote.assert_called_once_with(collection, shared=True, recommended=False)

    def test_undelivered_static_library_only_row_keeps_its_placement(self, ctx: EngineContext):
        """INT-3: a STATIC-titled 'Library only' row that exists but got no picks this run keeps its
        library-only placement — it must NOT fall to the everywhere-visible default and pop onto Home
        for that one run (the promote-phase fallback maps it to its spec by its stable title)."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=10, placement="library")]
        ctx.config.dry_run = False
        section = MagicMock(type="movie", key="1", title="Movies")
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        coll = MagicMock(title=render_row_name("Hidden Gems", user, []) + row_marker(100))  # exists, no picks
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": True,
        }

    def test_undelivered_library_name_row_maps_each_library_to_its_spec(self, ctx: EngineContext):
        """The default {library_name} row renders a DIFFERENT title per library, so an undelivered but
        still-lingering copy must map to its spec in EACH library — not fall to the everywhere-visible
        default in the libraries the fallback didn't render. Both keep the row's library-only placement."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        tpl = "✨ {library_name} Picked for You"
        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [RowSpec(slug="picked", name_template=tpl, size=10, placement="library")]
        ctx.config.dry_run = False
        movies = MagicMock(type="movie", key="1", title="Movies")
        shows = MagicMock(type="show", key="2", title="TV Shows")
        ctx.delivery_sections = [movies, shows]
        ctx.plex.sections.return_value = [movies, shows]
        colls = {
            movies: MagicMock(title=render_row_name(tpl, user, [], library_name="Movies") + row_marker(100)),
            shows: MagicMock(title=render_row_name(tpl, user, [], library_name="TV Shows") + row_marker(100)),
        }
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [colls[s]] if s in colls else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        assert ctx.plex.promote.call_count == 2  # each library's lingering row mapped to its spec
        for call in ctx.plex.promote.call_args_list:
            # placement="library" -> hidden from Home, shown only in the library's Recommended shelf.
            assert call.kwargs == {"shared": False, "home": False, "recommended": True}

    def test_an_undelivered_dynamic_titled_row_keeps_the_safe_fallback(self, ctx: EngineContext):
        """A {top_seed} row's title can't be predicted without picks, so an un-delivered one has no
        entry in the title map and takes the no-spec fallback.

        That is the right outcome, not a gap: the fallback browse-hides, gives each audience its own
        Home flag, and — crucially — does NOT claim the Recommended shelf, the one surface the owner
        cannot filter. Resolving it by label instead would mis-map a DISABLED row's leftover
        collection onto whichever row the user still has enabled.
        """
        from datetime import UTC, datetime

        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="dyn", name_template="Because you watched {top_seed}", size=10, placement="library")
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        coll = MagicMock(title="Because you watched Dune (from a prior run)")
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(coll, shared=True, home=False, recommended=False)

    def test_an_unmanaged_rows_config_still_resolves_its_default_row(self, ctx: EngineContext):
        """With no rows configured the engine synthesizes a legacy default spec, and every other
        phase builds from it. Promotion used to read the RAW `config.rows` instead, so the title map
        was empty, every lookup missed, and placement was silently ignored for the whole server."""
        from datetime import UTC, datetime

        from shortlist.engine.models import RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = []
        ctx.config.rows_defined = False  # unmanaged -> default_row_spec() is synthesized
        ctx.config.dry_run = False
        section = MagicMock()
        section.title = "Movies"
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        default_spec = ctx.config.per_person_rows()[0]
        title = render_row_name(
            resolve_row_template(default_spec, user, ctx.config), user, [], library_name="Movies"
        ) + row_marker(user.plex_account_id)
        coll = MagicMock(title=title)
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        # Resolved to the real spec (default placement "both") — NOT the no-spec fallback, which
        # would have withheld the Recommended shelf.
        assert ctx.plex.promote.call_args.kwargs["recommended"] is True

    def test_fallback_skips_a_row_this_user_is_not_in_the_audience_for(self, ctx: EngineContext):
        """Audience is honoured by the no-picks fallback: a per-person row this user is excluded from
        must never be handed a Home/Library placement for them. It stays on the unmapped safe fallback
        (per-person rows share the same marker, so this audience skip is the ONLY thing protecting it)."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="gems", name_template="Hidden Gems", size=10, placement="library", audience={999})
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        coll = MagicMock(title=render_row_name("Hidden Gems", user, []) + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(
            coll, shared=True, home=False, recommended=False
        )  # excluded → NOT mapped; friend → no home

    def _same_title_in_two_libraries(self, ctx: EngineContext):
        """Issue #121's shape: a Movies-only row and a TV-only row of one person, both titled "Friday",
        with DIFFERENT placements — so a collection handed the other row's spec is visibly wrong."""
        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import RowSpec, UserProfile, UserType

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="friday_movies", name_template="Friday", size=10, media="movie", placement="home"),
            RowSpec(slug="friday_tv", name_template="Friday", size=10, media="show", placement="library"),
        ]
        ctx.config.dry_run = False
        movies = MagicMock(type="movie", key="1", title="Movies")
        shows = MagicMock(type="show", key="2", title="TV Shows")
        ctx.delivery_sections = [movies, shows]
        ctx.plex.sections.return_value = [movies, shows]
        movies_c = MagicMock(title="Friday" + row_marker(100), ratingKey=11)
        shows_c = MagicMock(title="Friday" + row_marker(100), ratingKey=22)
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [movies_c] if s is movies else [shows_c]
        return user, movies_c, shows_c

    def _flags(self, ctx: EngineContext) -> dict:
        return {c.args[0].ratingKey: c.kwargs for c in ctx.plex.promote.call_args_list}

    def test_two_rows_sharing_a_title_in_different_libraries_each_keep_their_own_placement(self, ctx: EngineContext):
        """The rendered-title fallback was keyed by title alone, so the first row claimed BOTH
        collections and the TV row's "library only" was silently replaced by the Movies row's Home."""
        from datetime import UTC, datetime

        from shortlist.engine.models import RunReport, UserRunReport
        from shortlist.engine.pipeline import _promote_phase

        user, _movies_c, _shows_c = self._same_title_in_two_libraries(ctx)
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        assert self._flags(ctx) == {
            11: {"shared": True, "home": False, "recommended": False},
            22: {"shared": False, "home": False, "recommended": True},
        }

    def test_recorded_placement_titles_are_told_apart_by_library(self, ctx: EngineContext):
        """What a run stamps while delivering. Keyed by title alone, the second row overwrote the first's
        entry, and the TV row's placement then governed the Movies collection too."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import RunReport, UserRunReport
        from shortlist.engine.pipeline import _promote_phase

        user, _movies_c, _shows_c = self._same_title_in_two_libraries(ctx)
        title = "Friday" + row_marker(100)
        recorded = UserRunReport(username="sarah", slug="sarah")
        recorded.placement_titles = {("1", title): "friday_movies", ("2", title): "friday_tv"}
        report = RunReport(started_at=datetime.now(UTC), users=[recorded])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        assert self._flags(ctx) == {
            11: {"shared": True, "home": False, "recommended": False},
            22: {"shared": False, "home": False, "recommended": True},
        }

    def test_fallback_leaves_shared_rows_to_the_shared_promote_loop(self, ctx: EngineContext):
        """A shared row must never be picked up by the PER-PERSON fallback (it promotes in the separate
        shared loop). Even if a collection under this user's label matched the title the fallback would
        compute, the `spec.shared` skip keeps it on the unmapped safe fallback, not the shared spec's
        Home placement."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="all", name_template="Everyone's Picks", size=10, placement="home", shared=True)
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        coll = MagicMock(title=render_row_name("Everyone's Picks", user, []) + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(
            coll, shared=True, home=False, recommended=False
        )  # shared spec skipped → NOT mapped; friend → no home

    def test_a_top_seed_row_records_a_placement_title_per_library(self, ctx: EngineContext, mock_plextv):
        """A {top_seed} row spanning two libraries writes a DIFFERENT title in each (each curated from
        its own contents), so promotion must know both — not just the first. The recorded titles must
        match what the collections are actually delivered as, or every library but the first would fall
        back to the legacy everywhere-visible placement."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        movies_4k = MagicMock(type="movie", key="2", title="4K Movies")
        ctx.plex.sections.return_value = [movies, movies_4k]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        # Two seeds; each library holds candidates from a DIFFERENT seed, so its {top_seed} differs:
        # Movies is fed by Fargo (ids 10-15), 4K by Heat (ids 50-55).
        idx_std = {900: 999, 800: 888, **{i: 1000 + i for i in range(10, 16)}}
        idx_4k = {900: 999, 800: 888, **{i: 2000 + i for i in range(50, 56)}}
        ctx.plex.build_library_index.side_effect = lambda sec: idx_std if sec is movies else idx_4k

        def suggestions(tid, mt):  # returns (item, affinity) pairs
            base = 10 if tid == 900 else 50  # Fargo -> Movies ids, Heat -> 4K ids
            return _ranked(
                [{"id": base + i, "title": f"T{base + i}", "genre_ids": [], "vote_average": 8.0} for i in range(6)]
            )

        ctx.tmdb.suggestions.side_effect = suggestions
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),  # tmdb 900
            make_watched("Heat", days_ago=2, rating_key=888),  # tmdb 800
        ]
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="Because you watched {top_seed}", size=5, media="movie")
        ]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        # Capture the titles delivery actually writes so we can compare to what was recorded.
        delivered_titles: list[str] = []
        ctx.plex.create_collection.side_effect = lambda section, title, items: (
            delivered_titles.append(title) or MagicMock()
        )
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        recorded = {title for _library, title in report.users[0].placement_titles}
        # Two libraries with different top seeds -> two distinct titles; the pre-fix code recorded ONE
        # (union) and left the 4K collection unmatched. Every delivered title must be recorded.
        assert len(recorded) == 2, f"expected a distinct title per library, got {recorded}"
        assert set(delivered_titles) == recorded, "recorded titles must match what delivery wrote"
        assert all(slug == "picked" for slug in report.users[0].placement_titles.values())


class _DictCache:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


class TestLibraryScoping:
    """Only the libraries a row targets are read — an unselected/off-type library is never scanned."""

    def test_reads_only_libraries_a_row_targets(self, ctx: EngineContext):
        from shortlist.engine.models import RowSpec

        movies = MagicMock(type="movie", key="1", title="Movies")
        sports = MagicMock(type="movie", key="2", title="Sports")  # unselected by the row below
        shows = MagicMock(type="show", key="3", title="TV Shows")  # wrong media for a movie row
        ctx.config.rows = [RowSpec(slug="m", name_template="Movies", size=5, media="movie", library_keys=["1"])]
        ctx.config.rows_defined = True
        ctx.plex.section_signature.return_value = None  # force a scan (no cache)
        scanned: list[str] = []
        ctx.plex.build_library_index.side_effect = lambda sec: scanned.append(str(sec.key)) or {}

        pipeline_mod._build_indexes(ctx, [make_profile("sarah", account_id=100)], [movies, sports, shows])

        assert scanned == ["1"]  # Movies only — Sports and TV Shows never read
        assert [str(s.key) for s in ctx.delivery_sections] == ["1"]

    def test_a_run_with_no_users_names_the_libraries_without_scanning_them(self, ctx: EngineContext):
        """`engine_run(ctx, [])` still has to know WHERE rows live — it just must not read their contents.

        These are two questions and this used to answer both with one list: with no users
        `delivery_sections` came back EMPTY, so the shelf-ordering phase iterated nothing and every
        `privacy.sync` — the nightly job and the "Fix privacy" button — silently reordered nothing
        at all, whatever else it was asked to do (a large production server, 2026-08-12). Indexing stays gated on users
        because that is the part that costs thousands of PMS reads.
        """
        from shortlist.engine.models import RowSpec

        movies = MagicMock(type="movie", key="1", title="Movies")
        sports = MagicMock(type="movie", key="2", title="Sports")  # no row targets it
        ctx.config.rows = [RowSpec(slug="m", name_template="Movies", size=5, media="movie", library_keys=["1"])]
        ctx.config.rows_defined = True
        ctx.plex.section_signature.return_value = None
        scanned: list[str] = []
        ctx.plex.build_library_index.side_effect = lambda sec: scanned.append(str(sec.key)) or {}

        pipeline_mod._build_indexes(ctx, [], [movies, sports])

        assert [str(s.key) for s in ctx.delivery_sections] == ["1"]  # the shelf phase has a library
        assert scanned == []  # and not one item was read

    def test_unconfigured_run_still_reads_every_library(self, ctx: EngineContext):
        """No rows configured -> the synthesized default row targets everything, so all libraries read."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        shows = MagicMock(type="show", key="2", title="TV Shows")
        ctx.config.rows = []
        ctx.config.rows_defined = False
        ctx.plex.section_signature.return_value = None
        scanned: list[str] = []
        ctx.plex.build_library_index.side_effect = lambda sec: scanned.append(str(sec.key)) or {}

        pipeline_mod._build_indexes(ctx, [make_profile("sarah", account_id=100)], [movies, shows])

        assert sorted(scanned) == ["1", "2"]

    def test_muted_row_cleanup_scans_a_library_the_run_scoped_out(self, ctx: EngineContext):
        """A muted row whose stale copy lives in a de-targeted library is still removed — cleanup scans
        EVERY library, not the run's (targeting-scoped) delivery_sections."""
        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import CollectionDiff, RowOverride, RowSpec, UserRunReport
        from shortlist.engine.rows import _remove_muted_and_retired

        movies = MagicMock(type="movie", key="1", title="Movies")
        old_lib = MagicMock(type="movie", key="2", title="4K Movies")  # row no longer targets this
        # sections() deliberately WIDER than delivery_sections: the point is that cleanup reaches a
        # library this run no longer targets.
        ctx.plex.sections.return_value = [movies, old_lib]
        ctx.delivery_sections = [movies]
        ctx.config.rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=5, media="movie", library_keys=["1"])]
        ctx.config.rows_defined = True
        ctx.config.dry_run = False
        sarah = make_profile("sarah", account_id=100, row_overrides={"gems": RowOverride(muted=True)})
        stale = MagicMock(title="Hidden Gems" + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [stale] if s is old_lib else []

        report = UserRunReport(username="sarah", slug="sarah", diff=CollectionDiff())
        _remove_muted_and_retired(ctx, sarah, ctx.config, report)

        ctx.plex.delete_owned_collection.assert_called_once()  # removed from 4K Movies despite the scope
        # ...and the ledger entry for the collection just deleted is marked for forgetting, or the
        # dead ratingKey would be re-presented on every later run.
        assert report.removed_deliveries == [{"row_slug": "gems", "library_key": "2"}]


class TestLibraryIndexCache:
    """The cross-run tmdb_id -> ratingKey index cache in _library_index."""

    def _ctx(self, cache):
        ctx = MagicMock()
        ctx.index_cache = cache
        ctx.progress = None  # _emit only logs
        ctx.plex.section_signature.return_value = "100:200"
        ctx.plex.build_library_index.return_value = {42: 1}
        return ctx

    def _ctx_with_genres(self, cache, genres: dict[str, int] | None = None):
        """A ctx whose scan fills the caller's genre counter, like a real PMS listing does."""
        ctx = self._ctx(cache)
        tally = genres if genres is not None else {"Drama": 3}

        def scan(_section, genre_counts=None):
            if genre_counts is not None:
                genre_counts.update(tally)
            return {42: 1}

        ctx.plex.build_library_index.side_effect = scan
        return ctx

    def test_a_run_with_the_dial_off_does_not_poison_the_cache_for_a_run_with_it_on(self):
        """The regression this cache's `tallied` flag exists for.

        A dial-off run writes an entry with no tally. Serving that to a dial-on run leaves
        `library_genre_counts` empty, so genre avoidance silently does nothing for up to the whole
        TTL — or for ever on a library whose signature never moves.
        """
        cache = _DictCache()
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(self._ctx_with_genres(cache), section)  # dial off

        counts: Counter[str] = Counter()
        ctx_on = self._ctx_with_genres(cache)
        pipeline_mod._library_index(ctx_on, section, counts)

        assert counts == Counter({"Drama": 3}), "the dial-on run inherited an empty tally"
        assert ctx_on.plex.build_library_index.call_count == 1, "it should have re-scanned"

    def test_a_cached_tally_is_served_without_re_scanning(self):
        cache = _DictCache()
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(self._ctx_with_genres(cache), section, Counter())

        counts: Counter[str] = Counter()
        ctx_second = self._ctx_with_genres(cache)
        pipeline_mod._library_index(ctx_second, section, counts)

        assert counts == Counter({"Drama": 3})
        assert ctx_second.plex.build_library_index.call_count == 0, "a tallied entry must hit"

    def test_a_library_with_no_genre_tags_still_caches(self):
        """`tallied` records that a tally was TAKEN, not that it found anything.

        A real library whose items carry no <Genre> children has a full index and an empty tally.
        Inferring "no tally" from "empty tally" would make such a section re-scan on every run for
        ever — a complete section walk per run, which is the cost this cache exists to avoid.
        """
        cache = _DictCache()
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(self._ctx_with_genres(cache, genres={}), section, Counter())

        ctx_second = self._ctx_with_genres(cache, genres={})
        pipeline_mod._library_index(ctx_second, section, Counter())

        assert ctx_second.plex.build_library_index.call_count == 0, "an empty tally re-scanned for ever"

    def test_a_tallied_entry_still_serves_a_run_that_does_not_want_genres(self):
        cache = _DictCache()
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(self._ctx_with_genres(cache), section, Counter())

        ctx_second = self._ctx_with_genres(cache)
        assert pipeline_mod._library_index(ctx_second, section) == {42: 1}
        assert ctx_second.plex.build_library_index.call_count == 0

    def test_unchanged_library_serves_the_cached_index_without_re_scanning(self):
        ctx = self._ctx(_DictCache())
        section = MagicMock(key="1", title="Movies")
        first = pipeline_mod._library_index(ctx, section)
        second = pipeline_mod._library_index(ctx, section)
        assert first == second == {42: 1}
        assert ctx.plex.build_library_index.call_count == 1  # second run served from cache

    def test_a_changed_signature_re_scans(self):
        ctx = self._ctx(_DictCache())
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        ctx.plex.section_signature.return_value = "101:200"  # a title was added/removed/edited
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2

    def test_nullcache_always_scans(self):
        ctx = self._ctx(NullCache())
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2

    def test_a_missing_signature_disables_the_cache(self):
        ctx = self._ctx(_DictCache())
        ctx.plex.section_signature.return_value = None  # neither totalSize nor updatedAt available
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2


class TestShelfSequenceIsTotal:
    """Whatever the config, the arrangement is well-formed. A property test, because the ordering is
    now a real algorithm: it inserts each row next to its target's group, and the config space that
    reaches it includes self-references, cycles, ties and mixed directions that the editor tolerates
    when it did not create them. The fixpoint loop it replaced did NOT have this property — a tie
    made it oscillate until it ran out of passes."""

    @given(
        st.lists(
            st.tuples(
                st.sampled_from(["a", "b", "c", "d", "e"]),
                st.sampled_from(["top", "title-after", "title-before", "row-after", "row-before"]),
                st.sampled_from(["a", "b", "c", "d", "e"]),
            ),
            min_size=1,
            max_size=5,
            unique_by=lambda t: t[0],
        )
    )
    def test_every_row_is_placed_exactly_once_behind_exactly_one_marker(self, rows):
        from datetime import UTC, datetime

        from shortlist.engine.clients.plex_pms import POSITION_KINDS
        from shortlist.engine.models import HubAnchor, RowSpec, RunReport
        from shortlist.engine.pipeline import _shelf_sequence

        specs, keys = [], {}
        for n, (slug, kind, target) in enumerate(rows):
            if kind == "top":
                anchor = HubAnchor(to_top=True)
            elif kind.startswith("title"):
                anchor = HubAnchor(anchor_title="Gems", before=kind.endswith("before"))
            else:
                anchor = HubAnchor(anchor_row=target, before=kind.endswith("before"))
            specs.append(RowSpec(slug=slug, name_template=slug, size=10, hub_anchors={"1": anchor}))
            keys[slug] = {n + 1}

        sequence = _shelf_sequence(specs, "1", keys, "Movies", {}, RunReport(started_at=datetime.now(UTC)))

        kinds = [kind for kind, _ in sequence]
        blocks = [frozenset(value) for kind, value in sequence if kind == "rows"]
        # Alternating marker, block, marker, block … — never a block without a position of its own,
        # which is what made a row on Top inherit the previous row's anchor.
        assert all(kinds[n] in POSITION_KINDS for n in range(0, len(kinds), 2)), kinds
        assert all(kinds[n] == "rows" for n in range(1, len(kinds), 2)), kinds
        assert len(kinds) == 2 * len(rows), kinds
        # Every row exactly once: none dropped (it would sink to the bottom of the shelf) and none
        # duplicated (a repeated identifier can never satisfy the in-place check, so every pass would
        # rewrite the whole shelf). Compared as a multiset — `sorted` on frozensets is not a total
        # order, and an assertion built on it fails on a correct answer in a different order.
        assert Counter(blocks) == Counter(frozenset(v) for v in keys.values())


class TestShelfOrderFailureIsAudited:
    """A shelf write that RAISES can land mid-sequence, and used to leave no record at all."""

    def test_a_raised_move_is_recorded_rather_than_swallowed(self):
        """`place_rows` moves hubs one at a time. A raise part-way through — a collection
        deleted between the read and the move, a Plex 500 — leaves some hubs moved and some not.
        The failure path that RETURNS records itself (`verified: False`); this one recorded nothing,
        so the events feed said the run touched no shelf at all (plex-safety rule 10).
        """
        import threading
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from shortlist.engine.models import EngineConfig
        from shortlist.engine.pipeline import RunReport, _apply_placement

        plex = SimpleNamespace(place_rows=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("plex 500")))
        ctx = SimpleNamespace(plex=plex, config=EngineConfig(dry_run=False), write_lock=threading.Lock())
        report = RunReport(started_at=datetime.now(UTC))
        section = SimpleNamespace(title="Movies", key=1)

        _apply_placement(ctx, report, section, [("rows", {11})])

        assert len(report.hub_orderings) == 1, "a failed shelf write must leave a record"
        entry = report.hub_orderings[0]
        assert entry["library"] == "Movies"
        assert entry["placed"] is False
        assert "RuntimeError" in entry["reason"]

    def test_a_pass_that_moved_only_foreign_hubs_is_still_audited(self):
        """The audit used to be gated on `moved`, which holds OUR row titles only.

        A bottom-build moves the backbone as well, and when our one row here is already the shelf's
        last hub the move loop skips it (`if ident == tail: continue`) — so a pass that wrote three
        real hub moves to Plex came back `moved: []`, `repositioned: 3`, and recorded NOTHING. The
        precondition is the ordinary one, not an exotic one: Plex appends a new or rebuilt hub at the
        bottom, and the default placement is the top (plex-safety rule 10).
        """
        import threading
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from shortlist.engine.models import EngineConfig
        from shortlist.engine.pipeline import RunReport, _apply_placement

        result = {"anchor": "top", "moved": [], "repositioned": 3, "skipped": False, "verified": True}
        ctx = SimpleNamespace(
            plex=SimpleNamespace(place_rows=lambda *a, **k: result),
            config=EngineConfig(dry_run=False),
            write_lock=threading.Lock(),
        )
        report = RunReport(started_at=datetime.now(UTC))

        _apply_placement(ctx, report, SimpleNamespace(title="Movies", key=1), [("rows", {11})])

        assert len(report.hub_orderings) == 1, "three hub moves went to Plex with no audit record"
        assert report.hub_orderings[0]["repositioned"] == 3

    def test_a_pass_that_wrote_moves_and_did_not_converge_is_still_audited(self):
        """The `verified: False` shape — moves accepted by Plex and not applied, which is the exact
        production failure this whole change exists to fix. It was unaudited for the same reason."""
        import threading
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from shortlist.engine.models import EngineConfig
        from shortlist.engine.pipeline import RunReport, _apply_placement

        result = {"anchor": "top", "moved": [], "repositioned": 9, "skipped": False, "verified": False}
        ctx = SimpleNamespace(
            plex=SimpleNamespace(place_rows=lambda *a, **k: result),
            config=EngineConfig(dry_run=False),
            write_lock=threading.Lock(),
        )
        report = RunReport(started_at=datetime.now(UTC))

        _apply_placement(ctx, report, SimpleNamespace(title="Movies", key=1), [("rows", {11})])

        assert len(report.hub_orderings) == 1
        assert report.hub_orderings[0]["verified"] is False

    def test_a_pass_with_nothing_to_do_records_nothing(self):
        """The other half of the gate. A settled shelf must stay silent, or the events feed fills with
        a nightly "we changed nothing" and the contention detector counts its own noise."""
        import threading
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from shortlist.engine.models import EngineConfig
        from shortlist.engine.pipeline import RunReport, _apply_placement

        result = {"anchor": "top", "moved": [], "repositioned": 0, "skipped": True, "reason": "already in place"}
        ctx = SimpleNamespace(
            plex=SimpleNamespace(place_rows=lambda *a, **k: result),
            config=EngineConfig(dry_run=False),
            write_lock=threading.Lock(),
        )
        report = RunReport(started_at=datetime.now(UTC))

        _apply_placement(ctx, report, SimpleNamespace(title="Movies", key=1), [("rows", {11})])

        assert report.hub_orderings == []


class TestShelfSequence:
    """`_shelf_sequence` — the wanted arrangement of one library's shelf, top first.

    Replaces the group/cycle/refusal machinery that used to sequence many `move(after=…)` calls.
    Those calls are what breaks Plex: each halves the float gap between two hubs, and a gap dies
    after ~50 inserts. One sequence, realised from the top, needs no call sequencing at all.
    """

    @staticmethod
    def _spec(slug, anchors):
        from shortlist.engine.models import RowSpec

        return RowSpec(slug=slug, name_template=slug, size=10, hub_anchors=anchors)

    def _seq(self, specs, keys, **kw):
        from datetime import UTC, datetime

        from shortlist.engine.pipeline import RunReport, _shelf_sequence

        report = kw.get("report") or RunReport(started_at=datetime.now(UTC))
        return _shelf_sequence(specs, "1", keys, "Movies", {s.slug: s.slug for s in specs}, report), report

    def test_a_row_after_a_collection_puts_the_anchor_first(self):
        from shortlist.engine.models import HubAnchor

        specs = [self._spec("picked", {"1": HubAnchor(anchor_title="Recently Added Movies")})]
        seq, _ = self._seq(specs, {"picked": {11, 12}})

        assert seq == [("anchor", "Recently Added Movies"), ("rows", {11, 12})]

    def test_before_a_collection_names_the_direction_rather_than_relying_on_list_order(self):
        """`before` used to be encoded as [block, anchor] — the block ahead of its marker. The client
        reads a block with no marker ahead of it as "the very top", and those two cases are
        indistinguishable in that encoding, so "right before New Series" sent the row to the top of
        the shelf while the audit recorded New Series as its anchor. The direction is now explicit."""
        from shortlist.engine.models import HubAnchor

        specs = [self._spec("picked", {"1": HubAnchor(anchor_title="Recently Added Movies", before=True)})]
        seq, _ = self._seq(specs, {"picked": {11}})

        assert seq == [("anchor_before", "Recently Added Movies"), ("rows", {11})]

    def test_one_collection_can_have_a_row_above_it_and_another_below_it(self):
        """The anchor marker is de-duplicated per DIRECTION, not per title. Keyed by title alone, the
        second row's marker was dropped and it was placed on the first row's side."""
        from shortlist.engine.models import HubAnchor

        specs = [
            self._spec("above", {"1": HubAnchor(anchor_title="Recently Added Movies", before=True)}),
            self._spec("below", {"1": HubAnchor(anchor_title="Recently Added Movies")}),
        ]
        seq, _ = self._seq(specs, {"above": {11}, "below": {21}})

        assert seq == [
            ("anchor_before", "Recently Added Movies"),
            ("rows", {11}),
            ("anchor", "Recently Added Movies"),
            ("rows", {21}),
        ]

    def test_a_row_placed_before_another_row_precedes_it(self):
        """The missing cell of anchor-kind x direction. `want` was measured on a list still holding
        the row itself, so "before" asked for two distinct positions to be equal: the fixpoint loop
        oscillated, exhausted its passes, logged the owner's good config as a placement cycle, and
        fell back to declaration order — making `before` a silent no-op wherever it was set."""
        from shortlist.engine.models import HubAnchor

        specs = [
            self._spec("picked", {"1": HubAnchor(to_top=True)}),
            self._spec("because", {"1": HubAnchor(anchor_row="picked", before=True)}),
        ]
        seq, _ = self._seq(specs, {"picked": {11}, "because": {21}})

        assert seq == [("top", ""), ("rows", {21}), ("top", ""), ("rows", {11})]  # both chain to a Top head

    def test_two_rows_after_the_same_row_both_follow_it_in_row_order(self):
        """The missing cell of the row-chain matrix: N rows sharing one `anchor_row`.

        "Immediately after X" cannot be true of two rows at once, so the old fixpoint loop oscillated
        between them, exhausted its passes and reported the owner's perfectly legal config as a
        placement cycle — then fell back to declaration order, which put both followers ABOVE the row
        they were told to follow. The editor allows this (it offers every other row as a candidate and
        only refuses self-reference and true cycles), so it is reachable.
        """
        from loguru import logger as loguru_logger

        from shortlist.engine.models import HubAnchor

        lines: list[str] = []
        sink = loguru_logger.add(lines.append, level="WARNING")
        try:
            specs = [
                self._spec("second", {"1": HubAnchor(anchor_row="head")}),
                self._spec("third", {"1": HubAnchor(anchor_row="head")}),
                self._spec("head", {"1": HubAnchor(anchor_title="Gems")}),
            ]
            seq, _ = self._seq(specs, {"head": {11}, "second": {21}, "third": {31}})
        finally:
            loguru_logger.remove(sink)

        assert seq == [
            ("anchor", "Gems"),
            ("rows", {11}),
            ("anchor", "Gems"),
            ("rows", {21}),
            ("anchor", "Gems"),
            ("rows", {31}),
        ], "both followers sit below the head, in the order the Rows page has them"
        assert not [line for line in lines if "loop" in line.lower()], lines

    def test_a_fork_does_not_split_a_deeper_chain(self):
        """Two rows following one row, where one of them has a follower of its own.

        The sibling scan stepped over the target's DIRECT children only, so the second sibling was
        inserted inside the first sibling's subtree — breaking that deeper row's explicit "right after
        <row>" with nothing reported. Asserted in BOTH declared orders, because the insert is only
        wrong in one of them. `a` anchors the chain to a collection; `b` follows `a`, `c` follows `b`,
        `d` also follows `a`.
        """
        from shortlist.engine.models import HubAnchor

        def spec(slug, target):
            return self._spec(slug, {"1": HubAnchor(anchor_row=target)})

        head = self._spec("a", {"1": HubAnchor(anchor_title="Kometa Picks")})
        keys = {"a": {1}, "b": {2}, "c": {3}, "d": {4}}
        for declared in (
            [head, spec("b", "a"), spec("c", "b"), spec("d", "a")],
            [head, spec("d", "a"), spec("b", "a"), spec("c", "b")],
        ):
            seq, _ = self._seq(declared, keys)
            blocks = [next(iter(v)) for kind, v in seq if kind == "rows"]
            order = {1: "a", 2: "b", 3: "c", 4: "d"}
            names = [order[b] for b in blocks]
            assert names.index("c") == names.index("b") + 1, f"c must follow b directly, got {names}"
            assert names[0] == "a", names

    def test_two_rows_before_the_same_row_both_precede_it_in_row_order(self):
        """The same tie in the other direction."""
        from shortlist.engine.models import HubAnchor

        specs = [
            self._spec("first", {"1": HubAnchor(anchor_row="tail", before=True)}),
            self._spec("second", {"1": HubAnchor(anchor_row="tail", before=True)}),
            self._spec("tail", {"1": HubAnchor(anchor_title="Gems")}),
        ]
        seq, _ = self._seq(specs, {"tail": {11}, "first": {21}, "second": {31}})

        assert [v for kind, v in seq if kind == "rows"] == [{21}, {31}, {11}]

    def test_a_row_before_another_row_does_not_report_a_cycle(self):
        """The same bug from the owner's side: the run log blamed a loop that did not exist.

        A loguru SINK, not pytest's `caplog` — this codebase logs through loguru, which does not feed
        the stdlib handler `caplog` reads, so an absence assertion against `caplog.text` passes on
        broken code as readily as on fixed code.
        """
        from loguru import logger as loguru_logger

        from shortlist.engine.models import HubAnchor

        lines: list[str] = []
        sink = loguru_logger.add(lines.append, level="WARNING")
        try:
            specs = [
                self._spec("picked", {"1": HubAnchor(to_top=True)}),
                self._spec("because", {"1": HubAnchor(anchor_row="picked", before=True)}),
            ]
            self._seq(specs, {"picked": {11}, "because": {21}})
        finally:
            loguru_logger.remove(sink)

        assert not [line for line in lines if "loop" in line.lower()], lines

    def test_a_row_placed_after_another_row_follows_it(self):
        from shortlist.engine.models import HubAnchor

        specs = [
            self._spec("picked", {"1": HubAnchor(anchor_title="Recently Added Movies")}),
            self._spec("because", {"1": HubAnchor(anchor_row="picked")}),
        ]
        seq, _ = self._seq(specs, {"picked": {11}, "because": {21}})

        assert seq == [
            ("anchor", "Recently Added Movies"),
            ("rows", {11}),
            ("anchor", "Recently Added Movies"),
            ("rows", {21}),
        ], "the follower adopts the landmark at the head of its chain, or it has no landmark at all"

    def test_a_chain_of_row_anchors_is_resolved_in_dependency_order(self):
        """The maintainer's own config: picked after a collection, because after picked, popular
        after because. Declared in a different order than they must appear."""
        from shortlist.engine.models import HubAnchor

        specs = [
            self._spec("popular", {"1": HubAnchor(anchor_row="because")}),
            self._spec("because", {"1": HubAnchor(anchor_row="picked")}),
            self._spec("picked", {"1": HubAnchor(anchor_title="Recently Added Movies")}),
        ]
        seq, _ = self._seq(specs, {"picked": {11}, "because": {21}, "popular": {31}})

        assert seq == [
            ("anchor", "Recently Added Movies"),
            ("rows", {11}),
            ("anchor", "Recently Added Movies"),
            ("rows", {21}),
            ("anchor", "Recently Added Movies"),
            ("rows", {31}),
        ]

    def test_a_row_with_placement_switched_off_is_not_placed_at_all(self):
        from shortlist.engine.models import HubAnchor

        specs = [
            self._spec("picked", {"1": HubAnchor(to_top=True)}),
            self._spec("because", {"1": HubAnchor(enabled=False)}),
        ]
        seq, _ = self._seq(specs, {"picked": {11}, "because": {21}})

        assert seq == [("top", ""), ("rows", {11})], "an off row must not appear in the arrangement"

    def test_a_row_with_no_placement_configured_defaults_to_the_top(self):
        """Not "leave it alone" — Plex appends new hubs at the BOTTOM, so a new row nothing positions
        starts out of sight. Opting out is the per-row switch, set deliberately."""
        specs = [self._spec("picked", {})]
        seq, _ = self._seq(specs, {"picked": {11}})

        assert seq == [("top", ""), ("rows", {11})]

    def test_a_row_the_ledger_does_not_name_is_reported_not_silently_skipped(self):
        """A row with nothing recorded is not placed this run, and a new one sits where Plex put it —
        at the bottom of the shelf — so the skip is reported rather than silent."""
        from shortlist.engine.models import HubAnchor

        specs = [self._spec("picked", {"1": HubAnchor(to_top=True)})]
        seq, report = self._seq(specs, {})

        assert seq == []
        assert len(report.hub_orderings) == 1
        assert report.hub_orderings[0]["placed"] is False
        assert "delivery ledger" in report.hub_orderings[0]["reason"]

    def test_two_rows_pointing_at_each_other_fall_back_to_row_order(self):
        """A loop cannot be honoured. Dropping the rows instead would let them sink to the bottom."""
        from shortlist.engine.models import HubAnchor

        specs = [
            self._spec("a", {"1": HubAnchor(anchor_row="b")}),
            self._spec("b", {"1": HubAnchor(anchor_row="a")}),
        ]
        seq, report = self._seq(specs, {"a": {11}, "b": {21}})

        assert [kind for kind, _ in seq] == ["top", "rows", "top", "rows"]
        assert {frozenset(v) for kind, v in seq if kind == "rows"} == {frozenset({11}), frozenset({21})}
        # RECORDED, not just logged: a setting that is quietly doing nothing has to be answerable
        # from the UI (plex-safety rule 10). This was a container-log warning and nothing else.
        assert len(report.hub_orderings) == 1, report.hub_orderings
        entry = report.hub_orderings[0]
        assert entry["placed"] is False
        assert "loop" in entry["reason"]
        assert "a" in entry["row"] and "b" in entry["row"], entry["row"]

    def test_one_anchor_named_by_two_rows_is_marked_for_each_of_them(self):
        """The marker repeats on purpose. It used to be emitted once and the second row's block simply
        followed — which reads the same here, but meant a block's position came from the block BEFORE
        it rather than from its own setting. Insert a Top row between these two and the second one
        went to the top of the shelf. `place_rows` accumulates repeats of one anchor in order, so
        repeating the marker costs nothing and removes the inheritance."""
        from shortlist.engine.models import HubAnchor

        specs = [
            self._spec("picked", {"1": HubAnchor(anchor_title="Recently Added Movies")}),
            self._spec("gems", {"1": HubAnchor(anchor_title="Recently Added Movies")}),
        ]
        seq, _ = self._seq(specs, {"picked": {11}, "gems": {21}})

        assert seq == [
            ("anchor", "Recently Added Movies"),
            ("rows", {11}),
            ("anchor", "Recently Added Movies"),
            ("rows", {21}),
        ]
