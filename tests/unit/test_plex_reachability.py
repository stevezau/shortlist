"""Why the saved Plex address did not answer — in words an owner can act on (issue #139)."""

from __future__ import annotations

import httpx
import pytest
import requests

from shortlist.server.services import plex_reachability
from shortlist.server.services.plex_reachability import (
    PlexUnreachable,
    address_kind,
    error_text,
    explain_unreachable,
    explained,
    failure_reason,
)

MACHINE = "a" * 32
INTERNET = f"https://8-8-8-8.{MACHINE}.plex.direct:32400"
LOCAL_DIRECT = f"https://192-168-1-50.{MACHINE}.plex.direct:32400"


def _refused(url: str) -> requests.exceptions.ConnectionError:
    """The text is #139's own, host and port aside — what requests raises for a refused connection."""
    parts = httpx.URL(url)
    pool = f"HTTPSConnection(host='{parts.host}', port={parts.port})"
    return requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='{parts.host}', port={parts.port}): Max retries exceeded with url: / "
        f'(Caused by NewConnectionError("{pool}: Failed to establish a new connection: '
        '[Errno 111] Connection refused"))',
        request=requests.Request("GET", url).prepare(),
    )


@pytest.fixture
def in_container(monkeypatch):
    monkeypatch.setattr(plex_reachability, "in_container", lambda: True)


@pytest.fixture
def not_in_container(monkeypatch):
    monkeypatch.setattr(plex_reachability, "in_container", lambda: False)


class TestAddressKind:
    @pytest.mark.parametrize(
        ("url", "kind"),
        [
            ("https://localhost:34000", "loopback"),
            ("http://127.0.0.1:32400", "loopback"),
            ("http://[::1]:32400", "loopback"),
            (INTERNET, "plex.direct (internet)"),
            (LOCAL_DIRECT, "plex.direct (local)"),
            (f"https://relay.{MACHINE}.plex.direct:8443", "plex.direct"),
            ("http://192.168.1.10:32400", "private IP"),
            ("http://8.8.8.8:32400", "public IP"),
            ("http://plex:32400", "hostname"),
            ("http://host.docker.internal:32400", "hostname"),
        ],
    )
    def test_names_what_the_address_is(self, url, kind):
        assert address_kind(url) == kind


class TestFailureReason:
    def test_a_refused_connection(self):
        assert failure_reason(_refused("https://localhost:34000")) == "refused"

    def test_a_name_that_does_not_resolve(self):
        error = requests.exceptions.ConnectionError(
            "HTTPConnectionPool(host='plex', port=32400): Max retries exceeded with url: / (Caused by "
            "NameResolutionError(\"Failed to resolve 'plex' ([Errno -2] Name or service not known)\"))"
        )
        assert failure_reason(error) == "dns"

    @pytest.mark.parametrize(
        "error",
        [requests.exceptions.ConnectTimeout("timed out"), requests.exceptions.ReadTimeout("timed out")],
    )
    def test_no_answer_in_time(self, error):
        assert failure_reason(error) == "timeout"

    def test_a_certificate_plex_did_not_issue_for_that_name(self):
        assert failure_reason(requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")) == "tls"

    def test_any_other_connection_failure(self):
        assert failure_reason(requests.exceptions.ConnectionError("Connection aborted.")) == "unreachable"

    def test_the_setup_probes_httpx_failures_are_read_the_same_way(self):
        # `/api/setup/servers` probes with httpx, not requests — one classifier serves both logs.
        assert failure_reason(httpx.ConnectError("[Errno 111] Connection refused")) == "refused"
        assert failure_reason(httpx.ConnectTimeout("timed out")) == "timeout"

    def test_an_error_that_is_not_about_the_connection_is_not_ours(self):
        assert failure_reason(RuntimeError("401 unauthorized")) is None


class TestExplainUnreachable:
    def test_localhost_in_a_container_says_it_is_the_container(self, in_container):
        message = explain_unreachable("https://localhost:34000", _refused("https://localhost:34000"))

        assert "https://localhost:34000" in message
        assert "container itself" in message
        assert "http://host.docker.internal:32400" in message

    def test_localhost_outside_a_container_gets_no_container_advice(self, not_in_container):
        message = explain_unreachable("http://localhost:32400", _refused("http://localhost:32400"))

        assert "container" not in message
        assert "refused" in message

    def test_an_internet_plex_direct_address_says_it_depends_on_the_public_ip(self, not_in_container):
        message = explain_unreachable(INTERNET, _refused(INTERNET))

        assert "internet address" in message
        assert "8.8.8.8" in message
        assert "host.docker.internal" not in message

    def test_an_internet_address_in_a_container_also_offers_the_docker_host_name(self, in_container):
        assert "http://host.docker.internal:32400" in explain_unreachable(INTERNET, _refused(INTERNET))

    def test_any_other_address_says_why_and_what_to_check(self, not_in_container):
        message = explain_unreachable("http://192.168.1.10:32400", requests.exceptions.ConnectTimeout("timed out"))

        assert "http://192.168.1.10:32400" in message
        assert "did not answer in time" in message
        assert "Check that Plex is running" in message

    def test_a_refused_certificate_suggests_plain_http(self, not_in_container):
        message = explain_unreachable(
            "https://192.168.1.10:32400", requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
        )

        assert "certificate" in message
        assert "http://" in message

    def test_the_python_error_never_reaches_the_owner(self, in_container):
        message = explain_unreachable("https://localhost:34000", _refused("https://localhost:34000"))

        assert "HTTPSConnectionPool" not in message
        assert "Errno" not in message

    def test_a_password_in_the_address_is_not_repeated(self, not_in_container):
        url = "http://steve:hunter2@192.168.1.10:32400"

        assert "hunter2" not in explain_unreachable(url, requests.exceptions.ConnectTimeout("timed out"))

    def test_an_error_that_is_not_about_the_connection_is_left_alone(self):
        assert explain_unreachable("http://192.168.1.10:32400", RuntimeError("401 unauthorized")) is None

    def test_a_failure_reaching_some_other_service_is_not_blamed_on_plex(self):
        # A run talks to TMDB, plex.tv and an LLM too; only a request to the Plex address is Plex's.
        assert explain_unreachable("http://192.168.1.10:32400", _refused("https://api.themoviedb.org/3")) is None


class TestExplained:
    def test_an_unreachable_server_is_raised_as_the_explanation(self, not_in_container):
        cause = _refused(INTERNET)

        with pytest.raises(PlexUnreachable) as raised, explained(INTERNET, fix_hint=" Change it in Settings."):
            raise cause

        assert str(raised.value).startswith("Shortlist could not reach Plex at")
        assert str(raised.value).endswith(" Change it in Settings.")
        assert raised.value.__cause__ is cause

    def test_a_saved_address_that_redirects_names_both_addresses(self, not_in_container):
        # Measured with the real client: an address that answers 302 fails at the TARGET, so the
        # error names a host the owner never typed — #139's reporter read that as "Shortlist changed
        # my address". Inside `explained` the call was to Plex alone, so another host IS a redirect.
        saved = "http://192.168.1.171:32400"

        with pytest.raises(PlexUnreachable) as raised, explained(saved):
            raise _refused(INTERNET)

        message = str(raised.value)
        assert f"{saved} sent Shortlist on to {INTERNET}" in message
        assert "the connection was refused" in message
        assert "internet address" in message
        assert "8.8.8.8" in message

    def test_a_redirect_to_an_ordinary_address_says_only_that_it_failed(self, not_in_container):
        with pytest.raises(PlexUnreachable) as raised, explained("http://plex:32400"):
            raise _refused("https://192.168.1.10:32400")

        message = str(raised.value)
        assert "http://plex:32400 sent Shortlist on to https://192.168.1.10:32400" in message
        assert "internet address" not in message

    def test_any_other_error_passes_through_untouched(self):
        with pytest.raises(RuntimeError, match="401 unauthorized"), explained(INTERNET):
            raise RuntimeError("401 unauthorized")


class TestErrorText:
    def test_an_explanation_is_shown_as_written(self):
        assert error_text(PlexUnreachable("Shortlist could not reach Plex.")) == "Shortlist could not reach Plex."

    def test_anything_else_keeps_its_type_and_loses_its_secrets(self):
        text = error_text(RuntimeError("GET https://plex/library?X-Plex-Token=abcdefghij0123456789 failed"))

        assert text.startswith("RuntimeError: GET https://plex/library")
        assert "abcdefghij0123456789" not in text
