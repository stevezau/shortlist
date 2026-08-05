"""Address and identifier redaction — the pass that guards every shareable artifact.

Written after a positive-control audit of a real 88 MB report found this server's machine id still in
two of the zipped log files, in the one escaping no word-boundary pattern can match.
"""

from __future__ import annotations

from shortlist.server.services.redaction import known_identifiers, redact_all, redact_literals, shape_hosts

MACHINE_ID = "7ee8abc1bcdcc79389ad1e15c30e2692714bc940"


class TestShapeHosts:
    def test_url_keeps_scheme_and_port_and_drops_the_host(self):
        assert shape_hosts("GET https://172.16.10.240:32400/library") == "GET https://<host>:32400/library"

    def test_connection_pool_error_kwarg_is_shaped(self):
        assert shape_hosts("host='172.16.10.240', port=32400") == "host='<host>', port=32400"

    def test_bare_address_with_no_scheme_is_shaped(self):
        """How `http_retry` logs every PMS call — 17,234 of them in one real report, all missed by the
        URL and kwarg patterns."""
        assert shape_hosts("GET 172.16.10.240 -> 200 in 0.03s") == "GET <host> -> 200 in 0.03s"

    def test_version_string_is_not_mistaken_for_an_address(self):
        assert shape_hosts("PMS 1.43.3.10861 ok") == "PMS 1.43.3.10861 ok"

    def test_plain_machine_id_is_shaped(self):
        assert shape_hosts(f"server {MACHINE_ID} ok") == "server <machine-id> ok"

    def test_url_encoded_machine_id_is_shaped(self):
        """THE regression. `%2F` ends in `F`, a hex character, so `\\b` finds no boundary before the id
        and a plain `\\b[0-9a-f]{32,40}\\b` matched nothing here."""
        shaped = shape_hosts(f"?type=1&uri=server%3A%2F%2F{MACHINE_ID}%2Fcom.plexapp")

        assert MACHINE_ID not in shaped
        assert shaped == "?type=1&uri=server%3A%2F%2F<machine-id>%2Fcom.plexapp"

    def test_machine_id_inside_a_plex_direct_hostname_is_shaped(self):
        shaped = shape_hosts(f"https://192-168-1-5.{MACHINE_ID[:32]}.plex.direct:32400/x")

        assert shaped == "https://<host>:32400/x"
        assert MACHINE_ID[:32] not in shaped, "the hostname embeds the machine id"

    def test_a_shorter_hex_string_is_not_taken_for_a_machine_id(self):
        """Plex item guids are 24 hex characters (`plex://episode/5d9c…`) and are diagnostically
        useful, so the floor is 32. (The `plex://` scheme's first segment is still shaped as a netloc —
        over-redaction, and deliberately preferred to a miss.)"""
        assert shape_hosts("guid 5d9c08e3e98e47001eb0d74d ok") == "guid 5d9c08e3e98e47001eb0d74d ok"


class TestRedactLiterals:
    def test_known_machine_id_is_replaced_whatever_the_escaping(self):
        """Including double-encoded, which no boundary pattern can reach: `%252F` puts an alphanumeric
        immediately left of the id, twice over."""
        text = f"a {MACHINE_ID} b %2F{MACHINE_ID}%2F c %252F{MACHINE_ID}%252F"

        out = redact_literals(text, {MACHINE_ID: "<machine-id>"})

        assert MACHINE_ID not in out
        assert out.count("<machine-id>") == 3

    def test_a_host_is_replaced_at_a_boundary(self):
        assert redact_literals("at 172.16.10.240 now", {"172.16.10.240": "<host>"}) == "at <host> now"

    def test_a_short_hostname_does_not_eat_the_words_around_it(self):
        """`plex` is the stock Docker Compose hostname for a PMS. A bare `str.replace` on it rewrites
        the loguru source field, `com.plexapp`, `plex.tv`, and a user called `plexfan` — the last of
        which then no longer matches the anonymiser, putting a mangled REAL username into an
        anonymised report."""
        text = "clients.plex_pms:_send - GET plex -> 200; com.plexapp; plex.tv; user plexfan"

        out = redact_literals(text, {"plex": "<host>"})

        assert out == "clients.plex_pms:_send - GET <host> -> 200; com.plexapp; plex.tv; user plexfan"

    def test_no_literals_is_a_no_op(self):
        assert redact_literals("unchanged", {}) == "unchanged"


class TestRedactAll:
    def test_literals_run_before_patterns_so_a_plex_direct_host_survives_intact(self):
        """The ordering defect, at the level it is decided. Patterns-first rewrites the middle of the
        hostname to `<machine-id>`, the exact literal then misses, and the dashed LAN IP on the front
        reaches the export."""
        short = MACHINE_ID[:32]
        host = f"192-168-1-5.{short}.plex.direct"

        out = redact_all(f"GET {host} -> 200", {host: "<host>", short: "<machine-id>"})

        assert out == "GET <host> -> 200"
        assert "192-168-1-5" not in out

    def test_the_patterns_still_catch_an_identifier_we_do_not_know(self):
        """Another server's machine id, in a plex.tv response. Literals cover ours; patterns are the
        net for everything else."""
        assert redact_all(f"other {MACHINE_ID}", {}) == "other <machine-id>"


class TestKnownIdentifiers:
    def _sessions(self, tmp_path):
        from shortlist.server.db.session import make_engine, make_session_factory, run_migrations

        run_migrations(tmp_path)
        return make_session_factory(make_engine(tmp_path))

    def _with_server(self, tmp_path, url: str):
        from shortlist.server.db.models import Server

        sessions = self._sessions(tmp_path)
        with sessions() as session:
            session.query(Server).delete()
            session.add(Server(machine_id=MACHINE_ID, url=url, name="SFLIX", token_enc=""))
            session.commit()
            return known_identifiers(session)

    def test_returns_machine_id_and_host_longest_first(self, tmp_path):
        found = self._with_server(tmp_path, "http://172.16.10.240:32400")

        assert found == {MACHINE_ID: "<machine-id>", "172.16.10.240": "<host>"}
        assert list(found) == [MACHINE_ID, "172.16.10.240"], "longest first"

    def test_no_server_row_yields_nothing_rather_than_raising(self, tmp_path):
        from shortlist.server.db.models import Server

        sessions = self._sessions(tmp_path)
        with sessions() as session:
            session.query(Server).delete()
            session.commit()
            assert known_identifiers(session) == {}

    def test_an_unparseable_url_does_not_sink_the_machine_id(self, tmp_path):
        assert self._with_server(tmp_path, "http://[oops") == {MACHINE_ID: "<machine-id>"}

    def test_a_short_hostname_is_kept_now_that_hosts_are_boundary_matched(self, tmp_path):
        """The old length floor dropped `pms` — a real Docker hostname — while admitting `plex`, which
        was the destructive one. Boundary matching makes the floor unnecessary in both directions."""
        assert self._with_server(tmp_path, "http://pms:32400") == {MACHINE_ID: "<machine-id>", "pms": "<host>"}

    def test_every_server_row_is_covered_not_just_the_first(self, tmp_path):
        """Linking a new server ADDS a row rather than replacing one, so `.first()` would protect the
        server the owner used to have and leave the current one exposed."""
        from shortlist.server.db.models import Server

        sessions = self._sessions(tmp_path)
        with sessions() as session:
            session.query(Server).delete()
            session.add(Server(machine_id="a" * 40, url="http://old:32400", name="old", token_enc=""))
            session.add(Server(machine_id="b" * 40, url="http://new:32400", name="new", token_enc=""))
            session.commit()

            assert known_identifiers(session) == {
                "a" * 40: "<machine-id>",
                "b" * 40: "<machine-id>",
                "old": "<host>",
                "new": "<host>",
            }
