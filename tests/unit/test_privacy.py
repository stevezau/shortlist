"""Privacy module tests — the merge code is the highest-consequence code in the repo."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine import privacy
from shortlist.engine.models import UserType
from shortlist.engine.privacy import (
    FilterCondition,
    FilterParseError,
    merge_label_excludes,
    parse_filter,
    remove_label_excludes,
    serialize_filter,
    shortlist_labels_in,
    summarise_filter_diff,
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
    """Filter-state matrix: empty / shortlist-only / pre-existing-foreign / mixed."""

    def test_merge_into_empty_filter(self):
        assert merge_label_excludes("", {"Shortlist_sarah"}) == "label!=Shortlist_sarah"

    def test_merge_into_existing_shortlist_excludes_appends(self):
        merged = merge_label_excludes("label!=Shortlist_mike", {"Shortlist_sarah"})
        assert merged == "label!=Shortlist_mike,Shortlist_sarah"

    def test_merge_preserves_foreign_conditions_byte_identical(self):
        raw = "contentRating!=R,NC-17|genre=Horror"
        merged = merge_label_excludes(raw, {"Shortlist_sarah"})
        assert merged == raw + "|label!=Shortlist_sarah"

    def test_merge_mixed_only_touches_the_label_condition(self):
        raw = "contentRating!=R|label!=kids_hide,Shortlist_mike|genre=Horror"
        merged = merge_label_excludes(raw, {"Shortlist_sarah"})
        assert merged == "contentRating!=R|label!=kids_hide,Shortlist_mike,Shortlist_sarah|genre=Horror"

    def test_merge_is_idempotent(self):
        once = merge_label_excludes("label!=x", {"Shortlist_a", "Shortlist_b"})
        assert merge_label_excludes(once, {"Shortlist_a", "Shortlist_b"}) == once

    def test_merge_already_present_returns_input_unchanged(self):
        raw = "label!=Shortlist_sarah|contentRating!=R"
        assert merge_label_excludes(raw, {"Shortlist_sarah"}) is raw

    def test_merge_is_case_insensitive_like_plex_tag_matching(self):
        # A case-variant of an already excluded label must never be appended as a duplicate.
        raw = "label!=Shortlist_sarah"
        assert merge_label_excludes(raw, {"shortlist_sarah"}) is raw

    def test_desired_excludes_only_covers_users_with_real_collections(self):
        stored = {"mike": "Shortlist_mike"}  # newbie has no collection yet — nothing to leak
        assert privacy.desired_excludes("Shortlist_sarah", stored) == {"Shortlist_mike"}

    def test_desired_excludes_covers_rows_whose_owner_shortlist_does_not_manage(self):
        """A row is visible to anyone whose filter doesn't exclude it. Plex does not care that
        Shortlist considers its owner disabled, paused, or a stranger — so the excludes come from
        the rows that EXIST, never from the roster of users we happen to be processing."""
        stored = {"sarah": "Shortlist_sarah", "mike": "Shortlist_mike"}

        # An account that owns no row (own_label=None) is excluded from every one of them.
        assert privacy.desired_excludes(None, stored) == {"Shortlist_sarah", "Shortlist_mike"}

    def test_a_user_is_never_excluded_from_their_own_row(self):
        assert privacy.desired_excludes("Shortlist_sarah", {"sarah": "Shortlist_sarah"}) == set()

    def test_identity_is_the_label_not_the_name(self):
        """Two Plex display names can slugify to the same string, and anyone can rename
        themselves. If "is this row mine?" were answered from a name, one account would be let
        off an exclude it needs (they see someone else's row) and another would be excluded from
        their own. The caller resolves the label from the ACCOUNT ID and passes it here."""
        stored = {"bob_smith": "Shortlist_bob_smith", "mike": "Shortlist_mike"}

        # A different account whose name happens to slugify to "bob_smith" owns no row...
        assert privacy.desired_excludes(None, stored) == {"Shortlist_bob_smith", "Shortlist_mike"}
        # ...while the account that really owns it is not excluded from itself.
        assert privacy.desired_excludes("Shortlist_bob_smith", stored) == {"Shortlist_mike"}


class TestSharedRowExcludes:
    """A shared 'popular on this server' row follows its audience: public rows are hidden from
    nobody; subset rows are hidden from everyone NOT in the audience. Classification is by CONFIG
    (the `shared_labels` map), never by the label string."""

    def test_public_shared_row_is_excluded_from_nobody(self):
        stored = {"sarah": "Shortlist_sarah", "shared_popular": "Shortlist__shared_popular"}
        shared = {"shortlist__shared_popular": None}  # configured public shared row
        # The public shared row is excluded from nobody; the per-person label still is.
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels=shared) == {"Shortlist_sarah"}

    def test_hide_all_shared_hides_even_a_public_row_from_an_opted_out_account(self):
        # A DISABLED (opted-out) Shortlist account: hide_all_shared hides EVERY shared row from them,
        # including the public one that everyone else sees.
        stored = {"sarah": "Shortlist_sarah", "shared_popular": "Shortlist__shared_popular"}
        shared = {"shortlist__shared_popular": None}  # public
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels=shared) == {"Shortlist_sarah"}
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels=shared, hide_all_shared=True) == {
            "Shortlist_sarah",
            "Shortlist__shared_popular",
        }

    def test_subset_shared_row_is_hidden_from_accounts_outside_the_audience(self):
        stored = {"shared_staff": "Shortlist__shared_staff"}
        shared = {"shortlist__shared_staff": {201, 202}}
        assert privacy.desired_excludes(None, stored, account_id=201, shared_labels=shared) == set()
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels=shared) == set()
        assert privacy.desired_excludes(None, stored, account_id=203, shared_labels=shared) == {
            "Shortlist__shared_staff"
        }

    def test_hide_all_shared_hides_a_subset_row_even_from_an_in_audience_account(self):
        # An opted-out account that WAS in a subset row's audience still has it hidden under
        # hide_all_shared — the same guard as the public case.
        stored = {"shared_staff": "Shortlist__shared_staff"}
        shared = {"shortlist__shared_staff": {201, 202}}
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels=shared) == set()
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels=shared, hide_all_shared=True) == {
            "Shortlist__shared_staff"
        }

    def test_a_private_row_is_never_misread_as_shared_by_its_slug(self):
        """A per-person user whose slug looks shared (label shortlist_shared_tv) is NOT in the config
        map, so it's treated as private and excluded — never leaked. This is the HIGH bug regression."""
        stored = {"shared_tv": "Shortlist_shared_tv"}  # a real user's private label, single underscore
        shared = {"shortlist__shared_popular": None}  # the only configured shared row is something else
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels=shared) == {"Shortlist_shared_tv"}

    def test_a_stale_shared_label_not_in_config_is_excluded_not_leaked(self):
        # A shared collection left on the server but no longer configured -> hidden, not public.
        stored = {"gone": "Shortlist__shared_gone"}
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels={}) == {"Shortlist__shared_gone"}

    def test_subset_shared_and_private_rows_compose(self):
        stored = {"sarah": "Shortlist_sarah", "shared_staff": "Shortlist__shared_staff"}
        shared = {"shortlist__shared_staff": {202}}
        # Mike (202) is in the staff audience but must still be hidden from sarah's private row.
        assert privacy.desired_excludes(None, stored, account_id=202, shared_labels=shared) == {"Shortlist_sarah"}

    @given(st.sets(st.integers(min_value=1, max_value=5), min_size=0, max_size=5), st.integers(1, 6))
    def test_shared_label_is_excluded_from_exactly_the_non_audience(self, audience: set[int], account_id: int):
        """Property: a subset shared row is excluded from an account iff that account is not in its
        audience — for any audience and any account. Never leaks in, never over-hides."""
        stored = {"shared_x": "Shortlist__shared_x"}
        shared = {"shortlist__shared_x": audience}
        excludes = privacy.desired_excludes(None, stored, account_id=account_id, shared_labels=shared)
        if account_id in audience:
            assert "Shortlist__shared_x" not in excludes
        else:
            assert excludes == {"Shortlist__shared_x"}

    @given(
        filter_string, st.sets(st.sampled_from(["Shortlist_a", "Shortlist_b", "Shortlist_c"]), min_size=1, max_size=3)
    )
    def test_merge_never_drops_existing_conditions(self, raw: str, labels: set[str]):
        merged_conditions = parse_filter(merge_label_excludes(raw, labels))
        for original in parse_filter(raw):
            match = [c for c in merged_conditions if c.field == original.field and c.op == original.op]
            assert match, f"condition {original} vanished"
            surviving_values = set().union(*(set(c.values) for c in match))
            assert set(original.values) <= surviving_values

    @given(filter_string, st.sets(st.sampled_from(["Shortlist_a", "Shortlist_b"]), min_size=1, max_size=2))
    def test_remove_inverts_merge_when_labels_were_absent(self, raw: str, labels: set[str]):
        for cond in parse_filter(raw):
            if cond.field == "label" and cond.op == "!=" and set(cond.values) & labels:
                return  # labels pre-existed; removal would legitimately alter the original
        assert remove_label_excludes(merge_label_excludes(raw, labels), labels) == raw


class TestShortlistLabelsIn:
    def test_finds_only_prefixed_labels_case_insensitive(self):
        raw = "label!=Shortlist_sarah,kids_hide,shortlist_mike"
        assert shortlist_labels_in(raw, "shortlist") == {"Shortlist_sarah", "shortlist_mike"}


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
            {"sarah": "Shortlist_sarah"},
            snapshot_store,
        )
        call = mock_plextv.update_user_filters.call_args
        assert call.args[0] == 400
        # The FIELDS: only the two filter fields, never a restriction-profile write (rule 5)...
        assert sorted(call.args[1]) == ["filterMovies", "filterTelevision"]
        # ...and their VALUES. Asserting only the field names is bug-blind: a managed user written
        # the wrong exclude sees every row the filter was supposed to hide, with the test still green.
        assert call.args[1]["filterMovies"] == "label!=Shortlist_sarah"
        assert call.args[1]["filterTelevision"] == "label!=Shortlist_sarah"

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
        stored = {"mike": "Shortlist_mike", "steve": "Shortlist_steve"}

        wrote = sync_user_restrictions(
            mock_plextv, sarah, mock_plextv.get_user(sarah.plex_account_id), stored, snapshot_store
        )

        # The return value IS the audit record: what changed, on which field, from what to what.
        assert wrote == {
            "filterMovies": ("contentRating!=R", "contentRating!=R|label!=Shortlist_mike,Shortlist_steve"),
            "filterTelevision": ("", "label!=Shortlist_mike,Shortlist_steve"),
        }
        assert snapshot_store.saved[100].filters["filterMovies"] == "contentRating!=R"
        call = mock_plextv.update_user_filters.call_args
        assert call.args[0] == 100
        # Both fields merged; foreign condition preserved byte-identical; stored (title-cased) labels used.
        assert call.args[1]["filterMovies"] == "contentRating!=R|label!=Shortlist_mike,Shortlist_steve"
        assert call.args[1]["filterTelevision"] == "label!=Shortlist_mike,Shortlist_steve"

    def test_prunes_a_stale_shared_exclude_but_keeps_private_and_foreign(self, mock_plextv, snapshot_store):
        """A re-enabled user (or one added to a shared row's audience) must get the shared-row exclude
        REMOVED so the row is restored — but a private-row exclude and any foreign condition stay.
        This is the only place we remove an exclude, and only ever for a shared row (never a leak)."""
        sarah = self._users()[0]  # account 100, not opted out
        mock_plextv.users = [
            plextv_user(
                100,
                "sarah",
                filters={
                    # A public shared exclude left from when she was disabled, plus a private exclude and
                    # a foreign condition that must both survive.
                    "filterMovies": "contentRating!=R|label!=Shortlist__shared_popular,Shortlist_mike",
                    "filterTelevision": "label!=Shortlist__shared_popular,Shortlist_mike",
                },
            )
        ]
        stored = {"mike": "Shortlist_mike", "shared_popular": "Shortlist__shared_popular"}
        shared = {"shortlist__shared_popular": None}  # configured PUBLIC shared row

        wrote = sync_user_restrictions(
            mock_plextv,
            sarah,
            mock_plextv.get_user(sarah.plex_account_id),
            stored,
            snapshot_store,
            shared_labels=shared,
        )

        # The public shared exclude is pruned; the private one and the foreign condition remain.
        assert wrote["filterMovies"][1] == "contentRating!=R|label!=Shortlist_mike"
        assert wrote["filterTelevision"][1] == "label!=Shortlist_mike"

    def test_a_stale_private_exclude_is_never_pruned(self, mock_plextv, snapshot_store):
        """The leak-safe boundary: only SHARED excludes are ever removed. A stale PRIVATE exclude (a
        label not in stored/wanted and not a configured shared row) stays — removing a private exclude
        is the leak direction, so the sync never does it, even for a label pointing at a deleted row."""
        sarah = self._users()[0]
        both = "label!=Shortlist_ghost,Shortlist_mike"  # a stale private exclude (ghost) + a live one
        mock_plextv.users = [plextv_user(100, "sarah", filters={"filterMovies": both, "filterTelevision": both})]
        stored = {"mike": "Shortlist_mike"}  # Shortlist_ghost is gone from the server, and it's private
        wrote = sync_user_restrictions(
            mock_plextv, sarah, mock_plextv.get_user(100), stored, snapshot_store, shared_labels={}
        )
        # Nothing to add (mike present) and ghost is private, so nothing is pruned -> zero writes, and
        # the stale private exclude is left exactly where it is (removing it would be the leak direction).
        assert wrote is None

    def test_steady_state_makes_zero_writes(self, mock_plextv, snapshot_store):
        sarah = self._users()[0]
        mock_plextv.users = [
            plextv_user(
                100,
                "sarah",
                filters={
                    "filterMovies": "label!=Shortlist_mike,Shortlist_steve",
                    "filterTelevision": "label!=Shortlist_mike,Shortlist_steve",
                },
            )
        ]
        stored = {"mike": "Shortlist_mike", "steve": "Shortlist_steve"}
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
            {"mike": "Shortlist_mike"},
            snapshot_store,
            dry_run=True,
        )
        assert wrote == {
            "filterMovies": ("", "label!=Shortlist_mike"),
            "filterTelevision": ("", "label!=Shortlist_mike"),
        }
        mock_plextv.update_user_filters.assert_not_called()
        assert snapshot_store.saved == {}

    def test_writes_without_a_per_user_readback(self, mock_plextv, snapshot_store):
        """The per-user GET/verify was O(A^2) and moved to one batched roster read in the pipeline
        (the read-back at the end of _privacy_sync_phase). sync_user_restrictions now only writes +
        returns the diff; it must NOT read the roster back itself."""
        sarah = self._users()[0]
        mock_plextv.users = [plextv_user(100, "sarah")]
        mock_plextv.update_user_filters.side_effect = lambda _id, fields: mock_plextv.users[0].filters.update(fields)
        remote = mock_plextv.get_user(sarah.plex_account_id)
        mock_plextv.get_user.reset_mock()

        diff = sync_user_restrictions(mock_plextv, sarah, remote, {"mike": "Shortlist_mike"}, snapshot_store)

        assert diff == {
            "filterMovies": ("", "label!=Shortlist_mike"),
            "filterTelevision": ("", "label!=Shortlist_mike"),
        }
        mock_plextv.update_user_filters.assert_called_once()
        mock_plextv.get_user.assert_not_called()


class TestRestore:
    def test_restore_puts_only_diverged_fields_byte_identical(self, mock_plextv):
        from datetime import UTC, datetime

        from shortlist.engine.models import FilterSnapshot

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
                    "filterMovies": "contentRating!=R|label!=Shortlist_mike",
                    "filterTelevision": "label!=Shortlist_mike",
                },
            )
        ]

        def put(account_id, fields):
            mock_plextv.users[0].filters.update(fields)

        mock_plextv.update_user_filters.side_effect = put
        assert privacy.restore_user_restrictions(mock_plextv, snapshot) is True
        call = mock_plextv.update_user_filters.call_args
        assert call.args[1] == {"filterMovies": "contentRating!=R", "filterTelevision": ""}

    def test_restore_raises_when_the_readback_does_not_match(self, mock_plextv):
        """The write is verified: if Plex accepts it but the value doesn't actually change, the
        restore must FAIL loudly rather than report a clean uninstall over stale filters."""
        from datetime import UTC, datetime

        from shortlist.engine.models import FilterSnapshot

        snapshot = FilterSnapshot(
            plex_account_id=100,
            username="sarah",
            taken_at=datetime.now(UTC),
            filters={"filterMovies": "contentRating!=R"},
        )
        mock_plextv.users = [
            plextv_user(100, "sarah", filters={"filterMovies": "contentRating!=R|label!=Shortlist_mike"})
        ]
        # The write silently doesn't take — the read-back still shows the shortlist exclude.
        mock_plextv.update_user_filters.side_effect = lambda account_id, fields: None
        with pytest.raises(RuntimeError, match="restore mismatch"):
            privacy.restore_user_restrictions(mock_plextv, snapshot)


class TestFilterDiffSummary:
    """A 48-user server puts every other account's exclude in each account's filter string, so
    logging the before AND after was ~8 KB per user per field — the same 47 labels every time, with
    the one that changed buried in the middle. The full diff still goes to the audit event."""

    PREFIX = "Shortlist"

    def _long_filter(self, n: int, extra: str = "") -> str:
        labels = [f"{self.PREFIX}_user{i}" for i in range(n)]
        if extra:
            labels.append(extra)
        return "label!=" + ",".join(labels)

    def test_it_names_only_what_changed(self):
        before, after = self._long_filter(47), self._long_filter(47, f"{self.PREFIX}_s_flix")

        summary = summarise_filter_diff({"filterMovies": (before, after)}, self.PREFIX)

        assert summary == "filterMovies +1 (Shortlist_s_flix)"
        assert "user0" not in summary, "the 47 unchanged labels are noise"
        assert len(summary) < 100, f"a log line, not a dump: {len(summary)} chars"

    def test_a_first_run_adding_everyone_stays_short(self):
        summary = summarise_filter_diff({"filterMovies": ("", self._long_filter(47))}, self.PREFIX)

        assert "+47" in summary
        assert "+44 more" in summary, "list a few, count the rest"
        assert len(summary) < 120

    def test_removals_are_reported_too(self):
        before, after = self._long_filter(3), self._long_filter(2)

        assert summarise_filter_diff({"filterMovies": (before, after)}, self.PREFIX) == (
            "filterMovies -1 (Shortlist_user2)"
        )

    def test_both_fields_are_covered(self):
        before, after = self._long_filter(1), self._long_filter(2)

        summary = summarise_filter_diff(
            {"filterMovies": (before, after), "filterTelevision": (before, after)}, self.PREFIX
        )

        assert summary.count("Shortlist_user1") == 2
        assert "filterMovies" in summary and "filterTelevision" in summary

    def test_a_change_outside_our_labels_is_still_reported(self):
        """Pruning a shared row's label can leave our own exclude set identical — the write still
        happened, so the log must not claim nothing changed."""
        summary = summarise_filter_diff(
            {"filterMovies": ("label!=Shortlist_a,other", "label!=Shortlist_a")}, self.PREFIX
        )

        assert summary == "filterMovies rewritten"
