"""Dry-run preview, preview titles and AI instructions on a row."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shortlist.server.db.models import DEFAULT_SLUG, User
from shortlist.server.services.season_catalogue import load_catalogue
from shortlist.server.settings_store import SettingsStore
from tests.integration.api_collections_support import _plex_jobs

pytestmark = pytest.mark.integration


class TestDryRunPreview:
    """`PATCH`/`DELETE /collections/{id}` previewing what they would owe Plex, writing nothing.

    Two of the four doors onto a row's Plex collections already preview (`/cleanup`, `/rename`) and
    two did not. The urgent one is PATCH: narrowing a row's media or libraries DELETES its
    collections in the libraries it walked away from (`_stranded_sections`), and that had no preview
    anywhere. DELETE's Plex half was already visible through `/cleanup?dry_run=true`; what only its
    own preview can show is the LOCAL half — `_forget_anchor_row` silently strips every other row's
    shelf placement that pointed at this one.
    """

    def _plex(self, monkeypatch, client, *, collections, sections_raise: bool = False):
        """A fake PMS over two libraries that RECORDS deletions instead of pretending they happened.

        `deleted` is the assertion that matters throughout this class: a preview that writes is the
        one failure mode worth the whole feature, and only a recording fake can catch it.
        """
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        deleted: list[tuple[str, str]] = []
        movies = SimpleNamespace(title="Movies", key="1", type="movie")
        shows = SimpleNamespace(title="TV", key="2", type="show")
        plex = MagicMock()
        if sections_raise:
            plex.sections.side_effect = OSError("PMS unreachable")
        else:
            plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.side_effect = lambda s, label: [
            SimpleNamespace(title=title, _section=str(s.key))
            for (title, lbl, key) in collections
            if lbl == label and key == str(s.key)
        ]
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append((c.title, c._section))
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)
        return deleted

    def _row(self, client: TestClient, **body):
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Gems", "media": "both", **body})
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            return created.json()["id"], user.slug, row_marker(user.plex_account_id)

    def _jobs(self, client: TestClient) -> list[dict]:
        return _plex_jobs(client)

    def _row_state(self, client: TestClient, cid: int) -> dict | None:
        """One row, read off the LIST endpoint, or None if it is gone.

        There is no `GET /collections/{id}` — asking for one answers 405, and two 405 bodies compare
        EQUAL, so an "is the row unchanged?" assertion written against it passes whatever the preview
        did. That is a test that cannot fail, which is worse than no test.
        """
        return next((c for c in client.get("/api/collections").json() if c["id"] == cid), None)

    def test_a_dry_run_patch_writes_nothing(self, client: TestClient, monkeypatch):
        """The whole contract in one test: a preview changes neither the row nor the queue."""
        cid, uslug, marker = self._row(client)
        deleted = self._plex(
            monkeypatch,
            client,
            collections=[("Gems" + marker, f"shortlist_{uslug}", "1"), ("Gems" + marker, f"shortlist_{uslug}", "2")],
        )
        before = self._row_state(client, cid)

        queue_before = client.get("/api/system/jobs").json()

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "media": "movie", "dry_run": True})

        assert r.status_code == 200
        assert r.json()["dry_run"] is True
        assert self._row_state(client, cid) == before, "a preview must not edit the row"
        assert client.get("/api/system/jobs").json() == queue_before, "a preview must not enqueue any work"
        assert deleted == [], "a preview must not delete anything on Plex"

    def test_a_dry_run_rename_of_the_default_row_does_not_write_the_global_template(
        self, client: TestClient, monkeypatch
    ):
        """The landmine the obvious implementation walks into.

        `SettingsStore.set` commits INSIDE itself (settings_store.py), so "apply the edit then
        `session.rollback()`" would leave this permanently applied — a preview of one row's rename
        that silently retitles every row on the server for every user.
        """
        self._plex(monkeypatch, client, collections=[])
        default = next(c for c in client.get("/api/collections").json() if c["slug"] == DEFAULT_SLUG)
        before = client.get("/api/settings").json()["row.name_template"]

        r = client.patch(f"/api/collections/{default['id']}", json={"name": "🍿 Movie night", "dry_run": True})

        assert r.status_code == 200
        assert client.get("/api/settings").json()["row.name_template"] == before

    def test_a_dry_run_patch_names_the_libraries_a_narrowing_would_strip(self, client: TestClient, monkeypatch):
        """Narrowing `both` to `movie` DELETES this row's collections in the libraries it leaves.

        The most destructive edit on the row editor, and the one with no preview at all before this.
        """
        cid, uslug, marker = self._row(client)
        deleted = self._plex(
            monkeypatch,
            client,
            collections=[("Gems" + marker, f"shortlist_{uslug}", "1"), ("Gems" + marker, f"shortlist_{uslug}", "2")],
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "media": "movie", "dry_run": True})

        plan = r.json()["plan"]
        assert [entry["kind"] for entry in plan] == ["reconcile"]
        # The TITLES are the whole point — a plan that merely says "a reconcile would happen" tells
        # the operator nothing about what they are about to lose.
        assert plan[0]["collections"] == ["Gems"]
        assert plan[0]["reason"] == "collection.libraries"
        assert r.json()["preview_incomplete"] is None
        assert deleted == []

    def _subset_row_of_two(self, client: TestClient, monkeypatch):
        """A per-person row shared with both fixture users, over a fake PMS holding both copies."""
        from shortlist.engine.delivery import row_marker

        with client.app.state.sessions() as session:
            keep, drop = session.query(User).order_by(User.id).all()[:2]
            keep_id, drop_id, keep_slug, drop_slug = keep.id, drop.id, keep.slug, drop.slug
            keep_marker, drop_marker = row_marker(keep.plex_account_id), row_marker(drop.plex_account_id)
        created = client.post(
            "/api/collections",
            json={"name": "Gems", "audience": "subset", "audience_user_ids": [keep_id, drop_id]},
        ).json()
        deleted = self._plex(
            monkeypatch,
            client,
            collections=[
                ("Gems" + keep_marker, f"shortlist_{keep_slug}", "1"),
                ("Gems" + drop_marker, f"shortlist_{drop_slug}", "1"),
            ],
        )
        return created["id"], keep_id, drop_id, deleted

    def test_a_dry_run_names_only_the_dropped_users_copy_when_the_audience_shrinks(
        self, client: TestClient, monkeypatch
    ):
        """Shrinking a per-person row's audience deletes exactly the dropped people's copies.

        The preview has to be right about WHOSE copy goes, not merely that a reconcile is owed —
        `only_user_ids` is what scopes it, and the projection derives that on its own. Sent the way
        the row editor sends it, with `audience` and the ids together.
        """
        cid, keep_id, drop_id, deleted = self._subset_row_of_two(client, monkeypatch)

        r = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Gems", "audience": "subset", "audience_user_ids": [keep_id], "dry_run": True},
        )

        plan = r.json()["plan"]
        assert [(e["kind"], e["reason"]) for e in plan] == [("reconcile", "collection.audience")]
        # Both copies render the same title, so `collections` cannot tell them apart — `only_user_ids`
        # is the whole discriminator, and it is asserted POSITIVELY: `!= [keep_id]` would also pass on
        # `[]`, which means "everyone", i.e. the kept person's copy going too.
        assert plan[0]["collections"] == ["Gems"]
        assert plan[0]["only_user_ids"] == [drop_id]
        assert deleted == []

    def test_a_dry_run_reports_the_wipe_that_ids_without_an_audience_actually_perform(
        self, client: TestClient, monkeypatch
    ):
        """`audience_user_ids` sent WITHOUT `audience` clears the row's whole membership.

        `_set_audience` gates on the RAW `body.audience`, which `CollectionIn` defaults to "everyone"
        — so it deletes every membership row and adds none back, while the `audience` COLUMN stays
        "subset". Everyone is dropped, not just the ids left out.

        Whether that is the right live behaviour is a separate question; what this pins is that the
        preview tells the truth about it. Reasoning from the row's merged audience instead made the
        preview name one person's collection while the save removed every one of them — on a 40-user
        server, "1 collection would go" followed by 40 deletions.
        """
        cid, keep_id, _drop_id, deleted = self._subset_row_of_two(client, monkeypatch)

        preview = client.patch(
            f"/api/collections/{cid}", json={"name": "Gems", "audience_user_ids": [keep_id], "dry_run": True}
        )
        previewed = preview.json()["plan"][0]["collections"]

        client.patch(f"/api/collections/{cid}", json={"name": "Gems", "audience_user_ids": [keep_id]})

        # The preview reports DISPLAY titles; the fake records what Plex was asked to delete, which
        # still carries the invisible ownership marker.
        from shortlist.engine.delivery import strip_marker

        assert sorted(previewed) == sorted(strip_marker(title) for title, _section in deleted), (
            "the preview must name exactly the collections the save removes"
        )
        assert len(deleted) == 2, "both copies go — the membership was wiped, not narrowed"

    def test_a_preview_says_it_could_not_find_out_when_plex_is_unreadable(self, client: TestClient, monkeypatch):
        """The third state. `_stranded_sections` answers an unreachable Plex with an EMPTY set —
        the right answer for a live edit ("not knowing which libraries exist must mean delete
        nothing") and a lie in a preview, where it is indistinguishable from "this edit is safe".

        A preview that under-reports a deletion is worse than no preview, so the two cases must not
        share one answer.
        """
        cid, _, _ = self._row(client)
        self._plex(monkeypatch, client, collections=[], sections_raise=True)

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "media": "movie", "dry_run": True})

        assert r.status_code == 200
        assert r.json()["plan"] == [], "an unreadable Plex plans nothing — which is exactly the trap"
        assert r.json()["preview_incomplete"], "the preview must SAY it could not find out"
        assert "librar" in r.json()["preview_incomplete"].lower()

    def test_a_dry_run_patch_is_refused_by_whatever_would_refuse_the_real_edit(self, client: TestClient):
        """A preview that succeeds where the save 422s is a preview of an edit that cannot happen."""
        client.post("/api/collections", json={"name": "Taken"})
        cid, _, _ = self._row(client)

        preview = client.patch(f"/api/collections/{cid}", json={"name": "Taken", "dry_run": True})
        real = client.patch(f"/api/collections/{cid}", json={"name": "Taken"})

        assert preview.status_code == 422
        assert (preview.status_code, preview.json()["detail"]) == (real.status_code, real.json()["detail"])

    def test_a_refused_edit_leaves_the_global_template_alone(self, client: TestClient):
        """A 422 must not have written half the edit — and this one could.

        `SettingsStore.set` commits inside itself, and the default row's rename used to run BEFORE
        `_validate_pairing`. So a save that renamed the default row and was then refused for an
        unrelated contradiction had already retitled every row on the server, permanently, while
        answering "422, nothing happened". Splitting the handler into validate-then-write for the
        preview fixed that for the live path too, and this pins it.
        """
        default = next(c for c in client.get("/api/collections").json() if c["slug"] == DEFAULT_SLUG)
        # Narrow it to movies FIRST, so the contradiction below is only visible on the MERGED row:
        # `_validate` judges the request body, where `media` is absent and defaults to "both", and
        # only the handler's second `_validate_pairing` sees the stored "movie". That is the one
        # 422 that used to arrive after the rename had already been committed.
        client.patch(f"/api/collections/{default['id']}", json={"name": default["name"], "media": "movie"})
        before = client.get("/api/settings").json()["row.name_template"]

        r = client.patch(
            f"/api/collections/{default['id']}",
            json={"name": "🍿 Movie night", "unstarted_only": True},
        )

        assert r.status_code == 422
        assert client.get("/api/settings").json()["row.name_template"] == before

    def test_an_unknown_audience_id_is_refused_before_the_rename_is_written(self, client: TestClient):
        """The second 422 that used to land after `SettingsStore.set` had already committed.

        `_set_audience` raises this from inside the apply half, so a default-row rename carrying a
        bad user id answered "422, no such user" with every row on the server already retitled.
        """
        default = next(c for c in client.get("/api/collections").json() if c["slug"] == DEFAULT_SLUG)
        before = client.get("/api/settings").json()["row.name_template"]

        r = client.patch(
            f"/api/collections/{default['id']}",
            json={"name": "🍿 Movie night", "audience": "subset", "audience_user_ids": [999999]},
        )

        assert r.status_code == 422
        assert "no such user" in r.json()["detail"]
        assert client.get("/api/settings").json()["row.name_template"] == before

    @pytest.mark.parametrize(
        "patch",
        [
            {"media": "movie"},
            {"library_keys": ["1"]},
            {"build": "shared"},
            {"enabled": False},
            {"audience": "subset", "audience_user_ids": [1]},
            {"audience": "everyone"},
            # Ids WITHOUT `audience`: the cell that exercises the projection's second branch, where
            # the kind is read off the row and only the membership comes from the request.
            {"audience_user_ids": [1]},
            {"show_days": [1, 3, 5]},
            {"poster": {"mode": ""}},
            {"name": "Renamed Gems"},
            {"size": 25},  # owes Plex nothing — the empty-plan cell
        ],
    )
    def test_a_dry_run_projects_exactly_what_the_real_patch_produces(
        self, client: TestClient, monkeypatch, patch: dict
    ):
        """`_projected_snapshot` computes the post-edit state WITHOUT applying it, so it can drift
        from the apply path field by field with nothing to notice — and a drifted preview is simply
        wrong about a delete. Both are run over the matrix `plan_row_changes` branches on and
        asserted to agree (`.claude/rules/testing.md`: cover the matrix, not one cell).

        The actual save now persists every external effect in one ordered convergence job.
        Compare that durable contract to the preview, including people and library targets.
        """
        cid, uslug, marker = self._row(client, poster={"mode": "text", "title": "Gems"})
        self._plex(
            monkeypatch,
            client,
            collections=[("Gems" + marker, f"shortlist_{uslug}", "1"), ("Gems" + marker, f"shortlist_{uslug}", "2")],
        )
        before = client.get("/api/system/jobs").json()
        preview = client.patch(f"/api/collections/{cid}", json={"name": "Gems", **patch, "dry_run": True})
        assert preview.status_code == 200
        previewed = [
            (e["kind"], e["reason"], tuple(e["only_user_ids"]), tuple(e["in_sections"])) for e in preview.json()["plan"]
        ]
        assert client.get("/api/system/jobs").json() == before, "a preview must not write any job"

        applied = client.patch(f"/api/collections/{cid}", json={"name": "Gems", **patch})
        assert applied.status_code == 200

        before_ids = {job["id"] for job in before}
        kind_map = {
            "row.reconcile": "reconcile",
            "privacy.sync": "privacy_sync",
            "row.rename": "rename",
            "poster.reset": "poster_reset",
            "rows.visibility": "visibility",
        }
        executed = []
        for job in _plex_jobs(client):
            if job["id"] in before_ids:
                continue
            payload = job["payload"]
            kind = kind_map[job["kind"]]
            assert payload.get("slug", payload.get("row", "gems")) == "gems"
            reason = payload.get("scope", payload.get("reason"))
            # Visibility's persisted contract carries its exact row; the explanation is preview-only.
            executed.append(
                (kind, reason, tuple(payload.get("only_user_ids") or ()), tuple(payload.get("in_sections") or ()))
            )
        previewed = [
            (kind, None if kind == "visibility" else reason, people, sections)
            for kind, reason, people, sections in previewed
        ]
        assert previewed == executed, f"the preview drifted from the edit for {patch}"

    def test_a_dry_run_delete_keeps_the_row_and_names_what_it_would_take(self, client: TestClient, monkeypatch):
        cid, uslug, marker = self._row(client)
        deleted = self._plex(monkeypatch, client, collections=[("Gems" + marker, f"shortlist_{uslug}", "1")])

        queue_before = client.get("/api/system/jobs").json()

        r = client.delete(f"/api/collections/{cid}?dry_run=true")

        assert r.status_code == 200
        assert r.json()["dry_run"] is True
        assert r.json()["collections"] == ["Gems"]
        assert self._row_state(client, cid) is not None, "a preview must not delete the row"
        assert client.get("/api/system/jobs").json() == queue_before and deleted == []

    @pytest.mark.parametrize(
        ("build", "schedule", "expect_privacy_sync", "expect_schedule_cleared"),
        [
            # A SHARED row's label stops being declared shared, so every account's excludes have to
            # be recomputed; a per-person row's deletion touches nobody else's filter.
            ("shared", "30 3 * * *", True, True),
            ("per_person", "30 3 * * *", False, True),
            ("per_person", "", False, False),
        ],
    )
    def test_a_dry_run_delete_reports_the_privacy_and_schedule_consequences(
        self, client: TestClient, monkeypatch, build, schedule, expect_privacy_sync, expect_schedule_cleared
    ):
        """`privacy_sync` states a share-filter fact to the operator, so it has to be true.

        All three flags are hand-built into the response, and none of them had a test row: an
        inverted boolean here tells someone their friends' Plex filters will be rewritten when they
        will not, or the reverse.
        """
        self._plex(monkeypatch, client, collections=[])
        row = client.post("/api/collections", json={"name": "Gems", "build": build, "schedule": schedule}).json()

        r = client.delete(f"/api/collections/{row['id']}?dry_run=true")

        assert r.status_code == 200
        assert r.json()["privacy_sync"] is expect_privacy_sync
        assert r.json()["schedule_cleared"] is expect_schedule_cleared
        assert r.json()["preview_incomplete"] is None
        assert self._row_state(client, row["id"]) is not None

    def test_a_dry_run_delete_says_when_it_could_not_read_plex(self, client: TestClient, monkeypatch):
        """The walk-failed branch of the DELETE preview — the same third state the PATCH has."""
        self._plex(monkeypatch, client, collections=[], sections_raise=True)
        row = client.post("/api/collections", json={"name": "Gems"}).json()

        r = client.delete(f"/api/collections/{row['id']}?dry_run=true")

        assert r.status_code == 200
        assert r.json()["preview_incomplete"], "an unreadable Plex must not report an empty removal as fact"
        assert self._row_state(client, row["id"]) is not None

    def test_a_dry_run_delete_of_the_default_row_names_its_real_title(self, client: TestClient, monkeypatch):
        """The default row's `name` column is stale seed data — its title IS the global template."""
        self._plex(monkeypatch, client, collections=[])
        default = next(c for c in client.get("/api/collections").json() if c["slug"] == DEFAULT_SLUG)
        template = client.get("/api/settings").json()["row.name_template"]

        r = client.delete(f"/api/collections/{default['id']}?dry_run=true")

        assert template in r.json()["message"]

    def test_a_dry_run_delete_warns_that_other_rows_lose_their_placement(self, client: TestClient, monkeypatch):
        """`_forget_anchor_row` silently reparents every row positioned relative to this one. It is
        logged AFTER the fact and nothing warned first — this is the half `/cleanup?dry_run=true`
        cannot show, and the reason DELETE needs a preview of its own rather than a pointer at
        cleanup.
        """
        self._plex(monkeypatch, client, collections=[])
        target = client.post("/api/collections", json={"name": "Anchor row"}).json()
        follower = client.post(
            "/api/collections",
            json={"name": "Follower", "hub_anchor": {"1": {"row": target["slug"], "before": False}}},
        ).json()

        r = client.delete(f"/api/collections/{target['id']}?dry_run=true")

        assert r.json()["anchors_cleared"] == [follower["slug"]]
        assert self._row_state(client, follower["id"])["hub_anchor"] != {}, "the preview must not clear it"
        # And the warning must be TRUE: the real delete does exactly what the preview promised.
        client.delete(f"/api/collections/{target['id']}")
        assert self._row_state(client, follower["id"])["hub_anchor"] == {}


class TestPreviewTitles:
    """`preview_titles`: the Rows list's 4-poster collage, from each row's most recent delivery."""

    @staticmethod
    def _people(client: TestClient, *slugs: str) -> list[int]:
        """The ids of these people, adding any the fixture's roster does not already have."""
        with client.app.state.sessions() as session:
            for i, slug in enumerate(slugs):
                if session.query(User).filter_by(slug=slug).one_or_none() is None:
                    session.add(User(username=slug, slug=slug, plex_account_id=5000 + i, enabled=True))
            session.commit()
            return [session.query(User).filter_by(slug=slug).one().id for slug in slugs]

    @staticmethod
    def _run(client: TestClient, picks: list[tuple[int, str, int, str, int]], *, dry_run: bool = False) -> int:
        """One run delivering `(user_id, slug, rating_key, title, rank)` picks."""
        from shortlist.server.db.models import PickRow, Run
        from tests.watch_fixtures import live_row, personal_delivery

        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok", dry_run=dry_run)
            session.add(run)
            session.flush()
            for user_id, slug, rating_key, title, rank in picks:
                session.add(
                    PickRow(
                        run_id=run.id,
                        user_id=user_id,
                        tmdb_id=rating_key,
                        media_type="movie",
                        rating_key=rating_key,
                        rank=rank,
                        collection_slug=slug,
                        section_key="1",
                        title=title,
                    )
                )
            if not dry_run:
                for user_id, slug in {(pick[0], pick[1]) for pick in picks}:
                    live_row(session, user_id, slug, "1")
                    personal_delivery(session, run.id, user_id=user_id, slug=slug)
            session.commit()
            return run.id

    @staticmethod
    def _row(client: TestClient, slug: str) -> dict:
        return next(c for c in client.get("/api/collections").json() if c["slug"] == slug)

    def test_a_row_never_built_has_none(self, client: TestClient):
        assert self._row(client, "picked")["preview_titles"] == []

    def test_four_distinct_titles_from_the_latest_run_best_ranked_first(self, client: TestClient):
        sarah, mike = self._people(client, "sarah", "mike")
        # An older run's titles are not what the row holds now.
        self._run(client, [(sarah, "picked", 900, "Old", 1)])
        # Both people got Heat at the top: one poster, not two.
        self._run(
            client,
            [
                (sarah, "picked", 11, "Heat", 1),
                (sarah, "picked", 12, "Ronin", 2),
                (sarah, "picked", 13, "Collateral", 3),
                (sarah, "picked", 14, "Thief", 4),
                (sarah, "picked", 15, "Manhunter", 5),
                (mike, "picked", 11, "Heat", 1),
                (mike, "picked", 16, "Drive", 2),
            ],
        )

        titles = self._row(client, "picked")["preview_titles"]

        assert titles == [
            {"rating_key": 11, "title": "Heat"},
            {"rating_key": 12, "title": "Ronin"},
            {"rating_key": 16, "title": "Drive"},
            {"rating_key": 13, "title": "Collateral"},
        ]

    def test_fewer_than_four_are_returned_as_they_are(self, client: TestClient):
        (sarah,) = self._people(client, "sarah")
        self._run(client, [(sarah, "picked", 21, "Alien", 1), (sarah, "picked", 22, "Aliens", 2)])

        assert self._row(client, "picked")["preview_titles"] == [
            {"rating_key": 21, "title": "Alien"},
            {"rating_key": 22, "title": "Aliens"},
        ]

    def test_a_pick_never_matched_to_a_library_item_has_no_artwork_to_show(self, client: TestClient):
        (sarah,) = self._people(client, "sarah")
        self._run(client, [(sarah, "picked", 0, "Unmatched", 1), (sarah, "picked", 31, "Matched", 2)])

        assert self._row(client, "picked")["preview_titles"] == [{"rating_key": 31, "title": "Matched"}]

    def test_each_row_gets_its_own_titles_from_one_list_call(self, client: TestClient):
        (sarah,) = self._people(client, "sarah")
        other = client.post("/api/collections", json={"name": "Another"}).json()["slug"]
        self._run(client, [(sarah, "picked", 41, "Mine", 1), (sarah, other, 42, "Theirs", 1)])

        assert self._row(client, "picked")["preview_titles"] == [{"rating_key": 41, "title": "Mine"}]
        assert self._row(client, other)["preview_titles"] == [{"rating_key": 42, "title": "Theirs"}]

    def test_a_shared_row_reads_its_latest_real_delivery(self, client: TestClient):
        """A shared row's picks live only in `run_shared_rows` — and that table is written by dry runs
        too, so a preview must not show a row as holding titles a dry run only imagined."""
        from shortlist.server.db.models import Delivery, RunSharedRow
        from tests.watch_fixtures import shared_delivery

        slug = client.post("/api/collections", json={"name": "Popular", "build": "shared"}).json()["slug"]
        real = self._run(client, [])
        dry = self._run(client, [], dry_run=True)
        skipped = self._run(client, [])
        with client.app.state.sessions() as session:
            session.add(Delivery(collection_slug=slug, user_slug=f"shared_{slug}", library_key="1", rating_key=7))
            session.add(
                RunSharedRow(
                    run_id=real,
                    collection_slug=slug,
                    status="ok",
                    picks=[
                        {"rating_key": 51, "title": "Up", "rank": 1},
                        {"rating_key": 52, "title": "Coco", "rank": 2},
                    ],
                )
            )
            session.add(
                RunSharedRow(
                    run_id=dry, collection_slug=slug, status="ok", picks=[{"rating_key": 99, "title": "Dry", "rank": 1}]
                )
            )
            # A later run that delivered nothing for it leaves Plex holding the earlier titles.
            session.add(RunSharedRow(run_id=skipped, collection_slug=slug, status="skipped", picks=[]))
            shared_delivery(session, real, slug=slug)
            session.commit()

        assert self._row(client, slug)["preview_titles"] == [
            {"rating_key": 51, "title": "Up"},
            {"rating_key": 52, "title": "Coco"},
        ]

    def test_a_single_row_response_carries_them_too(self, client: TestClient):
        (sarah,) = self._people(client, "sarah")
        created = client.post("/api/collections", json={"name": "Another"}).json()
        self._run(client, [(sarah, created["slug"], 61, "Solo", 1)])

        patched = client.patch(f"/api/collections/{created['id']}", json={"name": "Another"})

        assert patched.status_code == 200, patched.text
        assert patched.json()["preview_titles"] == [{"rating_key": 61, "title": "Solo"}]


class TestAiInstructions:
    """A row's instructions for AI web search (#138), kept in the formerly dead `Collection.prompt` column."""

    @staticmethod
    def _spec(client: TestClient, slug: str):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            specs = builder._build_rows(session, store, catalogue=load_catalogue(session))
            config = builder._engine_config(session, store)
        return next(s for s in specs if s.slug == slug), config

    def test_ai_instructions_round_trip_and_reach_the_spec(self, client: TestClient):
        from shortlist.engine.web_guidance import AiInstructions

        body = {"name": "Guided Row", "ai_instructions": {"mode": "add", "text": " No kids films. "}}
        created = client.post("/api/collections", json=body)
        assert created.status_code == 201
        assert created.json()["ai_instructions"] == {"mode": "add", "text": "No kids films."}
        rid = created.json()["id"]
        patched = client.patch(
            f"/api/collections/{rid}",
            json={"name": "Guided Row", "ai_instructions": {"mode": "own", "text": "Any decade."}},
        )
        assert patched.json()["ai_instructions"] == {"mode": "own", "text": "Any decade."}
        spec, _ = self._spec(client, "guided_row")
        assert spec.ai_instructions == AiInstructions("own", "Any decade.")

    def test_the_server_text_reaches_the_engine_config(self, client: TestClient):
        client.post("/api/collections", json={"name": "Plain Row"})
        assert (
            client.put("/api/settings", json={"values": {"llm_web.instructions": "Favour classics."}}).status_code
            == 200
        )
        spec, config = self._spec(client, "plain_row")
        assert config.web_instructions == "Favour classics."
        assert spec.ai_instructions is None

    def test_ai_instructions_need_words_unless_they_use_the_default(self, client: TestClient):
        def post(instructions: dict):
            return client.post("/api/collections", json={"name": "X", "ai_instructions": instructions})

        assert post({"mode": "own", "text": "   "}).status_code == 422
        assert post({"mode": "add", "text": ""}).status_code == 422
        assert post({"mode": "nope", "text": "x"}).status_code == 422
        assert post({"mode": "add", "text": "x" * 2001}).status_code == 422

    def test_blank_instructions_are_not_required_when_the_row_has_web_search_off(self, client: TestClient):
        def post(sources: list[str]):
            body = {"name": "Quiet", "candidate_sources": sources, "ai_instructions": {"mode": "add", "text": ""}}
            return client.post("/api/collections", json=body)

        assert post(["tmdb_similar"]).status_code == 201
        assert post(["tmdb_similar", "llm_web"]).status_code == 422

    def test_a_row_saved_with_the_default_stores_nothing_new(self, client: TestClient):
        from shortlist.server.db.models import Collection

        rid = client.post("/api/collections", json={"name": "Quiet Row"}).json()["id"]
        with client.app.state.sessions() as session:
            assert session.get(Collection, rid).prompt == {}
        rows = client.get("/api/collections").json()
        assert next(r for r in rows if r["id"] == rid)["ai_instructions"] == {"mode": "default", "text": ""}

    def test_a_legacy_prompt_value_reads_as_the_default(self, client: TestClient):
        from shortlist.server.db.models import Collection

        rid = client.post("/api/collections", json={"name": "Old Row"}).json()["id"]
        with client.app.state.sessions() as session:
            session.get(Collection, rid).prompt = {"tone": "warm", "guidance": "old curate setting"}
            session.commit()
        row = next(r for r in client.get("/api/collections").json() if r["id"] == rid)
        assert row["ai_instructions"] == {"mode": "default", "text": ""}

    def test_a_patch_that_omits_ai_instructions_leaves_them_alone(self, client: TestClient):
        from shortlist.server.db.models import Event

        body = {"name": "Kept Row", "ai_instructions": {"mode": "add", "text": "No kids films."}}
        rid = client.post("/api/collections", json=body).json()["id"]
        patched = client.patch(f"/api/collections/{rid}", json={"name": "Kept Row Renamed"})
        assert patched.status_code == 200
        assert patched.json()["ai_instructions"] == {"mode": "add", "text": "No kids films."}
        with client.app.state.sessions() as session:
            assert session.query(Event).filter_by(scope="collection.ai_instructions").count() == 0

    def test_the_response_schema_declares_ai_instructions(self, client: TestClient):
        schema = client.app.openapi()["components"]["schemas"]["CollectionOut"]
        assert "ai_instructions" in schema["properties"]
        assert "ai_instructions" in schema["required"]

    def test_changing_ai_instructions_is_audited(self, client: TestClient):
        from shortlist.server.db.models import Event

        rid = client.post("/api/collections", json={"name": "Audited Row"}).json()["id"]
        patch = {"name": "Audited Row", "ai_instructions": {"mode": "add", "text": "No kids films."}}
        assert client.patch(f"/api/collections/{rid}", json=patch).status_code == 200
        with client.app.state.sessions() as session:
            event = session.query(Event).filter_by(scope="collection.ai_instructions").one()
            assert event.message["mode"] == "add" and event.message["chars"] == len("No kids films.")
