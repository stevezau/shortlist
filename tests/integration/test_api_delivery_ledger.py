"""API contract tests: the delivery ledger — which Plex collection is which row, for whom, in which library."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shortlist.server.db.models import User

pytestmark = pytest.mark.integration


class TestDeliveryLedger:
    """Which Plex collection is which row, for whom, in which library — recorded at delivery time.

    Every reconcile needs that answer, and every other way of asking is a guess. Rendering the row's
    name template covers a static / `{library_name}` / `{user}` title but CANNOT cover `{top_seed}`,
    which renders differently every run. The run breakdown that used to fill the gap is scoped to one
    run and erased outright by `DELETE /api/runs`.
    """

    def _plex(self, monkeypatch, client, *, collections):
        """collections: [(title, label, ratingKey)]. Returns the list of deleted (title, ratingKey)."""
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        deleted: list[tuple[str, int]] = []
        section = SimpleNamespace(title="Movies", key="1", type="movie")
        plex = MagicMock()
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda s, label: [
            SimpleNamespace(title=title, ratingKey=key) for (title, lbl, key) in collections if lbl == label
        ]
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append((c.title, c.ratingKey))
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)
        return deleted

    def test_a_top_seed_row_is_removed_by_identity_when_no_title_can_be_computed(self, client: TestClient, monkeypatch):
        """The case nothing else could reach. `{top_seed}` renders to a different title every run, so
        `_rendered_titles` deliberately returns nothing for it rather than match every row — and with
        run history cleared there is no recorded title either. The ledger's ratingKey is the only
        handle left."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Delivery

        created = client.post(
            "/api/collections", json={"name": "Because", "name_template": "Because you watched {top_seed}"}
        )
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
            session.add(
                Delivery(collection_slug=slug, user_slug=uslug, library_key="1", rating_key=9001, title="whatever")
            )
            session.commit()
        client.delete("/api/runs")  # the record the old code depended on, gone

        deleted = self._plex(
            monkeypatch,
            client,
            collections=[("Because you watched Dune" + row_marker(acct), f"shortlist_{uslug}", 9001)],
        )
        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})

        assert r.status_code == 200
        assert deleted == [("Because you watched Dune" + row_marker(acct), 9001)]

    def test_identity_matching_never_reaches_another_rows_collection(self, client: TestClient, monkeypatch):
        """A ratingKey narrows the search; it must never widen ownership. Every candidate is still
        found under the user's own label, and only the keys THIS row recorded are matched."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Delivery

        keep = client.post("/api/collections", json={"name": "Keep Me"})
        drop = client.post(
            "/api/collections", json={"name": "Because", "name_template": "Because you watched {top_seed}"}
        )
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
            session.add(
                Delivery(
                    collection_slug=drop.json()["slug"], user_slug=uslug, library_key="1", rating_key=9001, title="x"
                )
            )
            session.add(
                Delivery(
                    collection_slug=keep.json()["slug"], user_slug=uslug, library_key="1", rating_key=9002, title="y"
                )
            )
            session.commit()

        deleted = self._plex(
            monkeypatch,
            client,
            collections=[
                ("Because you watched Dune" + row_marker(acct), f"shortlist_{uslug}", 9001),
                ("Keep Me" + row_marker(acct), f"shortlist_{uslug}", 9002),
            ],
        )
        client.post(f"/api/collections/{drop.json()['id']}/cleanup", json={"dry_run": False})

        assert [key for _title, key in deleted] == [9001]

    def test_removing_a_row_forgets_its_ledger_entries(self, client: TestClient, monkeypatch):
        """The ledger records collections that EXIST. Left behind, it would grow for ever and its
        entries would name objects that are gone."""
        from shortlist.server.db.models import Delivery

        created = client.post("/api/collections", json={"name": "Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            uslug = session.query(User).order_by(User.id).first().slug
            session.add(Delivery(collection_slug=slug, user_slug=uslug, library_key="1", rating_key=9001, title="Gems"))
            session.commit()
        self._plex(monkeypatch, client, collections=[("Gems", f"shortlist_{uslug}", 9001)])

        client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})

        with client.app.state.sessions() as session:
            assert session.query(Delivery).filter_by(collection_slug=slug).count() == 0

    def test_a_dry_run_keeps_the_ledger(self, client: TestClient, monkeypatch):
        """A preview changed nothing on Plex. Forgetting here would leave the next real attempt with
        no ledger to address by — the exact gap this table exists to close."""
        from shortlist.server.db.models import Delivery

        created = client.post("/api/collections", json={"name": "Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            uslug = session.query(User).order_by(User.id).first().slug
            session.add(Delivery(collection_slug=slug, user_slug=uslug, library_key="1", rating_key=9001, title="Gems"))
            session.commit()
        deleted = self._plex(monkeypatch, client, collections=[("Gems", f"shortlist_{uslug}", 9001)])

        client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": True})

        assert deleted == []
        with client.app.state.sessions() as session:
            assert session.query(Delivery).filter_by(collection_slug=slug).count() == 1

    def test_narrowing_a_row_forgets_only_the_library_it_left(self, client: TestClient, monkeypatch):
        """The cell where the ledger's lifecycle and the narrowing path meet — and neither test class
        covered it, which is how the over-delete shipped.

        A narrowed row removes SOME libraries. Forgetting the whole row would drop the entry for a
        collection that is still live, and for a `{top_seed}` row that entry is the only thing that
        could ever address it: its title cannot be re-rendered, and a row with a blank schedule has no
        next run to re-populate the ledger. The collection would be stranded on Plex for good.
        """
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig
        from shortlist.server.db.models import Delivery, User

        created = client.post(
            "/api/collections",
            json={"name": "Because", "name_template": "Because you watched {top_seed}", "media": "both"},
        )
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug = user.slug
            for library_key, rating_key in (("1", 9001), ("2", 9002)):
                session.add(
                    Delivery(
                        collection_slug=slug,
                        user_slug=uslug,
                        library_key=library_key,
                        rating_key=rating_key,
                        title="Because you watched Dune",
                    )
                )
            session.commit()

        deleted: list[int] = []
        movies = SimpleNamespace(title="Movies", key="1", type="movie")
        shows = SimpleNamespace(title="TV", key="2", type="show")
        plex = MagicMock()
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.side_effect = lambda s, label: (
            [SimpleNamespace(title="Because you watched Dune", ratingKey=9001 if str(s.key) == "1" else 9002)]
            if label == f"shortlist_{uslug}"
            else []
        )
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.ratingKey)
        monkeypatch.setattr(
            client.app.state.run_service,
            "build_context",
            lambda **kw: SimpleNamespace(plex=plex, config=EngineConfig()),
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Because", "media": "movie"})

        assert r.status_code == 200
        assert deleted == [9002], "only the TV copy — the Movies one is still targeted and live"
        with client.app.state.sessions() as session:
            left = {d.library_key for d in session.query(Delivery).filter_by(collection_slug=slug)}
        assert left == {"1"}, "the live Movies collection must stay addressable"
