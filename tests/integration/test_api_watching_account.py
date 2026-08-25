"""`/api/watching-account/*` — the owner's escape from seeing everyone's rows.

The transfer endpoint is the one that matters: it decides which account gets written to, and whether
a dry run really is one. Both are asserted on the KWARGS reaching the service rather than on the
response, because a handler that quietly dropped `dry_run` — or swapped the two tokens — would still
return 200.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from shortlist.server.db.models import User, WatchedTitle, utcnow
from shortlist.server.services.watching_account import TransferReport

# Patched where the JOB HANDLER looks them up, not where the endpoint used to. The endpoint now
# queues the work and returns the job's stored report, so the service is reached through jobs.py.
TRANSFER = "shortlist.server.services.watching_account.transfer_watch_history"
UNDO = "shortlist.server.services.watching_account.undo_transfer"


def _plex_ctx(*, dry_run: bool = False) -> MagicMock:
    """A `plex_only` context whose two tokens are distinguishable, so a swap is visible."""
    ctx = MagicMock()
    ctx.config.dry_run = dry_run
    ctx.plex.token = "ADMIN-TOKEN"
    ctx.plextv.canary_server_token.return_value = "TARGET-TOKEN"
    return ctx


def _report(**overrides) -> TransferReport:
    return TransferReport(**overrides)


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
    def test_every_transfer_builds_a_plex_context(self, client):
        """There is no non-Plex path any more. The old endpoint had a "plain copy" that wrote only to
        our own tables and deliberately never built a context — which also meant it skipped the safe
        mode chokepoint while still writing irreversibly into someone's watched set."""
        _, target_id = _seed_owner_and_target(client)
        ctx = _plex_ctx()

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=ctx) as build,
            patch(TRANSFER, return_value=_report()),
        ):
            r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert r.status_code == 200, r.text
        assert build.called

    def test_it_writes_with_the_targets_own_token_and_reads_with_the_admins(self, client):
        """The whole feature turns on this pair. Reading as the target would replicate the target onto
        itself; writing as the owner would mark the OWNER's account, which is the account being
        escaped from."""
        _, target_id = _seed_owner_and_target(client)
        ctx = _plex_ctx()

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=ctx),
            patch(TRANSFER, return_value=_report()) as service,
        ):
            client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        kwargs = service.call_args.kwargs
        assert kwargs["target_token"] == "TARGET-TOKEN"
        assert kwargs["source_token"] == "ADMIN-TOKEN"
        ctx.plextv.canary_server_token.assert_called_once_with(555000300)

    def test_dry_run_reaches_the_service(self, client):
        _, target_id = _seed_owner_and_target(client)

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch(TRANSFER, return_value=_report(dry_run=True)) as service,
        ):
            r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id, "dry_run": True})

        assert service.call_args.kwargs["dry_run"] is True
        assert r.json()["dry_run"] is True

    def test_safe_mode_forces_a_dry_run(self, client):
        """Safe mode is read back off the built context, not trusted from the request body."""
        _, target_id = _seed_owner_and_target(client)
        ctx = _plex_ctx(dry_run=True)

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=ctx),
            patch(TRANSFER, return_value=_report(dry_run=True)) as service,
        ):
            client.post("/api/watching-account/transfer", json={"to_user_id": target_id, "dry_run": False})

        assert service.call_args.kwargs["dry_run"] is True

    def test_the_schema_carries_the_fields_the_confirmation_screen_needs(self, client):
        """The web types are generated from this schema, so a field missing here is a field the UI
        cannot show — and `unmarks`/`removals_preview` are what tell someone this will DELETE."""
        props = client.app.openapi()["components"]["schemas"]["TransferOut"]["properties"]

        for field in ("planned", "unmarks", "removals_preview", "snapshot_id", "source_empty", "verify_mismatched"):
            assert field in props, field

    def test_the_transfer_is_audited_including_what_it_removed(self, client):
        """Rule 10 — "what changed on whose account at 03:31" has to be answerable afterwards, and
        this is the one path here that can delete watch history."""
        from shortlist.server.db.models import Event

        _, target_id = _seed_owner_and_target(client)

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch(TRANSFER, return_value=_report(unmarks=7, removals_preview=["Jaws"])),
        ):
            client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        with client.app.state.sessions() as session:
            event = session.query(Event).filter(Event.scope == "watching_account.transfer").one()
            assert event.message["unmarks"] == 7
            assert event.message["removals_preview"] == ["Jaws"]
            assert event.message["to_user_id"] == target_id

    def test_an_unknown_target_is_a_404(self, client):
        _seed_owner_and_target(client)

        with patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()):
            r = client.post("/api/watching-account/transfer", json={"to_user_id": 99999})

        assert r.status_code == 404

    def test_transferring_onto_the_owner_is_refused(self, client):
        owner_id, _ = _seed_owner_and_target(client)

        with patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()):
            r = client.post("/api/watching-account/transfer", json={"to_user_id": owner_id})

        assert r.status_code == 400

    def test_no_owner_registered_yet_says_so(self, client):
        """The stock `client` fixture has sarah and mike but no owner row."""
        with client.app.state.sessions() as session:
            target_id = session.query(User).filter(User.username == "sarah").one().id

        with patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()):
            r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert r.status_code == 409
        assert "user sync" in r.json()["detail"]

    def test_needs_the_owner_session(self, client):
        client.cookies.clear()
        assert client.post("/api/watching-account/transfer", json={"to_user_id": 1}).status_code in (401, 403)


class TestUndoEndpoint:
    def test_it_restores_from_the_snapshot_that_transfer_took(self, client):
        from shortlist.server.db.models import WatchStateSnapshot

        _, target_id = _seed_owner_and_target(client)
        with client.app.state.sessions() as session:
            snapshot = WatchStateSnapshot(user_id=target_id, state=[[77, 2, 900, "movie"]], taken_at=utcnow())
            session.add(snapshot)
            session.commit()
            snapshot_id = snapshot.id

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch(UNDO, return_value=_report()) as service,
        ):
            r = client.post("/api/watching-account/undo", json={"snapshot_id": snapshot_id})

        assert r.status_code == 200, r.text
        assert service.call_args.kwargs["snapshot_id"] == snapshot_id
        # Resolved from the SNAPSHOT's user, not from the request: an undo pointed at the wrong
        # account would restore one person's history onto another's.
        assert service.call_args.kwargs["target_token"] == "TARGET-TOKEN"

    def test_an_unknown_snapshot_is_a_404(self, client):
        _seed_owner_and_target(client)

        r = client.post("/api/watching-account/undo", json={"snapshot_id": 99999})

        assert r.status_code == 404

    def test_needs_the_owner_session(self, client):
        client.cookies.clear()
        assert client.post("/api/watching-account/undo", json={"snapshot_id": 1}).status_code in (401, 403)


class TestItNoLongerDependsOnAWarmCache:
    """#88 is gone structurally, not by being handled.

    The old transfer copied `watched_titles`, which nothing filled for an owner without a row of
    their own — so it reported 0 for ever and the wizard's offer to bring the history across was a
    dead end. The replication reads the SOURCE ACCOUNT from Plex, so an empty cache is irrelevant.
    """

    def test_an_owner_with_an_empty_cache_still_has_something_to_replicate(self, client):
        from shortlist.engine.watch_replica import ItemState, WatchState

        app = client.app
        with app.state.sessions() as session:
            # enabled=False, exactly as `user_sync` creates the owner.
            owner = User(plex_account_id=555000001, username="steve", slug="steve", user_type="owner", enabled=False)
            target = User(plex_account_id=555000300, username="steve-tv", slug="steve-tv", user_type="managed")
            session.add_all([owner, target])
            session.commit()
            owner_id, target_id = owner.id, target.id

        ctx = _plex_ctx()
        states = {
            "ADMIN-TOKEN": WatchState(items={7: ItemState(rating_key=7, media_type="movie", view_count=1)}),
            "TARGET-TOKEN": WatchState(items={}),
        }
        ctx.plex.read_watch_state.side_effect = lambda sections, token: states[token]
        ctx.plex.apply_watch_op.return_value = True
        ctx.plex.play_history.return_value = []

        with app.state.sessions() as session:
            assert session.query(WatchedTitle).filter_by(user_id=owner_id).count() == 0

        with patch.object(app.state.run_service, "build_context", return_value=ctx):
            r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        body = r.json()
        assert body["source_empty"] is False
        assert body["planned"] == 1


class TestItRunsOnTheDurableQueue:
    """The transfer is ~11,000 PMS writes on a heavy account.

    Held open as a plain request, a reverse proxy times it out at 60s and the work stops half-applied
    with no record of how far it got. On the queue the row is committed first, so a timed-out request
    still finishes, is retried on failure, and leaves its report on the Jobs page.
    """

    def test_it_leaves_a_job_row_carrying_the_report(self, client):
        from shortlist.server.db.models import Job

        _, target_id = _seed_owner_and_target(client)

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch(TRANSFER, return_value=_report(marks=12, applied=12)),
        ):
            client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        with client.app.state.sessions() as session:
            job = session.query(Job).filter(Job.kind == "watching_account.transfer").one()
            assert job.status == "done"
            assert job.payload["to_user_id"] == target_id
            assert job.result["marks"] == 12
            # The one-liner the Jobs page shows, so a timed-out request is still answerable there.
            assert "12" in job.result["detail"]

    def test_a_failure_is_reported_without_leaking_a_token(self, client):
        """rule 9. A plex.tv failure can carry a tokened URL, and this is the response body."""
        _, target_id = _seed_owner_and_target(client)
        boom = RuntimeError("GET https://plex.tv/api/resources?X-Plex-Token=supersecret failed")

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch(TRANSFER, side_effect=boom),
        ):
            r = client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert r.status_code == 502
        assert "supersecret" not in r.text

    def test_the_undo_runs_on_the_queue_too(self, client):
        from shortlist.server.db.models import Job, WatchStateSnapshot

        _, target_id = _seed_owner_and_target(client)
        with client.app.state.sessions() as session:
            snapshot = WatchStateSnapshot(user_id=target_id, state=[[77, 2, 900, "movie"]], taken_at=utcnow())
            session.add(snapshot)
            session.commit()
            snapshot_id = snapshot.id

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch(UNDO, return_value=_report(planned=3)),
        ):
            client.post("/api/watching-account/undo", json={"snapshot_id": snapshot_id})

        with client.app.state.sessions() as session:
            job = session.query(Job).filter(Job.kind == "watching_account.undo").one()
            assert (job.status, job.payload["snapshot_id"]) == ("done", snapshot_id)

    def test_both_kinds_declare_that_they_write_to_plex(self, client):
        """`writes_plex` decides whether a kind takes the exclusive lock an engine run also holds. A
        transfer rewriting thousands of watch flags while a run converges collections is exactly the
        overlap that lock exists for — and this one can also DELETE watch history."""
        from shortlist.server.services.jobs import BY_KIND

        assert BY_KIND["watching_account.transfer"].writes_plex is True
        assert BY_KIND["watching_account.undo"].writes_plex is True

    def test_neither_kind_is_offered_as_a_press_me_button(self, client):
        """Both need a payload naming an account. A generic "run now" button would queue one that can
        only fail, and the Jobs page's buttons are built from `manual`."""
        from shortlist.server.services.jobs import KINDS

        assert "watching_account.transfer" not in KINDS
        assert "watching_account.undo" not in KINDS

    def test_an_unusable_target_never_reaches_the_queue(self, client):
        """Fail fast on the request. A job that can only fail retries three times, writes a failure to
        the Jobs page, and answers 502 for what is really "you picked the wrong account"."""
        from shortlist.server.db.models import Job

        _seed_owner_and_target(client)
        with client.app.state.sessions() as session:
            shared = User(plex_account_id=999, username="sarah2", slug="sarah2", user_type="shared")
            session.add(shared)
            session.commit()
            shared_id = shared.id

        r = client.post("/api/watching-account/transfer", json={"to_user_id": shared_id})

        assert r.status_code == 400
        assert "Plex Home users" in r.json()["detail"]
        with client.app.state.sessions() as session:
            assert session.query(Job).count() == 0


class TestTheSnapshotListing:
    """`GET /watching-account/snapshots` — what makes an undo reachable after a timed-out request.

    Three cells and no coverage until now. The one that matters is the exclusion: an UNDO takes a
    snapshot too (that is what makes an undo undoable), but restoring it RE-APPLIES the transfer it
    reversed — and since the undo deletes the copied play events, the re-applied state arrives
    undated. Offering that under "an earlier copy can still be undone" would be the opposite of what
    it says.
    """

    def _snapshot(self, client, user_id: int, *, job_kind: str | None = None, restored: bool = False):
        from shortlist.server.db.models import Job, WatchStateSnapshot

        with client.app.state.sessions() as session:
            job_id = None
            if job_kind is not None:
                job = Job(kind=job_kind, payload={})
                session.add(job)
                session.flush()
                job_id = job.id
            row = WatchStateSnapshot(
                user_id=user_id,
                job_id=job_id,
                state=[[1, 1, 0, "movie", None]],
                taken_at=utcnow(),
                restored_at=utcnow() if restored else None,
            )
            session.add(row)
            session.commit()
            return row.id

    def test_it_lists_a_transfers_snapshot(self, client):
        _, target_id = _seed_owner_and_target(client)
        made = self._snapshot(client, target_id, job_kind="watching_account.transfer")

        body = client.get("/api/watching-account/snapshots").json()

        assert [s["id"] for s in body] == [made]
        assert body[0]["username"] == "steve-tv"
        assert body[0]["entries"] == 1

    def test_it_lists_one_with_no_job_recorded(self, client):
        """`job_id` is nullable and older rows carry none — they are transfer-origin by construction."""
        _, target_id = _seed_owner_and_target(client)
        made = self._snapshot(client, target_id)

        assert [s["id"] for s in client.get("/api/watching-account/snapshots").json()] == [made]

    def test_it_hides_a_snapshot_an_UNDO_took(self, client):
        """Restoring this one re-applies the transfer, undated. It is not "an earlier copy"."""
        _, target_id = _seed_owner_and_target(client)
        self._snapshot(client, target_id, job_kind="watching_account.undo")

        assert client.get("/api/watching-account/snapshots").json() == []

    def test_it_hides_one_already_restored(self, client):
        _, target_id = _seed_owner_and_target(client)
        self._snapshot(client, target_id, job_kind="watching_account.transfer", restored=True)

        assert client.get("/api/watching-account/snapshots").json() == []

    def test_an_incomplete_snapshot_is_listed_but_flagged(self, client):
        """Listed so the owner can see WHY there is no undo, rather than it silently missing."""
        from shortlist.server.db.models import WatchStateSnapshot

        _, target_id = _seed_owner_and_target(client)
        made = self._snapshot(client, target_id, job_kind="watching_account.transfer")
        with client.app.state.sessions() as session:
            session.get(WatchStateSnapshot, made).complete = False
            session.commit()

        body = client.get("/api/watching-account/snapshots").json()

        assert [(s["id"], s["complete"]) for s in body] == [(made, False)]

    def test_needs_the_owner_session(self, client):
        client.cookies.clear()
        assert client.get("/api/watching-account/snapshots").status_code in (401, 403)


class TestTheSourceCanBeAnAccountOtherThanTheOwner:
    """Someone who already moved to a watching account once, and is moving again.

    Their history is on THAT account, not on the admin one they abandoned — which is the maintainer's
    own situation. The service always took any `from_user_id`; only the endpoint hardcoded the owner.

    The token is the load-bearing part: reading a shared account with the ADMIN token would replicate
    the owner's watching onto the target while claiming to copy that person's — one account's history
    silently wearing another's name.
    """

    def _shared(self, client, username="moohouse"):
        with client.app.state.sessions() as session:
            user = User(plex_account_id=555000900, username=username, slug=username, user_type="shared")
            session.add(user)
            session.commit()
            return user.id

    def test_it_defaults_to_the_owner_when_no_source_is_given(self, client):
        owner_id, target_id = _seed_owner_and_target(client)

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch(TRANSFER, return_value=_report()) as service,
        ):
            client.post("/api/watching-account/transfer", json={"to_user_id": target_id})

        assert service.call_args.kwargs["from_user_id"] == owner_id

    def test_a_named_source_reaches_the_service(self, client):
        _, target_id = _seed_owner_and_target(client)
        source_id = self._shared(client)

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch(TRANSFER, return_value=_report()) as service,
        ):
            client.post(
                "/api/watching-account/transfer",
                json={"to_user_id": target_id, "from_user_id": source_id},
            )

        assert service.call_args.kwargs["from_user_id"] == source_id

    def test_a_shared_source_is_read_with_its_OWN_token(self, client):
        """Not the admin's. This is the assertion that stops one person's history being copied under
        another's name.

        The REAL owner/shared/managed split runs here — only the plex.tv boundary is stubbed. The
        first version patched `server_token_for` itself, which meant the decision under test was the
        mock: hardcoding `UserType.OWNER` in the handler (so a shared source reads with the ADMIN
        token) passed all 118 tests in this suite and its unit sibling.
        """
        _, target_id = _seed_owner_and_target(client)
        source_id = self._shared(client)
        ctx = _plex_ctx()
        ctx.plextv.shared_server_tokens.return_value = {555000900: "MOOHOUSE-TOKEN"}

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=ctx),
            patch(TRANSFER, return_value=_report()) as service,
        ):
            client.post(
                "/api/watching-account/transfer",
                json={"to_user_id": target_id, "from_user_id": source_id},
            )

        assert service.call_args.kwargs["source_token"] == "MOOHOUSE-TOKEN"
        # And emphatically NOT the admin token, which is what a wrong `user_type` would select.
        assert service.call_args.kwargs["source_token"] != "ADMIN-TOKEN"

    def test_a_managed_source_falls_back_to_a_canary_exchanged_token(self, client):
        """The third cell of the `user_type` matrix, and the one the feature was built for.

        `docs/reference.md` says to name a source when the history lives on "an account you already
        moved to" — and a watching account is enforced to be MANAGED, so the realistic non-owner
        source is precisely this one. It is also the only cell that takes the switch-and-exchange
        path rather than the shared roster.
        """
        _, target_id = _seed_owner_and_target(client)
        with client.app.state.sessions() as session:
            managed = User(plex_account_id=555000901, username="old-tv", slug="old-tv", user_type="managed")
            session.add(managed)
            session.commit()
            source_id = managed.id
        ctx = _plex_ctx()
        ctx.plextv.shared_server_tokens.return_value = {}  # roster MISS, so the canary path runs
        ctx.plextv.canary_server_token.side_effect = lambda account: f"canary-{account}"

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=ctx),
            patch(TRANSFER, return_value=_report()) as service,
        ):
            client.post(
                "/api/watching-account/transfer",
                json={"to_user_id": target_id, "from_user_id": source_id},
            )

        kwargs = service.call_args.kwargs
        # Minted for the SOURCE and for the TARGET separately — the handler asks for both, and
        # nothing pinned that they were different accounts.
        assert kwargs["source_token"] == "canary-555000901"
        assert kwargs["target_token"] == "canary-555000300"

    def test_an_owner_source_uses_the_admin_token_and_asks_plex_tv_for_nothing(self, client):
        """The default path must not gain a plex.tv round trip: `_token_for` short-circuits on OWNER
        before the roster fetch."""
        owner_id, target_id = _seed_owner_and_target(client)
        ctx = _plex_ctx()

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=ctx),
            patch(TRANSFER, return_value=_report()) as service,
        ):
            client.post(
                "/api/watching-account/transfer",
                json={"to_user_id": target_id, "from_user_id": owner_id},
            )

        assert service.call_args.kwargs["source_token"] == "ADMIN-TOKEN"
        ctx.plextv.shared_server_tokens.assert_not_called()

    def test_a_source_with_no_obtainable_token_fails_loudly(self, client):
        """Rather than silently falling back to the admin token and copying the wrong account."""
        _, target_id = _seed_owner_and_target(client)
        source_id = self._shared(client)

        with (
            patch.object(client.app.state.run_service, "build_context", return_value=_plex_ctx()),
            patch("shortlist.engine.history.ShareTokenWatchSource.server_token_for", return_value=None),
        ):
            r = client.post(
                "/api/watching-account/transfer",
                json={"to_user_id": target_id, "from_user_id": source_id},
            )

        assert r.status_code == 502
        assert "no server token" in r.json()["detail"]

    def test_an_unknown_source_is_a_404_before_anything_is_queued(self, client):
        from shortlist.server.db.models import Job

        _, target_id = _seed_owner_and_target(client)

        r = client.post(
            "/api/watching-account/transfer",
            json={"to_user_id": target_id, "from_user_id": 99999},
        )

        assert r.status_code == 404
        with client.app.state.sessions() as session:
            assert session.query(Job).count() == 0

    def test_copying_an_account_onto_itself_is_still_refused(self, client):
        _, target_id = _seed_owner_and_target(client)

        r = client.post(
            "/api/watching-account/transfer",
            json={"to_user_id": target_id, "from_user_id": target_id},
        )

        assert r.status_code == 400
