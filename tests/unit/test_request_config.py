"""Resolving one row's effective request config from the global one plus that row's overrides.

The rule the whole per-row feature rests on: ceilings that protect a shared resource stay global,
settings that express taste go per-row. A row may make itself *more* restrictive and never less.
"""

from __future__ import annotations

from shortlist.engine.models import ArrTarget, RequestConfig, RequestOverrides
from shortlist.engine.request_config import resolve_request_config

RADARR = ArrTarget(url="http://radarr.test", api_key="rk", quality_profile_id=1, root_folder="/movies")
SONARR = ArrTarget(url="http://sonarr.test", api_key="sk", quality_profile_id=2, root_folder="/tv")


def _cfg(**kw) -> RequestConfig:
    defaults = dict(
        enabled=True,
        radarr=RADARR,
        sonarr=SONARR,
        rating_source="imdb",
        mdblist_api_key="mk",
        min_rating=7.0,
        min_votes=100,
        min_demand=2,
        min_year=2000,
        max_year=0,
        max_per_run=10,
        auto_send=True,
        auto_min_demand=3,
        auto_min_rating=8.0,
    )
    defaults.update(kw)
    return RequestConfig(**defaults)


class TestInheritance:
    def test_no_overrides_returns_an_equal_config(self):
        base = _cfg()
        assert resolve_request_config(base, None) == base

    def test_an_empty_override_set_changes_nothing(self):
        base = _cfg()
        assert resolve_request_config(base, RequestOverrides()) == base

    def test_resolving_never_mutates_the_global_config(self):
        """Every row resolves from the same base; mutating it would leak row A's floors into row B."""
        base = _cfg(min_rating=7.0)
        resolve_request_config(base, RequestOverrides(min_rating=9.0))
        assert base.min_rating == 7.0

    def test_an_override_replaces_only_its_own_field(self):
        out = resolve_request_config(_cfg(min_rating=7.0, min_year=2000), RequestOverrides(min_rating=8.5))
        assert (out.min_rating, out.min_year) == (8.5, 2000)

    def test_a_zero_override_is_honoured_not_treated_as_absent(self):
        """0 is meaningful for the year bounds — 'no lower bound' — so None must be the only 'inherit'."""
        out = resolve_request_config(_cfg(min_year=2020), RequestOverrides(min_year=0))
        assert out.min_year == 0

    def test_a_false_override_is_honoured(self):
        """Same trap for auto_send: False is a real choice, not 'unset'."""
        out = resolve_request_config(_cfg(auto_send=True), RequestOverrides(auto_send=False))
        assert out.auto_send is False


class TestTheGlobalCeilingsAreNotOverridable:
    """`max_per_run` and the rating source are the run's protection and its one API account. A row
    that could raise either would make the global setting a suggestion."""

    def test_a_row_cannot_raise_the_run_ceiling(self):
        out = resolve_request_config(_cfg(max_per_run=10), RequestOverrides(max_per_row=999))
        assert out.max_per_run == 10

    def test_max_per_row_is_carried_separately_for_the_allocator(self):
        out = resolve_request_config(_cfg(max_per_run=10), RequestOverrides(max_per_row=3))
        assert out.max_per_row == 3

    def test_max_per_row_defaults_to_the_run_ceiling(self):
        """No row limit means 'as much as the run allows' — the allocator still divides the cap."""
        assert resolve_request_config(_cfg(max_per_run=10), None).max_per_row == 10

    def test_the_rating_source_and_key_stay_global(self):
        base = _cfg(rating_source="imdb", mdblist_api_key="mk")
        out = resolve_request_config(base, RequestOverrides(min_rating=9.0))
        assert (out.rating_source, out.mdblist_api_key) == ("imdb", "mk")


class TestTargetOverrides:
    """A row files into its own folder with its own profile, on the SAME Radarr/Sonarr instance."""

    def test_profile_and_folder_override_but_url_and_key_do_not(self):
        out = resolve_request_config(_cfg(), RequestOverrides(radarr_quality_profile_id=9, radarr_root_folder="/kids"))
        assert (out.radarr.url, out.radarr.api_key) == ("http://radarr.test", "rk")
        assert (out.radarr.quality_profile_id, out.radarr.root_folder) == (9, "/kids")

    def test_overriding_radarr_leaves_sonarr_alone(self):
        out = resolve_request_config(_cfg(), RequestOverrides(radarr_root_folder="/kids"))
        assert out.sonarr == SONARR

    def test_sonarr_overrides_independently(self):
        out = resolve_request_config(
            _cfg(), RequestOverrides(sonarr_quality_profile_id=7, sonarr_root_folder="/kidstv")
        )
        assert (out.sonarr.quality_profile_id, out.sonarr.root_folder) == (7, "/kidstv")
        assert out.radarr == RADARR

    def test_an_override_on_an_unconfigured_arr_stays_none(self):
        """No global Radarr means no URL and no key, so a row override cannot conjure a target —
        it would be an ArrTarget pointing nowhere, and `_request_one` would try to send to it."""
        out = resolve_request_config(_cfg(radarr=None), RequestOverrides(radarr_root_folder="/kids"))
        assert out.radarr is None

    def test_a_partial_target_override_keeps_the_other_half(self):
        out = resolve_request_config(_cfg(), RequestOverrides(radarr_root_folder="/kids"))
        assert (out.radarr.quality_profile_id, out.radarr.root_folder) == (1, "/kids")

    def test_the_arr_tag_is_preserved(self):
        tagged = ArrTarget(url="u", api_key="k", quality_profile_id=1, root_folder="/m", tag="shortlist")
        out = resolve_request_config(_cfg(radarr=tagged), RequestOverrides(radarr_root_folder="/kids"))
        assert out.radarr.tag == "shortlist"


class TestZeroIsARealChoiceNotAnUnsetSentinel:
    """Architecture review, 2026-08-18 (HIGH). `max_per_row` used 0 as its "inherit" sentinel, so a
    row set to 0 — which the editor offers, and describes as "this row never asks for anything on its
    own" — was handed the FULL run cap. The exact inverse of the control, on the path that adds
    titles to Radarr. The 0 cell was the only one this class did not cover."""

    def test_zero_means_zero(self):
        out = resolve_request_config(_cfg(max_per_run=5), RequestOverrides(max_per_row=0))
        assert out.max_per_row == 0

    def test_none_still_means_inherit(self):
        assert resolve_request_config(_cfg(max_per_run=5), RequestOverrides(max_per_row=None)).max_per_row == 5
        assert resolve_request_config(_cfg(max_per_run=5), None).max_per_row == 5

    def test_a_bare_config_still_defaults_to_its_own_run_cap(self):
        """Nothing else in the codebase passes `max_per_row`, so the default must keep working."""
        assert _cfg(max_per_run=7).max_per_row == 7
