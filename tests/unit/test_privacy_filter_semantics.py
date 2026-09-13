"""A share filter must be ENFORCED by Plex, not just stored (#116, #115).

plex.tv stores `contentRating!=R|label!=shortlist_x` verbatim and hands it back, so every read-back
passes — but a real PMS reads `|` as OR, so that string hides nothing of ours and voids the owner's own
rating exclude as well (`tests/fixtures/pms_share_filter_boolean_semantics.json`).

The evaluator below is written from that RECORDED behaviour and deliberately does not use
`privacy.parse_filter`: a check built on the parser under test cannot catch the parser being wrong.
Two grouping models survive the recording — strict left-to-right, and `|` binding tighter than `&`
— and the fixture cannot tell them apart, so every property must hold under BOTH.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine.privacy import (
    AmbiguousFilterError,
    FilterParseError,
    merge_label_excludes,
    remove_label_excludes,
    summarise_filter_diff,
    unenforced_excludes,
    voids_owner_restriction,
)
from tests.strategies import filter_string

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pms_share_filter_boolean_semantics.json"
PREFIX = "shortlist"
OURS = ("shortlist_a", "shortlist_b", "shortlist_c")


def _conditions(raw: str) -> list[tuple[str, str, str, set[str]]]:
    """``(separator-before, field, op, values)`` — an independent reading of the filter grammar."""
    if not raw:
        return []
    chunks = re.split(r"([|&])", raw)
    out = []
    for i in range(0, len(chunks), 2):
        sep = chunks[i - 1] if i else "&"
        field, op, rest = re.match(r"^([A-Za-z]+)(!=|=)(.*)$", chunks[i]).groups()
        out.append((sep, field, op, {unquote(v).casefold() for v in re.split(r"%2C|%2c|,", rest) if v}))
    return out


def _holds(condition, item: dict[str, set[str]]) -> bool:
    _sep, field, op, values = condition
    hit = bool({v.casefold() for v in item.get(field, set())} & values)
    return hit if op == "=" else not hit


def visible_left_to_right(raw: str, item: dict[str, set[str]]) -> bool:
    conditions = _conditions(raw)
    if not conditions:
        return True
    result = _holds(conditions[0], item)
    for condition in conditions[1:]:
        result = (result and _holds(condition, item)) if condition[0] == "&" else (result or _holds(condition, item))
    return result


def visible_or_binds_tighter(raw: str, item: dict[str, set[str]]) -> bool:
    groups: list[list] = []
    for condition in _conditions(raw):
        if condition[0] == "&" or not groups:
            groups.append([condition])
        else:
            groups[-1].append(condition)
    return all(any(_holds(c, item) for c in group) for group in groups)


def visible_and_binds_tighter(raw: str, item: dict[str, set[str]]) -> bool:
    groups: list[list] = []
    for condition in _conditions(raw):
        if condition[0] == "|" or not groups:
            groups.append([condition])
        else:
            groups[-1].append(condition)
    return any(all(_holds(c, item) for c in group) for group in groups)


MODELS = (visible_left_to_right, visible_or_binds_tighter)


class TestTheEvaluatorMatchesARealServer:
    """The models above are only worth something if a real PMS agrees with them."""

    OTHER = "shortlist_other"
    OWN = "shortlist_me"

    def _expand(self, expression: str) -> str:
        return (
            expression.replace("OURS+OWN", f"label!={self.OTHER},{self.OWN}")
            .replace("OURS", f"label!={self.OTHER}")
            .replace("PROBE", "ZZShortlistProbe")
        )

    def _observations(self):
        for case in json.loads(FIXTURE.read_text())["cases"]:
            if "<" in case["filter"]:
                continue  # the literal-`&` shapes: asserted separately, they break Plex rather than filter
            raw = self._expand(case["filter"])
            # A Shortlist row is a collection: it carries labels and no content rating.
            if "own_movie_row_visible" in case:
                yield raw, {"label": {self.OWN}}, case["own_movie_row_visible"]
            if "others_rows_on_home" in case:
                yield raw, {"label": {self.OTHER}}, case["others_rows_on_home"] > 0

    @pytest.mark.parametrize("model", MODELS)
    def test_every_recorded_row_visibility_is_reproduced(self, model):
        for raw, row, visible in self._observations():
            assert model(raw, row) is visible, raw

    def test_the_recording_rules_out_and_binding_tighter(self):
        """Proves the fixture discriminates: the one grouping it refutes must fail somewhere."""
        assert any(visible_and_binds_tighter(raw, row) is not seen for raw, row, seen in self._observations())


class TestUnenforcedExcludes:
    @pytest.mark.parametrize(
        ("raw", "unenforced"),
        [
            ("label!=shortlist_a", set()),
            ("contentRating!=NC-17&label!=shortlist_a", set()),
            ("label!=shortlist_a&contentRating!=NC-17", set()),
            ("contentRating!=NC-17|label!=shortlist_a", {"shortlist_a"}),
            ("label!=shortlist_a|contentRating!=NC-17", {"shortlist_a"}),
            ("contentRating!=NC-17|contentRating=G&label!=shortlist_a", set()),
            ("label=Kids%2CFamily&label!=Shortlist%5Fa", set()),
            ("contentRating!=NC-17", {"shortlist_a"}),
        ],
    )
    def test_a_label_counts_only_where_plex_applies_it(self, raw, unenforced):
        assert unenforced_excludes(raw, {"shortlist_a"}) == unenforced


class TestMergeWritesWhatPlexEnforces:
    """Every cell of the filter-state matrix that reaches the merge, as it arrives from Plex."""

    def test_a_single_rating_exclude_is_joined_with_and(self):
        """#116 as recorded: the `|` form leaked 90 rows and voided the NC-17 exclude."""
        assert merge_label_excludes("contentRating!=NC-17", {"shortlist_a"}, label_prefix=PREFIX) == (
            "contentRating!=NC-17&label!=shortlist_a"
        )

    def test_an_allow_list_is_joined_with_and(self):
        """#115 as recorded: the `|` form let an allow-listed account see the whole library."""
        assert (
            merge_label_excludes("label=Kids", {"shortlist_a"}, label_prefix=PREFIX) == "label=Kids&label!=shortlist_a"
        )

    def test_a_pipe_joined_filter_from_another_tool_keeps_its_or_and_gains_a_trailing_and(self):
        merged = merge_label_excludes("contentRating!=R|genre=Horror", {"shortlist_a"}, label_prefix=PREFIX)
        assert merged == "contentRating!=R|genre=Horror&label!=shortlist_a"

    def test_an_exclude_clause_already_last_after_and_is_extended_in_place(self):
        merged = merge_label_excludes("label=Age%200&label!=Shortlist_a", {"shortlist_b"}, label_prefix=PREFIX)
        assert merged == "label=Age%200&label!=Shortlist_a,shortlist_b"

    def test_a_filter_of_only_our_excludes_is_left_alone(self):
        """Every account on SFLIX: one clause, enforced. Steady state must write nothing."""
        raw = "label!=shortlist_a,shortlist_b"
        assert merge_label_excludes(raw, {"shortlist_a", "shortlist_b"}, label_prefix=PREFIX) is raw

    def test_shortlists_own_pipe_merge_is_repaired_and_the_owners_restriction_comes_back(self):
        """What every affected account holds today. Nothing is missing, so the old merge returned it
        untouched every night — which is why no later run ever fixed it."""
        damaged = "contentRating!=NC-17|label!=shortlist_a,shortlist_b"
        merged = merge_label_excludes(damaged, {"shortlist_a", "shortlist_b"}, label_prefix=PREFIX)
        assert merged == "contentRating!=NC-17&label!=shortlist_a,shortlist_b"

    def test_the_repair_carries_every_shortlist_label_it_moves_not_only_the_wanted_ones(self):
        """A label missing from `labels` may be a live row the enumeration failed to see. Removing an
        exclude is the prune's decision, under its own guards — never a side effect of moving it."""
        merged = merge_label_excludes(
            "contentRating!=R|label!=shortlist_a,shortlist_gone", {"shortlist_a"}, label_prefix=PREFIX
        )
        assert unenforced_excludes(merged, {"shortlist_a", "shortlist_gone"}) == set()

    def test_our_labels_leave_a_mixed_clause_that_an_or_follows(self):
        raw = "contentRating!=R|label!=kids_hide,Shortlist_mike|genre=Horror"
        merged = merge_label_excludes(raw, {"Shortlist_mike", "Shortlist_sarah"}, label_prefix=PREFIX)
        assert merged == "contentRating!=R|label!=kids_hide|genre=Horror&label!=Shortlist_mike,Shortlist_sarah"

    def test_merge_is_idempotent_after_a_repair(self):
        once = merge_label_excludes("contentRating!=R|label!=shortlist_a", {"shortlist_a"}, label_prefix=PREFIX)
        assert merge_label_excludes(once, {"shortlist_a"}, label_prefix=PREFIX) is once

    def test_an_ampersand_inside_a_label_that_forces_an_and_is_refused(self):
        """`label=Kids & Family&label!=shortlist_a` cannot be read back as two conditions, and Plex itself
        answers that account's Home with HTTP 500 for a literal `&` in a label. Refused, and the run reports
        the account instead of writing a filter nothing could verify."""
        with pytest.raises(AmbiguousFilterError, match="Kids & Family"):
            merge_label_excludes("contentRating!=R|label!=Kids & Family", {"shortlist_a"}, label_prefix=PREFIX)

    def test_the_refusal_is_a_parse_error_so_every_existing_handler_treats_it_as_one(self):
        assert issubclass(AmbiguousFilterError, FilterParseError)

    @pytest.mark.parametrize("raw", ["label!=Kids & Family", "label!=Kids&Family,shortlist_a", "label=Rock & Roll"])
    def test_any_literal_ampersand_in_a_label_is_refused_because_plex_cannot_read_that_filter(self, raw):
        """Measured: with a literal `&` in a label, Plex answers that account's Home with HTTP 500, spaces or
        not. The filter is already broken for them, so nothing written into it can be verified — the
        account is reported instead, naming the label to rename (`%26` is fine and merges normally)."""
        with pytest.raises(AmbiguousFilterError, match="&"):
            merge_label_excludes(raw, {"shortlist_a"}, label_prefix=PREFIX)

    def test_an_encoded_ampersand_merges_normally(self):
        merged = merge_label_excludes("label=Kids%20%26%20Family", {"shortlist_a"}, label_prefix=PREFIX)
        assert merged == "label=Kids%20%26%20Family&label!=shortlist_a"

    def test_the_recording_says_a_literal_ampersand_breaks_plex_and_an_encoded_one_does_not(self):
        statuses = {c["filter"]: c.get("hubs_status") for c in json.loads(FIXTURE.read_text())["cases"]}
        assert statuses["label!=ZZ&Probe,<OURS+OWN values>"] == 500
        assert statuses["label!=ZZ%20%26%20Probe,<OURS+OWN values>"] == 200

    def test_removal_after_a_repair_hands_back_the_owners_filter(self):
        merged = merge_label_excludes("contentRating!=NC-17|label!=shortlist_a", {"shortlist_a"}, label_prefix=PREFIX)
        assert remove_label_excludes(merged, {"shortlist_a"}) == "contentRating!=NC-17"


class TestTheLogLineForARepair:
    def test_a_repair_says_what_it_did_rather_than_rewritten(self):
        """The label set is identical before and after, so the generic line said only "rewritten" —
        on the one write that changes what a person can see on every client."""
        line = summarise_filter_diff(
            {"filterMovies": ("contentRating!=R|label!=shortlist_a", "contentRating!=R&label!=shortlist_a")}, PREFIX
        )
        assert line == "filterMovies excludes moved to where Plex applies them"


class TestRemovalNeverRegroupsTheOwnersConditions:
    def test_an_emptied_clause_joined_by_and_with_an_or_after_it_is_left_in_place(self):
        """`A&label!=ours|B` minus ours would read `A|B` — wider than `A&(T|B)` if Plex binds `|` tighter,
        which the recording cannot rule out. An exclude of a label nothing carries hides nothing, so
        keeping it is free; regrouping the owner's conditions is not (architecture review 2026-09-13)."""
        raw = "contentRating!=R&label!=shortlist_a|genre=Horror"
        assert remove_label_excludes(raw, {"shortlist_a"}) == raw

    @pytest.mark.parametrize(
        ("raw", "cleaned"),
        [
            ("contentRating!=R|label!=shortlist_a", "contentRating!=R"),
            ("label!=shortlist_a|contentRating!=R", "contentRating!=R"),
            ("contentRating!=R&label!=shortlist_a", "contentRating!=R"),
        ],
    )
    def test_an_emptied_clause_that_cannot_regroup_anything_is_dropped(self, raw, cleaned):
        assert remove_label_excludes(raw, {"shortlist_a"}) == cleaned


class TestAMergeBugIsNeverMistakenForABadLabel:
    def test_a_merge_that_cannot_prove_itself_raises_a_plain_parse_error_so_promotion_blocks(self, monkeypatch):
        """`AmbiguousFilterError` is reported per account and does NOT block. A merge whose own output fails
        its check is a bug in this module, not a label to rename — it must reach `promotion_blockers`."""
        from shortlist.engine import privacy

        monkeypatch.setattr(privacy, "unenforced_excludes", lambda raw, labels: set(labels))
        with pytest.raises(FilterParseError) as caught:
            merge_label_excludes("contentRating!=R", {"shortlist_a"}, label_prefix=PREFIX)
        assert not isinstance(caught.value, AmbiguousFilterError)


class TestLeftAloneAccountsAreRepairedToo:
    """Review 2026-09-13: "leave their sharing alone" strips per-person excludes but must keep a restricted
    shared row's — and an old `|` merge left that one where Plex ignores it, with the owner's own
    restriction switched off and no run ever coming back to it."""

    def _remote(self, movies: str):
        from shortlist.engine.clients.plextv import PlexTvUser
        from shortlist.engine.models import UserType

        return PlexTvUser(
            id=500,
            username="kid",
            user_type=UserType.SHARED,
            home=False,
            restricted=False,
            protected=False,
            filters={
                "filterAll": "",
                "filterMovies": movies,
                "filterTelevision": "",
                "filterMusic": "",
                "filterPhotos": "",
            },
        )

    def test_the_kept_shared_exclude_moves_to_where_plex_applies_it(self, mock_plextv):
        from shortlist.engine import privacy
        from tests.conftest import make_profile

        kid = make_profile("kid", account_id=500)
        written = privacy.clear_our_excludes(
            mock_plextv, kid, self._remote("contentRating!=R|label!=shortlist_a,shortlist__shared_x")
        )

        after = written["filterMovies"][1]
        assert after == "contentRating!=R&label!=shortlist__shared_x"
        assert not voids_owner_restriction(after, PREFIX)

    def test_moving_shared_excludes_never_re_adds_a_per_person_one(self, mock_plextv):
        """Review 2026-09-13: the removal keep-rule leaves `label!=shortlist_bob` in place here, and the repair
        carried it to an enforced position — hiding bob's row from an account the owner said to leave alone."""
        from shortlist.engine import privacy
        from tests.conftest import make_profile

        kid = make_profile("kid", account_id=500)
        remote = self._remote("contentRating!=R&label!=shortlist_bob|label=Kids&label!=shortlist__shared_x")
        written = privacy.clear_our_excludes(mock_plextv, kid, remote)

        after = written["filterMovies"][1] if written else remote.filters["filterMovies"]
        assert unenforced_excludes(after, {"shortlist_bob"}) == {"shortlist_bob"}
        assert unenforced_excludes(after, {"shortlist__shared_x"}) == set()

    def test_a_label_plex_cannot_read_still_lets_the_per_person_excludes_come_out(self, mock_plextv):
        """Review 2026-09-13: the move of the shared exclude raised on the `&` label and took the whole clear
        with it, so the excludes the owner asked to remove stayed, every night."""
        from shortlist.engine import privacy
        from tests.conftest import make_profile

        kid = make_profile("kid", account_id=500)
        remote = self._remote("label=Kids & Family|label!=shortlist__shared_s,shortlist_a")

        written = privacy.clear_our_excludes(mock_plextv, kid, remote)

        assert written["filterMovies"][1] == "label=Kids & Family|label!=shortlist__shared_s"

    def test_a_per_person_clause_the_removal_must_keep_is_not_moved(self, mock_plextv):
        from shortlist.engine import privacy
        from tests.conftest import make_profile

        kid = make_profile("kid", account_id=500)
        written = privacy.clear_our_excludes(
            mock_plextv, kid, self._remote("contentRating!=R&label!=shortlist_bob|label=Kids")
        )

        assert written is None

    def test_a_left_alone_account_holding_only_an_unenforced_shared_exclude_is_still_repaired(self, mock_plextv):
        from shortlist.engine import privacy
        from tests.conftest import make_profile

        kid = make_profile("kid", account_id=500)
        written = privacy.clear_our_excludes(
            mock_plextv, kid, self._remote("contentRating!=R|label!=shortlist__shared_x")
        )

        assert written["filterMovies"][1] == "contentRating!=R&label!=shortlist__shared_x"


class TestVoidsOwnerRestriction:
    @pytest.mark.parametrize(
        ("raw", "voided"),
        [
            ("contentRating!=NC-17|label!=shortlist_a", True),
            ("label=Kids|label!=shortlist_a", True),
            ("contentRating!=NC-17&label!=shortlist_a", False),
            ("label!=shortlist_a,shortlist_b", False),
            ("contentRating!=R|genre=Horror", False),
            ("contentRating!=R&label!=shortlist_a|genre=Horror", False),
            ("label!=shortlist_a|contentRating!=R", True),
            ("", False),
        ],
    )
    def test_only_an_unenforced_shortlist_label_beside_an_owner_condition_counts(self, raw, voided):
        assert voids_owner_restriction(raw, PREFIX) is voided


@st.composite
def _case(draw, damaged: bool):
    raw = draw(filter_string)
    labels = draw(st.sets(st.sampled_from(OURS), min_size=1, max_size=3))
    if damaged:
        # Shortlist's pre-fix output wherever it can end up: appended with `|`, or somewhere in the middle.
        clause = "label!=" + ",".join(sorted(labels))
        parts = re.split(r"(?=[|&])", raw) if raw else []
        at = draw(st.integers(0, len(parts)))
        sep = draw(st.sampled_from("|&"))
        if not parts:
            raw = clause
        elif at == 0:
            raw = clause + sep + parts[0] + "".join(parts[1:])
        else:
            raw = "".join(parts[:at]) + sep + clause + "".join(parts[at:])
    pool = sorted({v for _s, _f, _o, values in _conditions(raw) for v in values} | {"zz"})
    item = {
        field: draw(st.sets(st.sampled_from(pool), max_size=3)) for field in ("label", "contentRating", "genre", "year")
    }
    if draw(st.booleans()):
        item["label"] = item["label"] | {draw(st.sampled_from(sorted(labels)))}
    return raw, labels, item


class TestMergeProperties:
    """Rule 3 as Plex sees it: the merge ANDs "not ours" onto the owner's filter and changes nothing else."""

    @given(_case(damaged=False))
    def test_any_filter_hides_our_rows_and_keeps_the_owners_meaning_exactly(self, case):
        raw, labels, item = case
        merged = merge_label_excludes(raw, labels, label_prefix=PREFIX)
        ours = bool({v.casefold() for v in item["label"]} & set(labels))
        for model in MODELS:
            assert model(merged, item) is (False if ours else model(raw, item)), (raw, merged, model.__name__)

    @given(_case(damaged=True))
    def test_a_damaged_filter_is_repaired_without_ever_showing_anyone_more(self, case):
        raw, labels, item = case
        merged = merge_label_excludes(raw, labels, label_prefix=PREFIX)
        for model in MODELS:
            if {v.casefold() for v in item["label"]} & set(labels):
                assert model(merged, item) is False, (raw, merged, model.__name__)
            elif model(merged, item):
                assert model(raw, item), f"repair widened {raw!r} -> {merged!r} under {model.__name__}"

    @given(_case(damaged=True))
    def test_the_merge_settles_after_one_pass(self, case):
        raw, labels, _item = case
        once = merge_label_excludes(raw, labels, label_prefix=PREFIX)
        assert merge_label_excludes(once, labels, label_prefix=PREFIX) == once
