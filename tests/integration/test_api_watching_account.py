"""`/api/watching-account/*` — the owner's escape from seeing everyone's rows.

The transfer endpoint is the one that matters: it decides whether Plex gets written to at all, and
whether a dry run really is one. Those are asserted on the KWARGS reaching the service, not just on
the response, because a handler that quietly dropped `scrobble` or `dry_run` would still 200.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from shortlist.server.db.models import User, WatchedTitle, utcnow

TRANSFER = "shortlist.server.api.watching_account.transfer_watch_history"


def _seed_owner_and_target(client) -> tuple[int, int]:
    """An owner with two watched titles, plus a Home user to move them onto."""
    app = client.app
    with app.state.sessions() as session:
        owner = User(plex_account_id=555000001, username="steve", slug="steve", user_type="owner", enabled=True)
        target = User(plex_account_id=555000300, username="steve-tv", slug="steve-tv", user_type="managed")
        session.add_all([owner, target])
        session.commit()
        for i in range(2):
            session.add(
                WatchedTitle(
                    user_id=owner.id,
                    section_key="1",
                    rating_key=100 + i,
                    tmdb_id=200 + i,
                    media_type="movie",
                    title=f"Film {i}",
                    viewed_at=utcnow(),
                )
            )
        session.commit()
        return owner.id, target.id


class TestCandidates:
    def test_lists_home_users_from_plex_tv(self, client):
        _seed_owner_and_target(client)
        ctx = MagicMock()
        ctx.plextv.home_users.return_value = [
            {"id": 555000001, "title": "Steve", "admin": True},
            {"id": 555000300, "title": "Steve TV"},
        ]
        with patch.object(client.app.state.run_service, "build_context", return_value=ctx):
            r = client.get("/api/watching-account/candidates")

        assert r.status_code == 200
        # The admin account is what's being escaped FROM, so it is never offered.
        assert [c["plex_account_id"] for c in r.json()] == [555000300]

    def test_a_plex_tv_failure_is_a_502_with_no_token_in_it(self, client):
        """A plex.tv error can carry a tokened URL — rule 9 says it must never reach the response."""
        _seed_owner_and_target(client)
        boom = RuntimeError("GET https://plex.tv/api/users?X-Plex-Token=supersecret failed")
        with patch.object(client.app.state.run_service, "build_context", side_effect=boom):
            r = client.get("/api/watching-account/candidates")

        assert r.status_code == 502
        assert "supersecret" not in r.text

    def test_needs_the_owner_session(self, client):
        client.cookies.clear()
        assert client.get("/api/watching-account/candidates").status_code in (401, 403)


class TestTransfer:
    def test_a_plain_copy_never_builds_a_plex_context(self, client):
        """The copy is pure DB work. Requiring a reachable PMS for it would make the whole feature
        fail on a server that is merely offline — and would be a Plex write path that isn't one."""
        _owner_id, target_id = _seed_owner_and_target(client)
        with patch.object(client.app.state.run_service, "build_context") as build:
            r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert r.status_code == 200
        assert r.json()["copied"] == 2
        build.assert_not_called()

    def test_the_copy_actually_lands_with_the_true_dates(self, client):
        _owner_id, target_id = _seed_owner_and_target(client)

        client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        with client.app.state.sessions() as session:
            rows = session.query(WatchedTitle).filter(WatchedTitle.user_id == target_id).all()
            assert len(rows) == 2
            # Every copied row carries the real date, which the watch sync must never overwrite.
            assert all(row.source_viewed_at is not None for row in rows)

    def test_an_owner_with_no_cached_history_is_told_so_not_handed_a_bare_zero(self, client):
        """The wizard offers this transfer before anything has ever read the owner's history, so
        the honest answer is "there is nothing to copy yet" — not a 200 saying 0 titles moved (#88)."""
        app = client.app
        with app.state.sessions() as session:
            owner = User(plex_account_id=555000001, username="steve", slug="steve", user_type="owner")
            target = User(plex_account_id=555000300, username="steve-tv", slug="steve-tv", user_type="managed")
            session.add_all([owner, target])
            session.commit()
            target_id = target.id

        r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert r.status_code == 200
        assert r.json()["source_empty"] is True

    def test_the_schema_declares_source_empty_so_the_generated_web_types_carry_it(self, client):
        """`PassthroughModel` lets the field reach the wire whether or not it is declared, but the
        web types are GENERATED from this schema — an undeclared field is one the UI cannot read
        without hand-writing a type, which the frontend rules forbid."""
        properties = client.app.openapi()["components"]["schemas"]["TransferOut"]["properties"]

        assert "source_empty" in properties

    def test_a_copy_with_something_to_copy_is_not_flagged_empty(self, client):
        _owner_id, target_id = _seed_owner_and_target(client)

        r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert (r.json()["copied"], r.json()["source_empty"]) == (2, False)

    def test_dry_run_reaches_the_service_and_writes_nothing(self, client):
        _owner_id, target_id = _seed_owner_and_target(client)

        r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id, "dry_run": True})

        assert r.status_code == 200
        assert r.json() == {
            "copied": 2,
            "already_present": 0,
            "scrobbled": 0,
            "scrobble_skipped": 0,
            "dry_run": True,
            "source_empty": False,
            "errors": [],
        }
        with client.app.state.sessions() as session:
            assert session.query(WatchedTitle).filter(WatchedTitle.user_id == target_id).count() == 0

    def test_scrobbling_passes_the_targets_own_token_not_the_owners(self, client):
        """The whole point of `canary_server_token`: marking a title played AS someone needs THEIR
        token. Sending the admin's would mark it watched for the wrong account."""
        owner_id, target_id = _seed_owner_and_target(client)
        ctx = MagicMock()
        ctx.config.dry_run = False
        ctx.plextv.canary_server_token.return_value = "target-token"
        with (
            patch.object(client.app.state.run_service, "build_context", return_value=ctx),
            patch(TRANSFER) as transfer,
        ):
            transfer.return_value = MagicMock(
                as_dict=lambda: {
                    "copied": 2,
                    "already_present": 0,
                    "scrobbled": 2,
                    "scrobble_skipped": 0,
                    "dry_run": False,
                    "source_empty": False,
                    "errors": [],
                },
                copied=2,
            )
            r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id, "scrobble": True})

        assert r.status_code == 200
        ctx.plextv.canary_server_token.assert_called_once_with(555000300)
        kwargs = transfer.call_args.kwargs
        assert kwargs["target_token"] == "target-token"
        assert kwargs["scrobble"] is True
        assert kwargs["from_user_id"] == owner_id
        assert kwargs["to_user_id"] == target_id

    def test_safe_mode_forces_a_scrobbling_transfer_to_dry_run(self, client):
        """`build_context` is the safe-mode chokepoint. A handler that trusted the request body
        instead would let SHORTLIST_DRY_RUN be bypassed by one JSON field."""
        _owner_id, target_id = _seed_owner_and_target(client)
        ctx = MagicMock()
        ctx.config.dry_run = True  # safe mode said so, even though the body says otherwise
        ctx.plextv.canary_server_token.return_value = "t"
        with (
            patch.object(client.app.state.run_service, "build_context", return_value=ctx),
            patch(TRANSFER) as transfer,
        ):
            transfer.return_value = MagicMock(
                as_dict=lambda: {
                    "copied": 0,
                    "already_present": 0,
                    "scrobbled": 0,
                    "scrobble_skipped": 0,
                    "dry_run": True,
                    "source_empty": False,
                    "errors": [],
                },
                copied=0,
            )
            client.post(
                "/api/watching-account/transfer",
                json={"to_user_id": target_id, "scrobble": True, "dry_run": False},
            )

        assert transfer.call_args.kwargs["dry_run"] is True

    def test_safe_mode_forces_a_PLAIN_transfer_to_dry_run_too(self, client):
        """The plain copy never builds a context, so it never met the usual SHORTLIST_DRY_RUN
        chokepoint — and it is still an irreversible write into another person's watched set."""
        _owner_id, target_id = _seed_owner_and_target(client)
        with patch("shortlist.server.api.watching_account.force_dry_run", return_value=True):
            r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert r.json()["dry_run"] is True
        with client.app.state.sessions() as session:
            assert session.query(WatchedTitle).filter(WatchedTitle.user_id == target_id).count() == 0

    def test_copying_onto_a_shared_user_is_refused(self, client):
        """The UI only offers Home users; the API has to enforce the same thing or a hand-rolled
        POST maps the owner's taste onto a friend's row."""
        _seed_owner_and_target(client)
        with client.app.state.sessions() as session:
            sarah_id = session.query(User).filter(User.username == "sarah").one().id

        r = client.post("/api/watching-account/transfer", json={"to_user_id": sarah_id})

        assert r.status_code == 400
        assert "Home users" in r.json()["detail"]

    def test_an_unknown_target_is_a_404(self, client):
        _seed_owner_and_target(client)

        r = client.post("/api/watching-account/transfer", json={"to_user_id": 99999})

        assert r.status_code == 404

    def test_transferring_onto_the_owner_is_refused(self, client):
        owner_id, _ = _seed_owner_and_target(client)

        r = client.post("/api/watching-account/transfer", json={"to_user_id": owner_id})

        assert r.status_code == 400

    def test_no_owner_registered_yet_says_so(self, client):
        """The stock `client` fixture has sarah and mike but no owner row."""
        with client.app.state.sessions() as session:
            target = session.query(User).filter(User.username == "sarah").one()
            target_id = target.id

        r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert r.status_code == 409
        assert "user sync" in r.json()["detail"]

    def test_the_transfer_is_audited(self, client):
        """Rule 10 — "what changed on whose account at 03:31" has to be answerable from the UI."""
        from shortlist.server.db.models import Event

        _, target_id = _seed_owner_and_target(client)

        client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        with client.app.state.sessions() as session:
            event = session.query(Event).filter(Event.scope == "watching_account.transfer").one()
            assert event.message["copied"] == 2
            assert event.message["to_user_id"] == target_id

    def test_needs_the_owner_session(self, client):
        client.cookies.clear()
        assert client.post("/api/watching-account/transfer", json={"to_user_id": 1}).status_code in (401, 403)


class TestTheWholeLoopAWizardWalksThrough:
    """Read the history, then copy it — the two halves the setup wizard has to chain.

    Each half is covered on its own elsewhere. This is the join, and the join is where the bug in
    #88 lived: the copy read a table that nothing ever filled for an owner without a row of their
    own, so it reported 0 for ever and the wizard's offer to bring the history across was a dead
    end. The history SOURCE is faked (so token selection is not exercised here — that is
    `test_run_service_context.py::test_the_owner_is_read_AS_the_owner_so_the_admin_token_is_the_one_used`);
    the job, the sweep, the cache and the copy are all real.
    """

    def test_syncing_the_history_first_makes_the_copy_land(self, client):
        from types import SimpleNamespace

        from shortlist.engine.models import MediaType, WatchedItem

        app = client.app
        with app.state.sessions() as session:
            # `enabled=False`, exactly as `user_sync` creates the owner: they get no row of their
            # own, which is the case that used to leave their watched set permanently empty.
            owner = User(plex_account_id=555000001, username="steve", slug="steve", user_type="owner", enabled=False)
            target = User(plex_account_id=555000300, username="steve-tv", slug="steve-tv", user_type="managed")
            session.add_all([owner, target])
            session.commit()
            owner_id, target_id = owner.id, target.id

        seen = WatchedItem(title="Dune", media_type=MediaType.MOVIE, watched_at=utcnow(), tmdb_id=42, rating_key=7)
        fake_ctx = SimpleNamespace(
            plex=SimpleNamespace(sections=lambda: [SimpleNamespace(key="1", type="movie")]),
            history_source=SimpleNamespace(
                fetch=lambda p, **k: [seen],
                fetch_section=lambda p, section, media_type, since=None: [seen],
            ),
            config=SimpleNamespace(min_completion=0.7),
        )

        with patch.object(app.state.run_service, "build_context", return_value=fake_ctx):
            before = client.post("/api/watching-account/transfer", json={"to_user_id": target_id, "dry_run": True})
            assert before.json()["source_empty"] is True

            job = client.post("/api/system/jobs", json={"kind": "sync.history"})
            assert job.status_code == 200, job.text

            after = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert (after.json()["copied"], after.json()["source_empty"]) == (1, False)
        with app.state.sessions() as session:
            assert session.query(WatchedTitle).filter_by(user_id=owner_id).count() == 1
            assert [t.title for t in session.query(WatchedTitle).filter_by(user_id=target_id)] == ["Dune"]
