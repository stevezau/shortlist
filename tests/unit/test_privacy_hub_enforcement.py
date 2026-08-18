"""`unhidden_rows_on_home` against a RECORDED real PMS `/hubs` response.

The canary that answers "Plex stored our filter — is it actually applying it?". Every case here
replays `tests/fixtures/pms_hubs_shared_account.json`, captured from a real server on 2026-08-18
with a shared account's own token, rather than a hand-built hub list: the detector's whole job is to
read a shape this repo does not control, and a mock would only prove it reads the shape I imagined.

The clean case is the weak half — a detector that always returned `[]` would pass it. The one that
matters is `test_it_fires_when_another_persons_row_is_on_this_home`, which replays the SAME real
response and re-attributes it, and is the offline twin of the live check run against the maintainer's
server on 2026-08-18 (MooHouse: clean under their own slug, four ratingKeys under another's).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shortlist.engine.privacy import unhidden_rows_on_home

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pms_hubs_shared_account.json"

#: ratingKeys of the rows in the recording, by whose they are.
MOOHOUSE_ROWS = [624682, 624683, 624690, 624691]
SHARED_ROW = [577628, 577629]
KOMETA_ROWS = [506410, 527794, 343591]


@dataclass
class _Row:
    rating_keys: list[int] = field(default_factory=list)


@pytest.fixture
def hubs() -> list[dict]:
    """The recorded response, unpacked exactly as `PlexClient.user_hubs` unpacks it."""
    return json.loads(FIXTURE.read_text())["MediaContainer"]["Hub"]


@pytest.fixture
def owned() -> dict[str, _Row]:
    """What `owned_collections()` returns for that server, trimmed to the rows in the recording."""
    return {
        "moohouse": _Row(MOOHOUSE_ROWS),
        "someone-else": _Row([700001, 700002]),
        "_shared_popular": _Row(SHARED_ROW),
    }


class TestAgainstTheRealResponse:
    def test_an_account_seeing_only_its_own_rows_reads_clean(self, hubs, owned):
        assert unhidden_rows_on_home(hubs, owned, "moohouse") == []

    def test_it_fires_when_another_persons_row_is_on_this_home(self, hubs, owned):
        """The teeth. Same real hubs, read as somebody else's account: the four rows that were
        legitimately theirs are now four rows of another person's on this Home, and every one is
        reported. A detector that could never fire passes the clean case above and fails here."""
        assert unhidden_rows_on_home(hubs, owned, "someone-else") == MOOHOUSE_ROWS

    def test_the_shared_row_is_never_reported(self, hubs, owned):
        """It is on this Home, it is ours, and it is not theirs — every condition for a hit except
        the one that counts. A shared row is MEANT to be seen; reporting it would make the alert
        fire on every server that has one."""
        assert not set(unhidden_rows_on_home(hubs, owned, "someone-else")) & set(SHARED_ROW)

    def test_another_tools_collections_are_never_reported(self, hubs, owned):
        """Kometa's rows share this Home. Ours are identified by ratingKey against `owned`, so a
        row we did not create cannot be reported no matter who is looking — plex-safety rule 4."""
        assert not set(unhidden_rows_on_home(hubs, owned, "someone-else")) & set(KOMETA_ROWS)

    def test_rows_that_exist_but_are_not_on_this_home_are_not_reported(self, hubs, owned):
        """`someone-else`'s own rows (700001/700002) are in `owned` and absent from this recording —
        which is the filter WORKING. Reporting on existence rather than visibility would invert it."""
        assert not set(unhidden_rows_on_home(hubs, owned, "moohouse")) & {700001, 700002}


class TestTheRefusals:
    def test_no_rows_of_ours_means_no_finding(self, hubs):
        """First run, or a failed collections read. Every hub in the recording belongs to another
        tool as far as we know, and 'sees none of ours' must not become 'sees all of theirs'."""
        assert unhidden_rows_on_home(hubs, {}, "moohouse") == []

    def test_an_empty_home_is_clean_not_an_error(self, owned):
        assert unhidden_rows_on_home([], owned, "someone-else") == []


class TestTheShapeItReadsIsTheRecordedOne:
    """Rule 11. These pin the assumptions the detector makes about a response nobody here controls —
    if a future PMS moves the row identity, these fail rather than the detector going quiet."""

    def test_a_row_is_its_own_hub_not_a_metadata_item(self, hubs):
        """The mistake this fixture was recorded to prevent: looking for Metadata entries of
        type 'collection'. Every hub in the recording HAS a Metadata array — so 'we found none' is
        evidence here, not an artefact of a fixture that carries no Metadata at all. What the items
        actually are is the library content the row points at: movies, shows, episodes."""
        items = [m for h in hubs for m in (h.get("Metadata") or [])]
        assert items, "a fixture with no Metadata anywhere would pass the next line for the wrong reason"
        assert {m["type"] for m in items} == {"movie", "show", "episode"}
        assert not [m for m in items if m.get("type") == "collection"]

    def test_home_carries_hubs_that_are_not_collections_and_they_are_never_rows(self, hubs, owned):
        """Continue Watching and Recently Added share this response. Anything that treated every
        hub as a row would report them, and they belong to no user at all."""
        not_collections = [h for h in hubs if "/library/collections/" not in h["key"]]
        assert len(not_collections) == 2, "the recording keeps two, so this asserts against real ones"
        assert unhidden_rows_on_home(not_collections, owned, "someone-else") == []

    def test_the_visible_title_names_nobody_so_identity_must_come_from_the_key(self, hubs):
        """Every per-person row is named from a template that carries no owner — the account this
        recording came from is nowhere in the visible text of its own rows. Two people's rows are
        therefore indistinguishable by title, and the only thing that tells them apart is the
        ratingKey in `key`. Getting this wrong reports a person's OWN row as somebody else's."""
        ours = [
            h for h in hubs if (m := re.search(r"/library/collections/(\d+)", h["key"])) and int(m[1]) in MOOHOUSE_ROWS
        ]
        assert len(ours) == len(MOOHOUSE_ROWS)
        visible = ["".join(c for c in h["title"] if c not in "​‌") for h in ours]
        assert not [t for t in visible if "moohouse" in t.lower()]
        # The marker that DOES distinguish them is zero-width, so it is invisible to a title read.
        assert all(set(h["title"]) & {"​", "‌"} for h in ours)


@dataclass
class _Acct:
    """One account as the spot-check will encounter it."""

    name: str
    kind: str = "shared"
    labelled: bool = True  # does its share filter already carry our excludes?
    outcome: str = "clean"  # clean | raise | leak | no_token


class TestTheSpotCheckGivesUp:
    """The 3-attempt cap, and what a capped run is then entitled to CLAIM.

    Two review findings meet here. The cap itself (2026-08-18, MEDIUM): the check needs ONE working
    account per type, so a failed read tries the next candidate, and nothing bounded that retry — a
    46-user server with broken hub reads paid 46 sequential reads nightly to learn nothing. And the
    regression the cap introduced (same review): `attempts` is per type while
    `filters_enforcement_measured` is one bool for the run.

    Driven directly rather than through `engine_run`: the fake's roster only ever yields three
    candidates, so a run-level test passes whether the cap exists or not. It was written that way
    first and proved nothing — the cap could be deleted and it stayed green.
    """

    def _drive(self, accts: list[_Acct]):
        from types import SimpleNamespace

        from shortlist.engine.models import RunReport, UserProfile, UserType
        from shortlist.engine.pipeline import _verify_filters_enforced

        reads: list[str] = []
        people, roster, by_token = [], {}, {}
        for i, a in enumerate(accts):
            profile = UserProfile(
                username=a.name, plex_account_id=300 + i, user_type=UserType(a.kind) if a.kind else UserType.SHARED
            )
            people.append(profile)
            ours = f"label!=shortlist_{profile.slug}"
            roster[profile.plex_account_id] = SimpleNamespace(
                filters={"filterMovies": ours if a.labelled else "label!=Kids"}, restriction_profile=""
            )
            by_token[f"server-{profile.plex_account_id}"] = a

        def token_for_user(profile):
            token = f"server-{profile.plex_account_id}"
            return None if by_token[token].outcome == "no_token" else token

        def user_hubs(token, path="/hubs"):
            acct = by_token[token]
            reads.append(acct.name)
            if acct.outcome == "raise":
                raise ConnectionError("PMS unreachable")
            return json.loads(FIXTURE.read_text())["MediaContainer"]["Hub"] if acct.outcome == "leak" else []

        ctx = SimpleNamespace(
            config=SimpleNamespace(dry_run=False),
            plex=SimpleNamespace(user_hubs=user_hubs),
            token_for_user=token_for_user,
            unmanaged_account_ids=set(),
        )
        report = RunReport(started_at=None, dry_run=False)
        # `owned` belongs to nobody in this audience, so any hub we return reads as a leak.
        _verify_filters_enforced(ctx, people, roster, {"absent-person": _Row(MOOHOUSE_ROWS)}, True, report)
        return reads, report

    def test_it_stops_after_three_failures_instead_of_walking_every_account(self):
        reads, _ = self._drive([_Acct(f"user{i}", outcome="raise") for i in range(40)])
        assert reads == ["user0", "user1", "user2"], f"walked {len(reads)} accounts; the cap is 3 per type"

    def test_a_healthy_server_costs_exactly_one_read_per_type(self):
        """The cap bounds FAILURE only. Nothing here should get slower on a server that works."""
        reads, report = self._drive([_Acct(f"user{i}") for i in range(40)])
        assert reads == ["user0"]
        assert report.filters_enforcement_measured is True

    def test_each_type_gets_its_own_budget(self):
        reads, _ = self._drive(
            [*(_Acct(f"s{i}", outcome="raise") for i in range(1, 4)), _Acct("m1", kind="managed", outcome="raise")]
        )
        assert reads == ["s1", "s2", "s3", "m1"], "one type's exhausted budget must not silence another"

    def test_an_unreadable_type_is_never_reported_as_clean_by_another_types_success(self):
        """The regression the cap introduced, in the shape the review reproduced it.

        Three shared accounts fail, the fourth is genuinely LEAKING and is never reached, and a
        managed account reads clean. The old flag went True on the managed success, the run persisted
        `filters_not_enforced: {}`, and the notification — which reads the newest run carrying that
        key — CLEARED the "Plex is ignoring the privacy filter" card while the leak was live. Worse
        than a missed detection: an active all-clear over a real exposure.
        """
        reads, report = self._drive(
            [
                *(_Acct(f"s{i}", outcome="raise") for i in range(3)),
                _Acct("s-leaking", outcome="leak"),
                _Acct("m1", kind="managed"),
            ]
        )

        assert "s-leaking" not in reads, "precondition: the cap means the leaking account is not reached"
        assert report.filters_not_enforced == {}, "precondition: so the run genuinely found nothing"
        assert report.filters_enforcement_measured is False, (
            "the shared type produced no reading at all, so this run may not publish an all-clear"
        )

    def test_a_finding_publishes_even_when_another_type_could_not_be_read(self):
        """The opposite error, which the fix must not introduce: gating publication on FULL coverage
        would discard a leak we actually saw because some other account type was unreachable."""
        _, report = self._drive(
            [_Acct("s-leaking", outcome="leak"), *(_Acct(f"m{i}", kind="managed", outcome="raise") for i in range(5))]
        )

        assert report.filters_not_enforced == {"s-leaking": MOOHOUSE_ROWS}
        assert report.filters_enforcement_measured is True, "a finding must always reach the run's stats"

    def test_an_account_without_our_excludes_does_not_consume_the_budget(self):
        """The accounting sits AFTER the eligibility guards on purpose. Hoisted above them, a server
        whose first three shared accounts are new invitees — no excludes written yet — would spend
        the whole budget on accounts that get skipped anyway and never check the type at all."""
        reads, report = self._drive([*(_Acct(f"new{i}", labelled=False) for i in range(3)), _Acct("established")])

        assert reads == ["established"]
        assert report.filters_enforcement_measured is True
