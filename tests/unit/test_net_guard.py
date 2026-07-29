"""SSRF and path-traversal guards on URLs and filenames the server acts on.

The shape of these matters as much as the coverage: Shortlist is a self-hosted Docker app, so the
textbook SSRF defence — block RFC1918, block loopback — would break the product for everyone. These
tests pin BOTH halves: what is refused, and what must keep working.
"""

from __future__ import annotations

import pytest

from shortlist.server.net_guard import BlockedUrl, check_url, safe_backup_name


class TestUrlsSelfHostersNeed:
    """If any of these ever start failing, the app is broken for its actual users."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://192.168.1.50:32400",  # the single most common Plex URL there is
            "http://10.0.0.5:32400",
            "http://172.16.0.10:32400",  # the third RFC1918 block, which people do use
            "http://plex:32400",  # docker compose service name
            "http://localhost:11434",  # Ollama, the documented default
            "http://127.0.0.1:8181",  # Tautulli on the same host
            "https://plex.example.com",
            "http://[::1]:32400",
        ],
    )
    def test_private_and_loopback_addresses_are_allowed(self, url):
        check_url(url)  # must not raise


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # AWS/GCP/Azure/DO instance credentials
            "http://169.254.169.254",
            "http://100.100.100.200/",  # Alibaba
        ],
    )
    def test_cloud_metadata_addresses(self, url):
        """No media server runs here, and what does run here hands out IAM credentials to anything
        that can make an HTTP request from the instance."""
        with pytest.raises(BlockedUrl, match="metadata"):
            check_url(url)

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/", "//evil.com", "not a url"])
    def test_non_http_schemes(self, url):
        with pytest.raises(BlockedUrl):
            check_url(url)

    def test_a_hostname_resolving_to_metadata_is_caught(self, monkeypatch):
        """The block cannot be by literal string: any DNS name can point at the metadata address."""
        import shortlist.server.net_guard as guard

        monkeypatch.setattr(
            guard.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("169.254.169.254", 80))]
        )
        with pytest.raises(BlockedUrl, match="metadata"):
            check_url("http://totally-innocent.example.com")

    def test_an_unresolvable_host_is_allowed_through(self, monkeypatch):
        """A host that is merely down, or not up yet, must still be savable — otherwise a broken
        connection becomes unfixable from the UI, which is where you go to fix it."""
        import shortlist.server.net_guard as guard

        monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError))
        check_url("http://not-up-yet.local:32400")


class TestBackupNames:
    """`config_dir / "backups" / name` with an unvalidated name escapes the directory, and restore
    then copies whatever it finds over the database."""

    @pytest.mark.parametrize(
        "name",
        ["../../etc/passwd", "../shortlist.db", "sub/dir.db", "..\\..\\windows", "/etc/passwd", "..", ""],
    )
    def test_traversal_is_refused(self, name):
        with pytest.raises(ValueError):
            safe_backup_name(name)

    def test_a_real_backup_name_passes(self):
        assert safe_backup_name("shortlist_20260729_002652_pre-migration.db").endswith(".db")


class TestTheGuardsAreWired:
    """A guard nothing calls is a guard that does not exist. These pin the call sites."""

    def test_restore_refuses_a_traversing_name_without_touching_the_db(self, tmp_path):
        """The backups directory must EXIST for the traversal to resolve — `a/backups/../x` is not a
        path at all if `backups` is missing, so a version of this test without the mkdir passed with
        the guard removed. The `..` has to actually get somewhere for this to prove anything."""
        from shortlist.server.services.backup import BACKUP_SUBDIR, restore_backup

        (tmp_path / "shortlist.db").write_text("the real database")
        (tmp_path / BACKUP_SUBDIR).mkdir(parents=True, exist_ok=True)
        (tmp_path / "secret.key").write_text("not a database")

        assert restore_backup(tmp_path, "../secret.key") is False
        assert (tmp_path / "shortlist.db").read_text() == "the real database", "the traversal was followed"

    def test_the_setup_probe_refuses_a_metadata_url(self):
        """The one server-side fetch an UNAUTHENTICATED caller can aim, on a fresh install."""
        from shortlist.server.net_guard import BlockedUrl
        from shortlist.server.services.setup_probe import run_capability_probe

        with pytest.raises(BlockedUrl, match="metadata"):
            run_capability_probe("http://169.254.169.254", "tok", "cid")

    def _client(self, tmp_path):
        """Through the real endpoint — calling `_reject_blocked_urls` directly proves the function
        works and nothing about whether the PUT handler calls it, which is the half that regresses."""
        from starlette.testclient import TestClient

        from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
        from shortlist.server.db.models import Server
        from shortlist.server.main import create_app

        app = create_app(config_dir=tmp_path)
        client = TestClient(app)
        client.__enter__()
        with app.state.sessions() as session:
            session.add(
                Server(machine_id="m1", url="http://pms:32400", token_enc="x", owner_account_id=7, capabilities={})
            )
            session.commit()
        client.cookies.set(SESSION_COOKIE, session_serializer(app.state.session_secret).dumps({"account_id": 7}))
        client.headers[CSRF_HEADER] = "1"
        return client

    def test_saving_a_metadata_url_through_the_api_is_refused(self, tmp_path):
        client = self._client(tmp_path)

        r = client.put("/api/settings", json={"values": {"requests.radarr.url": "http://169.254.169.254"}})

        assert r.status_code == 422
        assert client.get("/api/settings").json()["requests.radarr.url"] != "http://169.254.169.254"

    def test_saving_normal_self_hosted_urls_through_the_api_works(self, tmp_path):
        """The half that matters more: this app is useless if a LAN address is refused."""
        client = self._client(tmp_path)

        r = client.put(
            "/api/settings",
            json={
                "values": {
                    "requests.radarr.url": "http://192.168.1.50:7878",
                    "curator.ollama_url": "http://ollama:11434",
                }
            },
        )

        assert r.status_code == 200
        assert client.get("/api/settings").json()["requests.radarr.url"] == "http://192.168.1.50:7878"
