"""The in-app Logs view's reader: parsing, filtering, and — above all — redaction.

This view exists so logs can be COPIED AND SHARED (a beta user pasted theirs straight into a public
GitHub issue). Every credential shape that could reach a log line therefore gets its own row here:
a miss is a token in someone's issue tracker, not a cosmetic bug.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from shortlist.server.services import log_reader

LINE = "2026-07-21 07:27:18.100 | {level:<8} | shortlist.server.main:lifespan:168 - {message}"


def write_log(config_dir: Path, *lines: str, name: str = "shortlist.log") -> Path:
    logs = config_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestRedaction:
    @pytest.mark.parametrize(
        ("secret", "line"),
        [
            ("abc123SECRET", "GET https://plex.tv/api/users?X-Plex-Token=abc123SECRET -> 200"),
            ("abc123SECRET", "GET http://pms:32400/library?api_key=abc123SECRET"),
            ("tok_SUPERSECRET", "headers {'X-Plex-Token': 'tok_SUPERSECRET'}"),
            ("tok_SUPERSECRET", "X-Plex-Token: tok_SUPERSECRET"),
            ("bearer_tok_12345678", "Authorization: Bearer bearer_tok_12345678"),
            ("json_tok_value", 'payload {"token": "json_tok_value"}'),
            ("json_key_value", "payload {'apikey': 'json_key_value'}"),
            ("sk-ant-api03-AAAABBBBCCCCDDDDEEEE", "curator key sk-ant-api03-AAAABBBBCCCCDDDDEEEE rejected"),
            # Real wire shapes, hyphens and all — an alnum-only pattern matches NEITHER of these,
            # and the fixture that used to stand in for them (`sk-proj0123…`, no hyphen) was written
            # to the regex rather than to any key OpenAI has issued since 2024.
            (
                "sk-proj-AbCdEf0123456789GhIjKlMnOpQrStUvWxYz012345",
                "openai key sk-proj-AbCdEf0123456789GhIjKlMnOpQrStUvWxYz012345 rejected",
            ),
            (
                "sk-or-v1-0123456789abcdef0123456789abcdef",
                "openrouter rejected sk-or-v1-0123456789abcdef0123456789abcdef",
            ),
            ("AIzaSyA0123456789abcdefghijklmnop", "google key AIzaSyA0123456789abcdefghijklmnop rejected"),
            ("xai-0123456789abcdefghijklmnop", "xai key xai-0123456789abcdefghijklmnop rejected"),
            ("gsk_0123456789abcdefghijklmnop", "groq key gsk_0123456789abcdefghijklmnop rejected"),
        ],
    )
    def test_every_known_credential_shape_is_stripped(self, secret: str, line: str):
        scrubbed = log_reader.scrub(line)
        assert secret not in scrubbed, f"{secret!r} survived redaction in {scrubbed!r}"
        assert "REDACTED" in scrubbed

    def test_the_line_still_reads_sensibly_afterwards(self):
        """Redaction replaces the secret, not the context — a log you can't read is no use."""
        scrubbed = log_reader.scrub("GET https://plex.tv/api/users?X-Plex-Token=abc123SECRET -> 200")
        assert scrubbed == "GET https://plex.tv/api/users?X-Plex-Token=REDACTED -> 200"

    def test_ordinary_text_is_left_alone(self):
        line = "MrJohnPoz: promotion skipped — a share filter was refused for account 12345"
        assert log_reader.scrub(line) == line


class TestParsing:
    def test_parses_level_source_and_message(self):
        entries = log_reader.parse(LINE.format(level="INFO", message="shortlist server up"))
        assert len(entries) == 1
        assert entries[0].level == "INFO"
        assert entries[0].source == "shortlist.server.main:lifespan:168"
        assert entries[0].message == "shortlist server up"
        assert entries[0].ts == "2026-07-21 07:27:18.100"

    def test_a_traceback_is_folded_into_the_entry_it_belongs_to(self):
        """The stack trace is the most useful part of an error — orphaning or dropping those lines
        would leave the operator with 'privacy sync failed' and nothing else."""
        text = "\n".join(
            [
                LINE.format(level="ERROR", message="privacy sync failed"),
                "Traceback (most recent call last):",
                '  File "/app/shortlist/engine/privacy.py", line 276, in sync_user_restrictions',
                "RuntimeError: plex.tv rejected the share-filter update: HTTP 400",
                LINE.format(level="INFO", message="next entry"),
            ]
        )

        entries = log_reader.parse(text)

        assert len(entries) == 2, "traceback frames must not become entries of their own"
        assert entries[0].level == "ERROR"
        assert "HTTP 400" in entries[0].message
        assert entries[1].message == "next entry"


class TestReadLines:
    def test_level_filter_means_this_level_and_louder(self, tmp_path: Path):
        write_log(
            tmp_path,
            LINE.format(level="DEBUG", message="d"),
            LINE.format(level="INFO", message="i"),
            LINE.format(level="WARNING", message="w"),
            LINE.format(level="ERROR", message="e"),
        )

        assert [x["level"] for x in log_reader.read_lines(tmp_path, level="WARNING")["lines"]] == ["WARNING", "ERROR"]
        assert len(log_reader.read_lines(tmp_path, level="DEBUG")["lines"]) == 4

    def test_search_matches_message_or_source(self, tmp_path: Path):
        write_log(
            tmp_path,
            LINE.format(level="INFO", message="promotion skipped"),
            LINE.format(level="INFO", message="something else"),
        )

        found = log_reader.read_lines(tmp_path, query="PROMOTION")  # case-insensitive
        assert [x["message"] for x in found["lines"]] == ["promotion skipped"]
        assert log_reader.read_lines(tmp_path, query="lifespan")["total_matched"] == 2  # source matches too

    def test_the_newest_lines_are_kept_and_truncation_is_reported(self, tmp_path: Path):
        write_log(tmp_path, *[LINE.format(level="INFO", message=f"line {i}") for i in range(50)])

        out = log_reader.read_lines(tmp_path, limit=10)

        assert out["truncated"] is True
        assert out["total_matched"] == 50
        assert out["lines"][0]["message"] == "line 40", "the most recent lines are the useful ones"

    def test_served_lines_are_redacted(self, tmp_path: Path):
        write_log(tmp_path, LINE.format(level="INFO", message="GET /x?X-Plex-Token=LEAKME -> 200"))

        assert "LEAKME" not in str(log_reader.read_lines(tmp_path))

    def test_an_instance_with_no_logs_yet_is_empty_not_an_error(self, tmp_path: Path):
        assert log_reader.read_lines(tmp_path) == {"lines": [], "total_matched": 0, "truncated": False, "file": None}


class TestTailAndZip:
    def test_tail_reads_from_a_line_boundary(self, tmp_path: Path):
        """Seeking into the middle of a 10 MB file lands mid-line; a half-line would fail to parse
        and silently vanish from the view."""
        path = write_log(tmp_path, *[LINE.format(level="INFO", message=f"line {i}") for i in range(200)])

        text = log_reader.tail_text(path, max_bytes=500)

        assert not text.startswith("line"), "a partial first line must be discarded"
        assert all(log_reader._LINE.match(line) for line in text.splitlines() if line.strip())

    def test_zip_contains_every_log_file_redacted(self, tmp_path: Path):
        write_log(tmp_path, LINE.format(level="INFO", message="current X-Plex-Token: LEAKME"))
        write_log(tmp_path, LINE.format(level="INFO", message="older"), name="shortlist.2026-07-20.log")

        archive = zipfile.ZipFile(io.BytesIO(log_reader.build_zip(tmp_path)))

        assert sorted(archive.namelist()) == ["logs/shortlist.2026-07-20.log", "logs/shortlist.log"]
        assert "LEAKME" not in archive.read("logs/shortlist.log").decode()

    def test_zip_is_still_valid_when_there_are_no_logs(self, tmp_path: Path):
        archive = zipfile.ZipFile(io.BytesIO(log_reader.build_zip(tmp_path)))
        assert archive.namelist() == ["logs/EMPTY.txt"]

    def test_zip_shapes_addresses_and_machine_ids_without_any_literals(self, tmp_path: Path):
        """The export is what gets attached to a public issue, so `scrub`'s credential pass was only
        ever a third of the job — a log file's bulk is addresses."""
        write_log(
            tmp_path,
            LINE.format(level="DEBUG", message="GET 172.16.10.240 -> 200 in 0.03s"),
            LINE.format(level="ERROR", message="host='172.16.10.240', port=32400 unreachable"),
            LINE.format(level="INFO", message="PMS 1.43.3.10861 ok"),
        )

        body = zipfile.ZipFile(io.BytesIO(log_reader.build_zip(tmp_path))).read("logs/shortlist.log").decode()

        assert "172.16.10.240" not in body
        assert body.count("<host>") == 2
        assert "1.43.3.10861" in body, "a version string is not an address"

    def test_zip_redacts_this_servers_own_identifiers_as_literals(self, tmp_path: Path):
        """The regression that reached a real report.zip: the machine id arrives URL-encoded, where no
        word-boundary pattern can find it, so the known value is replaced by exact match.

        DOUBLE-encoded on purpose. `%2F` is now caught by the pattern too, so a test using it passes
        with the literal pass deleted — which is what the first version of this test did. `%252F` is a
        shape only the literal pass can catch, so this can only go green for the right reason.
        """
        machine_id = "7ee8abc1bcdcc79389ad1e15c30e2692714bc940"
        write_log(
            tmp_path,
            LINE.format(
                level="DEBUG", message=f"PUT /library/collections?type=1&uri=server%253A%252F%252F{machine_id}%252Fcom"
            ),
        )

        body = (
            zipfile.ZipFile(io.BytesIO(log_reader.build_zip(tmp_path, {machine_id: "<machine-id>"})))
            .read("logs/shortlist.log")
            .decode()
        )

        assert machine_id not in body
        assert "<machine-id>" in body

    def test_zip_redacts_a_plex_direct_host_before_the_pattern_can_break_it(self, tmp_path: Path):
        """The ordering defect. A `plex.direct` hostname EMBEDS the machine id, so running the patterns
        first rewrites its middle to `<machine-id>` — after which the exact hostname no longer matches
        and the dashed LAN IP on the front survives into the export."""
        machine_id = "7ee8abc1bcdcc79389ad1e15c30e2692"
        host = f"192-168-1-5.{machine_id}.plex.direct"
        write_log(tmp_path, LINE.format(level="DEBUG", message=f"GET {host} -> 200 in 0.03s"))

        literals = {host: "<host>", machine_id: "<machine-id>"}
        body = zipfile.ZipFile(io.BytesIO(log_reader.build_zip(tmp_path, literals))).read("logs/shortlist.log").decode()

        assert "192-168-1-5" not in body, "the LAN IP survived on the front of the hostname"
        assert "GET <host> -> 200" in body, body

    def test_zip_does_not_mangle_the_log_when_the_pms_hostname_is_a_common_word(self, tmp_path: Path):
        """`plex` is the stock Docker Compose hostname. Replacing it as a bare substring rewrites the
        loguru source field, `com.plexapp`, `plex.tv` and `plexapi` — destroying the field that says
        which client logged the line, in the export whose whole purpose is being readable."""
        write_log(
            tmp_path,
            "2026-08-05 03:31:02.000 | DEBUG    | shortlist.engine.clients.plex_pms:_send:134 - "
            "GET plex -> 200; uri=com.plexapp.plugins.library; see plex.tv\n",
        )

        body = (
            zipfile.ZipFile(io.BytesIO(log_reader.build_zip(tmp_path, {"plex": "<host>"})))
            .read("logs/shortlist.log")
            .decode()
        )

        assert "clients.plex_pms:_send:134" in body, body
        assert "com.plexapp.plugins.library" in body
        assert "plex.tv" in body
        assert "GET <host> -> 200" in body, "the actual hostname must still be redacted"

    def test_an_unreadable_log_file_does_not_disclose_its_path(self, tmp_path: Path, monkeypatch):
        """`OSError.__str__` carries the absolute path — a home directory and an OS username on any
        non-Docker install. This branch was the one writer into the zip with no redaction at all."""
        write_log(tmp_path, LINE.format(level="INFO", message="hello"))
        monkeypatch.setattr(
            Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError("[Errno 13] denied: '/home/dave/x'"))
        )

        archive = zipfile.ZipFile(io.BytesIO(log_reader.build_zip(tmp_path)))
        body = archive.read("logs/shortlist.log.unreadable.txt").decode()

        assert "/home/dave" not in body, body
        assert "OSError" in body, "the failure must still be reported"
