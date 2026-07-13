"""Privacy module tests — the merge code is the highest-consequence code in the repo."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rowarr.engine import privacy
from rowarr.engine.models import UserType
from rowarr.engine.privacy import (
    FilterCondition,
    FilterParseError,
    merge_label_excludes,
    parse_filter,
    remove_label_excludes,
    rowarr_labels_in,
    serialize_filter,
    sync_user_restrictions,
)
from tests.conftest import make_profile, plextv_user

# Raw filter values: plex.tv never gives us , | = ! inside a value (they're syntax).
value = st.text(alphabet=st.sampled_from("abcdefgXYZ0123456789_%."), min_size=1, max_size=12)
field_name = st.sampled_from(["label", "contentRating", "genre", "year"])
condition = st.builds(
    lambda f, op, vals: FilterCondition(f, op, tuple(vals)),
    field_name,
    st.sampled_from(["=", "!="]),
    st.lists(value, min_size=1, max_size=4),
)
filter_string = st.lists(condition, min_size=0, max_size=5).map(serialize_filter)


class TestParseSerializeRoundTrip:
    @given(filter_string)
    def test_round_trip_is_byte_identical(self, raw: str):
        assert serialize_filter(parse_filter(raw)) == raw

    def test_parse_empty_returns_no_conditions(self):
        assert parse_filter("") == []

    def test_parse_preserves_raw_urlencoded_values(self):
        conds = parse_filter("label!=Some%20Label,other")
        assert conds == [FilterCondition("label", "!=", ("Some%20Label", "other"))]

    def test_parse_rejects_garbage_instead_of_clobbering(self):
        with pytest.raises(FilterParseError):
            parse_filter("label!=ok|garbage-without-operator")


class TestMergeLabelExcludes:
    """Filter-state matrix: empty / rowarr-only / pre-existing-foreign / mixed."""

    def test_merge_into_empty_filter(self):
        assert merge_label_excludes("", {"Rowarr_sarah"}) == "label!=Rowarr_sarah"

    def test_merge_into_existing_rowarr_excludes_appends(self):
        merged = merge_label_excludes("label!=Rowarr_mike", {"Rowarr_sarah"})
        assert merged == "label!=Rowarr_mike,Rowarr_sarah"

    def test_merge_preserves_foreign_conditions_byte_identical(self):
        raw = "contentRating!=R,NC-17|genre=Horror"
        merged = merge_label_excludes(raw, {"Rowarr_sarah"})
        assert merged == raw + "|label!=Rowarr_sarah"

    def test_merge_mixed_only_touches_the_label_condition(self):
        raw = "contentRating!=R|label!=kids_hide,Rowarr_mike|genre=Horror"
        merged = merge_label_excludes(raw, {"Rowarr_sarah"})
        assert merged == "contentRating!=R|label!=kids_hide,Rowarr_mike,Rowarr_sarah|genre=Horror"

    def test_merge_is_idempotent(self):
        once = merge_label_excludes("label!=x", {"Rowarr_a", "Rowarr_b"})
        assert merge_label_excludes(once, {"Rowarr_a", "Rowarr_b"}) == once

    def test_merge_already_present_returns_input_unchanged(self):
        raw = "label!=Rowarr_sarah|contentRating!=R"
        assert merge_label_excludes(raw, {"Rowarr_sarah"}) is raw

    def test_merge_is_case_insensitive_like_plex_tag_matching(self):
        # A case-variant of an already excluded label must never be appended as a duplicate.
        raw = "label!=Rowarr_sarah"
        assert merge_label_excludes(raw, {"rowarr_sarah"}) is raw

    def test_desired_excludes_only_covers_users_with_real_collections(self):
        stored = {"mike": "Rowarr_mike"}  # newbie has no collection yet — nothing to leak
        assert privacy.desired_excludes("Rowarr_sarah", stored) == {"Rowarr_mike"}

    def test_desired_excludes_covers_rows_whose_owner_rowarr_does_not_manage(self):
        """A row is visible to anyone whose filter doesn't exclude it. Plex does not care that
        Rowarr considers its owner disabled, paused, or a stranger — so the excludes come from
        the rows that EXIST, never from the roster of users we happen to be processing."""
        stored = {"sarah": "Rowarr_sarah", "mike": "Rowarr_mike"}

        # An account that owns no row (own_label=None) is excluded from every one of them.
        assert privacy.desired_excludes(None, stored) == {"Rowarr_sarah", "Rowarr_mike"}

    def test_a_user_is_never_excluded_from_their_own_row(self):
        assert privacy.desired_excludes("Rowarr_sarah", {"sarah": "Rowarr_sarah"}) == set()

    def test_identity_is_the_label_not_the_name(self):
        """Two Plex display names can slugify to the same string, and anyone can rename
        themselves. If "is this row mine?" were answered from a name, one account would be let
        off an exclude it needs (they see someone else's row) and another would be excluded from
        their own. The caller resolves the label from the ACCOUNT ID and passes it here."""
        stored = {"bob_smith": "Rowarr_bob_smith", "mike": "Rowarr_mike"}

        # A different account whose name happens to slugify to "bob_smith" owns no row...
        assert privacy.desired_excludes(None, stored) == {"Rowarr_bob_smith", "Rowarr_mike"}
        # ...while the account that really owns it is not excluded from itself.
        assert privacy.desired_excludes("Rowarr_bob_smith", stored) == {"Rowarr_mike"}


class TestSharedRowExcludes:
    """A shared 'popular on this server' row follows its audience: public rows are hidden from
    nobody; subset rows are hidden from everyone NOT in the audience — the same machinery as a
    private row, generalized."""

    def test_public_shared_row_is_excluded_from_nobody(self):
        stored = {"sarah": "Rowarr_sarah", "shared_popular": "Rowarr_shared_popular"}
        # No audience map -> the shared row is public -> only the per-person label is excluded.
        assert privacy.desired_excludes(None, stored, account_id=202) == {"Rowarr_sarah"}

    def test_subset_shared_row_is_hidden_from_accounts_outside_the_audience(self):
        stored = {"shared_staff": "Rowarr_shared_staff"}
        audiences = {"rowarr_shared_staff": {201, 202}}
        # In the audience -> can see it -> not excluded.
        assert privacy.desired_excludes(None, stored, account_id=201, shared_audiences=audiences) == set()
        assert privacy.desired_excludes(None, stored, account_id=202, shared_audiences=audiences) == set()
        # Outside the audience -> excluded, exactly like a private row.
        assert privacy.desired_excludes(None, stored, account_id=203, shared_audiences=audiences) == {
            "Rowarr_shared_staff"
        }

    def test_subset_shared_and_private_rows_compose(self):
        stored = {"sarah": "Rowarr_sarah", "shared_staff": "Rowarr_shared_staff"}
        audiences = {"rowarr_shared_staff": {202}}
        # Mike (202) is in the staff audience but must still be hidden from sarah's private row.
        assert privacy.desired_excludes(None, stored, account_id=202, shared_audiences=audiences) == {"Rowarr_sarah"}

    @given(st.sets(st.integers(min_value=1, max_value=5), min_size=0, max_size=5), st.integers(1, 6))
    def test_shared_label_is_excluded_from_exactly_the_non_audience(self, audience: set[int], account_id: int):
        """Property: a subset shared row is excluded from an account iff that account is not in its
        audience — for any audience and any account. Never leaks in, never over-hides."""
        stored = {"shared_x": "Rowarr_shared_x"}
        audiences = {"rowarr_shared_x": audience}
        excludes = privacy.desired_excludes(None, stored, account_id=account_id, shared_audiences=audiences)
        if account_id in audience:
            assert "Rowarr_shared_x" not in excludes
        else:
            assert excludes == {"Rowarr_shared_x"}

    @given(filter_string, st.sets(st.sampled_from(["Rowarr_a", "Rowarr_b", "Rowarr_c"]), min_size=1, max_size=3))
    def test_merge_never_drops_existing_conditions(self, raw: str, labels: set[str]):
        merged_conditions = parse_filter(merge_label_excludes(raw, labels))
        for original in parse_filter(raw):
            match = [c for c in merged_conditions if c.field == original.field and c.op == original.op]
            assert match, f"condition {original} vanished"
            surviving_values = set().union(*(set(c.values) for c in match))
            assert set(original.values) <= surviving_values

    @given(filter_string, st.sets(st.sampled_from(["Rowarr_a", "Rowarr_b"]), min_size=1, max_size=2))
    def test_remove_inverts_merge_when_labels_were_absent(self, raw: str, labels: set[str]):
        for cond in parse_filter(raw):
            if cond.field == "label" and cond.op == "!=" and set(cond.values) & labels:
                return  # labels pre-existed; removal would legitimately alter the original
        assert remove_label_excludes(merge_label_excludes(raw, labels), labels) == raw


class TestRowarrLabelsIn:
    def test_finds_only_prefixed_labels_case_insensitive(self):
        raw = "label!=Rowarr_sarah,kids_hide,rowarr_mike"
        assert rowarr_labels_in(raw, "rowarr") == {"Rowarr_sarah", "rowarr_mike"}


class TestSyncUserRestrictions:
    """User-type matrix: owner (never restricted) / shared / managed all flow through here."""

    def _users(self):
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        owner = make_profile("steve", user_type=UserType.OWNER, account_id=1)
        return sarah, mike, owner

    def test_managed_user_gets_only_filter_fields_never_profile_writes(self, mock_plextv, snapshot_store):
        # MANAGED collapses with SHARED for sync (no branch on user_type besides OWNER) —
        # this pins the contract that only filterMovies/filterTelevision are ever PUT
        # (a managed user's restriction PROFILE is parental controls; rule 5).
        managed = make_profile("kid", user_type=UserType.MANAGED, account_id=400)
        mock_plextv.users = [plextv_user(400, "kid")]
        mock_plextv.update_user_filters.side_effect = lambda _id, fields: mock_plextv.users[0].filters.update(fields)
        sync_user_restrictions(
            mock_plextv,
            managed,
            mock_plextv.get_user(managed.plex_account_id),
            {"sarah": "Rowarr_sarah"},
            snapshot_store,
        )
        call = mock_plextv.update_user_filters.call_args
        assert sorted(call.args[1]) == ["filterMovies", "filterTelevision"]

    def test_owner_is_never_restricted(self, mock_plextv, snapshot_store):
        _sarah, _mike, owner = self._users()
        # The owner is not even on plex.tv's user list, so `remote` is None: they are skipped
        # before it is ever read (Plex cannot restrict the owner — rule 5).
        wrote = sync_user_restrictions(mock_plextv, owner, None, {}, snapshot_store)
        assert wrote is None
        mock_plextv.update_user_filters.assert_not_called()

    def test_first_sync_snapshots_then_merges_with_stored_labels(self, mock_plextv, snapshot_store):
        sarah = self._users()[0]
        mock_plextv.users = [plextv_user(100, "sarah", filters={"filterMovies": "contentRating!=R"})]

        def put(account_id, fields):
            user = mock_plextv.users[0].filters
            user.update(fields)

        mock_plextv.update_user_filters.side_effect = put
        stored = {"mike": "Rowarr_mike", "steve": "Rowarr_steve"}

        wrote = sync_user_restrictions(
            mock_plextv, sarah, mock_plextv.get_user(sarah.plex_account_id), stored, snapshot_store
        )

        # The return value IS the audit record: what changed, on which field, from what to what.
        assert wrote == {
            "filterMovies": ("contentRating!=R", "contentRating!=R|label!=Rowarr_mike,Rowarr_steve"),
            "filterTelevision": ("", "label!=Rowarr_mike,Rowarr_steve"),
        }
        assert snapshot_store.saved[100].filters["filterMovies"] == "contentRating!=R"
        call = mock_plextv.update_user_filters.call_args
        assert call.args[0] == 100
        # Both fields merged; foreign condition preserved byte-identical; stored (title-cased) labels used.
        assert call.args[1]["filterMovies"] == "contentRating!=R|label!=Rowarr_mike,Rowarr_steve"
        assert call.args[1]["filterTelevision"] == "label!=Rowarr_mike,Rowarr_steve"

    def test_steady_state_makes_zero_writes(self, mock_plextv, snapshot_store):
        sarah = self._users()[0]
        mock_plextv.users = [
            plextv_user(
                100,
                "sarah",
                filters={
                    "filterMovies": "label!=Rowarr_mike,Rowarr_steve",
                    "filterTelevision": "label!=Rowarr_mike,Rowarr_steve",
                },
            )
        ]
        stored = {"mike": "Rowarr_mike", "steve": "Rowarr_steve"}
        wrote = sync_user_restrictions(
            mock_plextv, sarah, mock_plextv.get_user(sarah.plex_account_id), stored, snapshot_store
        )
        assert wrote is None
        mock_plextv.update_user_filters.assert_not_called()

    def test_dry_run_writes_nothing_but_reports_pending_change(self, mock_plextv, snapshot_store):
        sarah = self._users()[0]
        mock_plextv.users = [plextv_user(100, "sarah")]
        wrote = sync_user_restrictions(
            mock_plextv,
            sarah,
            mock_plextv.get_user(sarah.plex_account_id),
            {"mike": "Rowarr_mike"},
            snapshot_store,
            dry_run=True,
        )
        assert wrote == {
            "filterMovies": ("", "label!=Rowarr_mike"),
            "filterTelevision": ("", "label!=Rowarr_mike"),
        }
        mock_plextv.update_user_filters.assert_not_called()
        assert snapshot_store.saved == {}

    def test_readback_missing_exclude_raises(self, mock_plextv, snapshot_store):
        sarah = self._users()[0]
        mock_plextv.users = [plextv_user(100, "sarah")]
        mock_plextv.update_user_filters.side_effect = lambda *a: None  # write silently doesn't stick
        with pytest.raises(RuntimeError, match="read-back missing"):
            sync_user_restrictions(
                mock_plextv, sarah, mock_plextv.get_user(sarah.plex_account_id), {"mike": "Rowarr_mike"}, snapshot_store
            )


class TestRestore:
    def test_restore_puts_only_diverged_fields_byte_identical(self, mock_plextv):
        from datetime import UTC, datetime

        from rowarr.engine.models import FilterSnapshot

        snapshot = FilterSnapshot(
            plex_account_id=100,
            username="sarah",
            taken_at=datetime.now(UTC),
            filters={
                "filterAll": "",
                "filterMovies": "contentRating!=R",
                "filterTelevision": "",
                "filterMusic": "",
                "filterPhotos": "",
            },
        )
        mock_plextv.users = [
            plextv_user(
                100,
                "sarah",
                filters={
                    "filterMovies": "contentRating!=R|label!=Rowarr_mike",
                    "filterTelevision": "label!=Rowarr_mike",
                },
            )
        ]

        def put(account_id, fields):
            mock_plextv.users[0].filters.update(fields)

        mock_plextv.update_user_filters.side_effect = put
        assert privacy.restore_user_restrictions(mock_plextv, snapshot) is True
        call = mock_plextv.update_user_filters.call_args
        assert call.args[1] == {"filterMovies": "contentRating!=R", "filterTelevision": ""}
