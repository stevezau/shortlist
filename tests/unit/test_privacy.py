"""Privacy module tests — the merge code is the highest-consequence code in the repo."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine import privacy
from shortlist.engine.clients.plextv import PlexTvUser
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

# Raw filter values. `,` `|` `=` `!` really are syntax and never appear inside a value — but `&` is
# NOT in that list: it is a separator in Plex Web's dialect and an ordinary character in a label
# ("Kids & Family"), so a filter using both at once is genuinely ambiguous and no parser can resolve
# it. `&`-in-value is therefore covered by explicit `|`-separated cases below rather than generated
# here, where hypothesis would happily build the ambiguous combination.
value = st.text(alphabet=st.sampled_from("abcdefgXYZ0123456789_%."), min_size=1, max_size=12)
field_name = st.sampled_from(["label", "contentRating", "genre", "year"])
# Both separators of each kind, because plex.tv stores whatever the last writer used and hands it
# straight back — live-verified 2026-08-10. Plex Web writes `&` with `%2C`, plexapi writes `|` with
# `%2C`, Shortlist writes `|` with `,`; a filter can be in any of those shapes when we read it.
condition_sep = st.sampled_from(["|", "&"])
value_sep = st.sampled_from([",", "%2C"])


@st.composite
def _condition(draw):
    values = tuple(draw(st.lists(value, min_size=1, max_size=4)))
    return FilterCondition(
        draw(field_name),
        draw(st.sampled_from(["=", "!="])),
        values,
        sep=draw(condition_sep),
        value_seps=tuple(draw(st.lists(value_sep, min_size=len(values) - 1, max_size=len(values) - 1))),
    )


condition = _condition()
filter_string = st.lists(condition, min_size=0, max_size=5).map(serialize_filter)


class TestParseSerializeRoundTrip:
    @given(filter_string)
    def test_round_trip_is_byte_identical(self, raw: str):
        assert serialize_filter(parse_filter(raw)) == raw

    def test_parse_empty_returns_no_conditions(self):
        assert parse_filter("") == []

    def test_parse_preserves_raw_urlencoded_values(self):
        # Asserted field-by-field, not by dataclass equality: the condition also carries the
        # separators it was read with, and this test is about the VALUES surviving undecoded.
        (cond,) = parse_filter("label!=Some%20Label,other")
        assert (cond.field, cond.op, cond.values) == ("label", "!=", ("Some%20Label", "other"))

    def test_parse_rejects_garbage_instead_of_clobbering(self):
        with pytest.raises(FilterParseError):
            parse_filter("label!=ok|garbage-without-operator")


class TestTheFormsPlexActuallyWrites:
    """Issue #77. plex.tv stores a filter byte-for-byte and hands it straight back — verified live on
    2026-08-10 against a real account: `|`+plain, `|`+`%2C`, `&`+plain and `&`+`%2C` all round-tripped
    identically, with no server-side normalization.

    So the shape we read is whichever writer last touched that account. Plex Web writes `&` with
    URL-encoded values, plexapi writes `|` with `%2C` (`myplex.py:_filterDictToStr`), and Shortlist
    writes plain commas. Splitting only on `|` and `,` mis-parsed Plex Web's form into ONE condition
    whose field was `'label=Age%200%2CAge%203&label'`, so the existing exclude clause was invisible,
    the merge appended a second one, and the account's Restrictions tab in Plex Web died with
    "Something went wrong" until the value was rewritten by hand.
    """

    # The exact string from the report, captured from Plex Web's own save request.
    PLEX_WEB = "label=Age%200%2CAge%203&label!=Shortlist_a%2CShortlist_b"

    def test_the_allow_clause_is_not_swallowed_into_the_field_name(self):
        allow, exclude = parse_filter(self.PLEX_WEB)

        assert (allow.field, allow.op, allow.values) == ("label", "=", ("Age%200", "Age%203"))
        assert (exclude.field, exclude.op, exclude.values) == ("label", "!=", ("Shortlist_a", "Shortlist_b"))

    def test_a_new_label_joins_the_existing_exclude_clause(self):
        """The corruption itself: a second `label!=` fragment appended with a THIRD separator.
        Plex Web cannot read the result, and the user is locked out of their own Restrictions tab."""
        merged = merge_label_excludes(self.PLEX_WEB, {"shortlist_test"})

        assert merged == "label=Age%200%2CAge%203&label!=Shortlist_a%2CShortlist_b%2Cshortlist_test"
        assert merged.count("label!=") == 1, "a second exclude clause is what breaks Plex Web"

    def test_an_appended_label_matches_the_encoding_already_in_use(self):
        """Joining with a plain comma inside a `%2C`-joined clause leaves a value no reader can split."""
        merged = merge_label_excludes("label!=A%2CB", {"shortlist_x"})

        assert merged == "label!=A%2CB%2Cshortlist_x"

    def test_our_own_excludes_are_visible_when_percent_encoded(self):
        """`shortlist_labels_in` drives the uninstall and every count of "is this row hidden". Reading
        an encoded copy of our own label as foreign made Shortlist blind to its own writes."""
        assert shortlist_labels_in("label!=Shortlist%5Fmike%2CShortlist_dan", "shortlist") == {
            "Shortlist%5Fmike",
            "Shortlist_dan",
        }

    def test_removal_works_on_the_encoded_form_so_uninstall_can_finish(self):
        assert remove_label_excludes(self.PLEX_WEB, {"Shortlist_a"}) == "label=Age%200%2CAge%203&label!=Shortlist_b"

    def test_an_already_excluded_label_is_not_added_again_in_the_other_encoding(self):
        assert merge_label_excludes("label!=Age%200", {"Age 0"}) == "label!=Age%200"

    def test_a_new_exclude_clause_uses_the_separator_the_filter_already_uses(self):
        """An allow-list with no exclude clause yet. Appending with the wrong separator is how the
        three-fragment value appeared in the first place."""
        merged = merge_label_excludes("label=Age 0,Age 3&contentRating=PG", {"shortlist_x"})

        assert merged == "label=Age 0,Age 3&contentRating=PG&label!=shortlist_x"

    @pytest.mark.parametrize(
        "raw",
        [
            "label=Age%200%2CAge%203&label!=Shortlist_a%2CShortlist_b",
            "label=Age 0,Age 3|label!=Shortlist_a,Shortlist_b",
            "label=Age 0,Age 3&label!=Shortlist_a,Shortlist_b",
            "label=A%2CB|label!=C%2CD",
            "label!=A,B|contentRating!=R",
            "label!=",
        ],
    )
    def test_every_real_world_shape_round_trips_byte_identical(self, raw):
        assert serialize_filter(parse_filter(raw)) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "label!=Kids & Family",
            "label!=Kids & Family,Shortlist_a",
            "label=Age 0,Age 3|label!=Kids & Family",
            "contentRating!=R|label!=Rock & Roll",
        ],
    )
    def test_an_ampersand_inside_a_LABEL_is_not_a_separator(self, raw):
        """`&` is a condition separator in Plex Web's dialect AND an ordinary character in a label —
        "Kids & Family" is a label a person can create. Splitting on it unconditionally left
        `' Family'` with no operator, which raised, which blocks promotion for EVERY user on the
        server until somebody hand-edits that one account (the #14 shape). These parsed fine before
        `&` was understood at all, and must keep parsing fine."""
        assert serialize_filter(parse_filter(raw)) == raw
        merged = merge_label_excludes(raw, {"shortlist_new"})
        assert merged.startswith(raw), "the existing filter must survive byte-identical"
        assert "shortlist_new" in merged

    def test_a_field_that_still_holds_a_separator_is_refused_not_rewritten(self):
        """Belt to the parser's braces. If Plex ever invents a fourth shape, refusing to touch it is
        the safe failure — rewriting one we only half-understand is how filters get corrupted."""
        with pytest.raises(FilterParseError):
            parse_filter("label=A;;label!=B")


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

    def test_a_kids_allow_list_survives_the_whole_write_path(self, mock_plextv, snapshot_store):
        """Issue #77, end to end — the primitives being right is not enough, because this is the
        path that actually reaches plex.tv.

        The account is a managed user with an age allow-list, written by Plex Web in its own form
        (`&` between conditions, `%2C` between values). That is the exact shape 2 of one reporter's
        16 accounts carried. What used to go out was a THIRD fragment appended with `|`, which Plex
        Web could not parse: the user was locked out of their own Restrictions tab until somebody
        rewrote the value by hand, and their allow-list was left inside a bogus field name.

        Asserted on the VALUE plex.tv is handed, not on "a write happened" — the call count was
        always right here; the string was not.
        """
        kid = make_profile("kid", user_type=UserType.MANAGED, account_id=400)
        plex_web_form = "label=Age%200%2CAge%203&label!=Shortlist_sarah"
        mock_plextv.users = [
            plextv_user(400, "kid", filters={"filterMovies": plex_web_form, "filterTelevision": plex_web_form})
        ]
        mock_plextv.update_user_filters.side_effect = lambda _id, fields: mock_plextv.users[0].filters.update(fields)

        sync_user_restrictions(
            mock_plextv,
            kid,
            mock_plextv.get_user(kid.plex_account_id),
            {"sarah": "Shortlist_sarah", "mike": "Shortlist_mike"},
            snapshot_store,
        )

        sent = mock_plextv.update_user_filters.call_args.args[1]["filterMovies"]
        assert sent == "label=Age%200%2CAge%203&label!=Shortlist_sarah%2CShortlist_mike"
        assert sent.count("label!=") == 1, "a second exclude clause is what breaks Plex Web"
        assert sent.startswith("label=Age%200%2CAge%203&"), "the kid's age allow-list must survive untouched"

    def test_a_kids_allow_list_makes_no_write_when_already_correct(self, mock_plextv, snapshot_store):
        """Steady state on the combined form. Before the fix this account was rewritten EVERY run,
        because the exclude clause it already had was invisible to the parser."""
        kid = make_profile("kid", user_type=UserType.MANAGED, account_id=400)
        settled = "label=Age%200%2CAge%203&label!=Shortlist_sarah"
        mock_plextv.users = [plextv_user(400, "kid", filters={"filterMovies": settled, "filterTelevision": settled})]

        wrote = sync_user_restrictions(
            mock_plextv,
            kid,
            mock_plextv.get_user(kid.plex_account_id),
            {"sarah": "Shortlist_sarah"},
            snapshot_store,
        )

        assert wrote is None
        mock_plextv.update_user_filters.assert_not_called()

    def test_owner_is_never_restricted(self, mock_plextv, snapshot_store):
        _sarah, _mike, owner = self._users()
        # The owner is not even on plex.tv's user list, so `remote` is None: they are skipped
        # before it is ever read (Plex cannot restrict the owner — rule 5).
        wrote = sync_user_restrictions(mock_plextv, owner, None, {}, snapshot_store)
        assert wrote is None
        mock_plextv.update_user_filters.assert_not_called()

    def _managed(self, profile: str, filters: str = "") -> PlexTvUser:
        """A Plex Home account. `restricted` is True either way — that is the whole point of #20:
        `/api/users` cannot tell a parental-controlled account from a plain managed one."""
        return PlexTvUser(
            id=500,
            username="kid",
            user_type=UserType.MANAGED,
            home=True,
            restricted=True,
            protected=False,
            restriction_profile=profile,
            filters={
                "filterAll": "",
                "filterMovies": filters,
                "filterTelevision": "",
                "filterMusic": "",
                "filterPhotos": "",
            },
        )

    def test_a_parental_profile_is_skipped_without_calling_plextv(self, mock_plextv, snapshot_store):
        """Plex refuses label restrictions outright while a preset is applied — its own docs say the
        profile "must be set to None if you wish to edit Rating and Label restrictions". Live-confirmed
        2026-07-29: a `little_kid` account 422s the write and sees 0 collections of any kind, so there
        is nothing an exclude could hide. Skipping keeps one such account from blocking promotion for
        the whole server (#14)."""
        kid = make_profile("kid", user_type=UserType.MANAGED, account_id=500)

        wrote = sync_user_restrictions(
            mock_plextv,
            kid,
            self._managed("little_kid", "contentRating=G"),
            {"sarah": "Shortlist_sarah"},
            snapshot_store,
        )

        assert wrote is None
        mock_plextv.update_user_filters.assert_not_called()

    def test_a_managed_account_with_NO_profile_gets_its_excludes(self, mock_plextv, snapshot_store):
        """Issue #20. `/api/users` reports `restricted="1"` for every managed account, so keying the
        skip on it also skipped managed users with no age restriction — who see everything and are
        exactly who the excludes exist for. Plex accepts label restrictions for these."""
        kid = make_profile("kid", user_type=UserType.MANAGED, account_id=500)

        wrote = sync_user_restrictions(
            mock_plextv, kid, self._managed(""), {"sarah": "Shortlist_sarah"}, snapshot_store
        )

        assert wrote is not None, "a managed user with no parental profile must be given their excludes"
        assert "Shortlist_sarah" in wrote["filterMovies"][1]
        mock_plextv.update_user_filters.assert_called_once()

    def test_a_profile_less_account_keeps_any_filters_it_already_had(self, mock_plextv, snapshot_store):
        """Rule 3 — merge, never rebuild. Writing excludes for a managed user must not disturb whatever
        the owner set by hand."""
        kid = make_profile("kid", user_type=UserType.MANAGED, account_id=500)

        wrote = sync_user_restrictions(
            mock_plextv, kid, self._managed("", "contentRating=PG"), {"sarah": "Shortlist_sarah"}, snapshot_store
        )

        assert wrote["filterMovies"][1].startswith("contentRating=PG")
        assert "Shortlist_sarah" in wrote["filterMovies"][1]

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
            # Required for ANY prune. `wanted` is derived from `stored`, so a PMS that answers with no
            # collections makes every shared exclude look unwanted — this says the enumeration is real.
            collections_known=True,
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


class TestSelfExclusionIsHealed:
    """An account's OWN label must never sit in its own filter — that hides a person from their own
    row, permanently, because private-row excludes are otherwise union-only (removing one is the
    leak direction, so nothing prunes them).

    Reachable: delete a user's DB row while their collection still exists on Plex. `own_label`
    resolves to None, so `desired_excludes` adds their own label to their own filter — and re-adding
    the user later never undid it.
    """

    def _user(self):
        from shortlist.engine.models import UserProfile, UserType

        return UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED, slug="sarah")

    def test_an_account_that_excluded_itself_gets_it_removed(self, mock_plextv):
        from shortlist.engine.privacy import sync_user_restrictions
        from tests.conftest import MemorySnapshotStore, plextv_user

        remote = plextv_user(201, "sarah", filters={"filterMovies": "label!=Shortlist_sarah,Shortlist_mike"})
        written = sync_user_restrictions(
            mock_plextv,
            self._user(),
            remote,
            {"sarah": "Shortlist_sarah", "mike": "Shortlist_mike"},
            MemorySnapshotStore(),
            own_label="Shortlist_sarah",
        )

        assert written is not None
        after = written["filterMovies"][1]
        assert "Shortlist_sarah" not in after  # they can see their own row again
        assert "Shortlist_mike" in after  # everyone else's stays hidden

    def test_another_persons_label_is_never_pruned(self, mock_plextv):
        """Only the account's OWN label. Removing anyone else's is the leak direction."""
        from shortlist.engine.privacy import sync_user_restrictions
        from tests.conftest import MemorySnapshotStore, plextv_user

        remote = plextv_user(201, "sarah", filters={"filterMovies": "label!=Shortlist_mike,Shortlist_canary"})
        written = sync_user_restrictions(
            mock_plextv,
            self._user(),
            remote,
            {"mike": "Shortlist_mike"},  # canary's collection is gone from the server
            MemorySnapshotStore(),
            own_label="Shortlist_sarah",
        )

        after = (written or {}).get("filterMovies", ("", "label!=Shortlist_mike,Shortlist_canary"))[1]
        assert "Shortlist_mike" in after
        assert "Shortlist_canary" in after  # stale, but pruning it is the leak direction


class TestRestrictedAccountFilters:
    """Pins the real shape of a RESTRICTED (managed) account's share filters — recorded from a live
    PMS, per plex-safety rule 11.

    This is the assumption behind `sync_user_restrictions` skipping these accounts entirely. The
    fixture shows the skip is not because there is nowhere to write: the account already carries
    `contentRating=` filters, and a `label!=` condition merges alongside one like any other.
    """

    def _fixture(self) -> dict:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "fixtures" / "plextv_restricted_user.json"
        return json.loads(path.read_text())

    def test_a_restricted_account_carries_content_rating_filters_not_label_ones(self):
        from shortlist.engine.privacy import parse_filter, shortlist_labels_in

        raw = self._fixture()["filters"]["filterMovies"]
        fields = {c.field for c in parse_filter(raw)}

        assert fields == {"contentRating"}  # a parental profile, not a Shortlist exclude
        assert shortlist_labels_in(raw, "shortlist") == set()

    def test_merging_excludes_preserves_the_parental_filter_byte_for_byte(self):
        """The reason issue #20 is fixable: writing label excludes for a managed user does not mean
        touching their parental controls. Rule 3 — merge, never rebuild."""
        from shortlist.engine.privacy import merge_label_excludes, parse_filter

        raw = self._fixture()["filters"]["filterMovies"]
        merged = merge_label_excludes(raw, {"Shortlist_sarah", "Shortlist_mike"})

        original, *rest = parse_filter(merged)
        assert original == parse_filter(raw)[0]  # the contentRating condition is untouched
        assert [c.field for c in rest] == ["label"]
        assert set(rest[0].values) == {"Shortlist_mike", "Shortlist_sarah"}
        assert raw in merged  # byte-preserved, URL-encoding and all

    def test_the_merge_round_trips(self):
        """`serialize_filter(parse_filter(s)) == s` must hold for anything plex.tv hands us — these
        values are URL-encoded and must never be decoded on the way through."""
        from shortlist.engine.privacy import parse_filter, serialize_filter

        for raw in self._fixture()["filters"].values():
            assert serialize_filter(parse_filter(raw)) == raw


class TestDeadSharedRowExcludesArePruned:
    """A `shortlist__shared_*` exclude for a row that no longer EXISTS on the server.

    Deleting a shared row, or flipping it to per-person, takes its label out of `shared_labels` — so
    the normal shared prune stops considering it and the dead entry sits in every account's filter for
    ever. On a 48-user server each filter already carries every other account's exclude; dead ones
    accumulate on top with nothing to ever collect them.

    Safe to remove ONLY because the collection is gone: an exclude that matches nothing cannot be
    hiding anything. That reasoning depends entirely on KNOWING it is gone, which is why it is gated
    on a successful enumeration rather than on an empty lookup.
    """

    def _user(self):
        from shortlist.engine.models import UserProfile, UserType

        return UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED, slug="sarah")

    def _sync(self, mock_plextv, *, stored, collections_known, filters):
        from shortlist.engine.privacy import sync_user_restrictions
        from tests.conftest import MemorySnapshotStore, plextv_user

        return sync_user_restrictions(
            mock_plextv,
            self._user(),
            plextv_user(201, "sarah", filters=filters),
            stored,
            MemorySnapshotStore(),
            own_label="Shortlist_sarah",
            shared_labels={},  # the row is no longer declared shared — that IS the scenario
            collections_known=collections_known,
        )

    def test_a_dead_shared_label_is_removed_once_we_know_it_is_gone(self, mock_plextv):
        written = self._sync(
            mock_plextv,
            stored={"sarah": "Shortlist_sarah", "mike": "Shortlist_mike"},
            collections_known=True,
            filters={"filterMovies": "label!=Shortlist_mike,shortlist__shared_popular"},
        )

        assert written is not None
        after = written["filterMovies"][1]
        assert "shortlist__shared_popular" not in after
        assert "Shortlist_mike" in after  # a live private row's exclude is untouched

    def test_a_shared_label_whose_collection_still_exists_is_left_alone(self, mock_plextv):
        """The row was un-declared in config but its collection is still on the server — so the
        exclude is doing real work, and `desired_excludes` re-adds it fail-safe. Pruning here would
        make a live row public."""
        written = self._sync(
            mock_plextv,
            stored={"sarah": "Shortlist_sarah", "shared_popular": "shortlist__shared_popular"},
            collections_known=True,
            filters={"filterMovies": "label!=shortlist__shared_popular"},
        )

        after = (written or {}).get("filterMovies", ("", "label!=shortlist__shared_popular"))[1]
        assert "shortlist__shared_popular" in after

    def test_nothing_is_pruned_when_the_collections_could_not_be_read(self, mock_plextv):
        """ "I could not enumerate the collections" and "that collection is gone" look identical from
        here, and one of those readings un-hides a live row. Not knowing must mean not touching."""
        written = self._sync(
            mock_plextv,
            stored={},  # the read failed, so we know nothing
            collections_known=False,
            filters={"filterMovies": "label!=shortlist__shared_popular"},
        )

        after = (written or {}).get("filterMovies", ("", "label!=shortlist__shared_popular"))[1]
        assert "shortlist__shared_popular" in after

    def test_a_private_rows_exclude_is_never_pruned_even_when_its_collection_is_gone(self, mock_plextv):
        """Only SHARED labels. A private row's exclude stays union-only: removing one is the leak
        direction, and a missing collection may just be a row that failed to build tonight."""
        written = self._sync(
            mock_plextv,
            stored={"sarah": "Shortlist_sarah"},
            collections_known=True,
            filters={"filterMovies": "label!=Shortlist_canary"},
        )

        after = (written or {}).get("filterMovies", ("", "label!=Shortlist_canary"))[1]
        assert "Shortlist_canary" in after

    def test_an_empty_but_successful_read_prunes_nothing(self, mock_plextv):
        """The exact hole the first version had. A PMS mid library-index rebuild answers 200 with no
        collections — indistinguishable from "every row is gone" — and `collections_known` was set
        purely from "the call did not raise". Acting on that reading strips shared excludes from every
        account on the server at once."""
        written = self._sync(
            mock_plextv,
            stored={},  # a successful call that returned nothing
            collections_known=True,
            filters={"filterMovies": "label!=shortlist__shared_popular,Shortlist_mike"},
        )

        after = (written or {}).get("filterMovies", ("", "label!=shortlist__shared_popular,Shortlist_mike"))[1]
        assert "shortlist__shared_popular" in after

    def test_a_live_restricted_row_keeps_its_exclude_for_an_out_of_audience_account(self, mock_plextv):
        """The row EXISTS and the config restricts it to accounts this one is not in — so the exclude
        is the only thing hiding it, and no branch may take it away. `dead_shared` additionally
        requires the config to have stopped declaring it, so a live row can never reach that path."""
        from shortlist.engine.privacy import sync_user_restrictions
        from tests.conftest import MemorySnapshotStore, plextv_user

        written = sync_user_restrictions(
            mock_plextv,
            self._user(),
            plextv_user(201, "sarah", filters={"filterMovies": "label!=shortlist__shared_popular"}),
            {"sarah": "Shortlist_sarah", "shared_popular": "shortlist__shared_popular"},
            MemorySnapshotStore(),
            own_label="Shortlist_sarah",
            shared_labels={"shortlist__shared_popular": {999}},  # audience 999 — NOT this account
            collections_known=True,
        )

        after = (written or {}).get("filterMovies", ("", "label!=shortlist__shared_popular"))[1]
        assert "shortlist__shared_popular" in after
