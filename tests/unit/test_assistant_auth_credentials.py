from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shortlist.server.assistant_auth.credentials import CredentialHasher, SecretAlreadyTaken

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def test_issued_credential_is_returned_once_and_never_appears_in_repr() -> None:
    hasher = CredentialHasher(b"a" * 32)

    issued = hasher.issue("shla")
    raw = issued.take()

    assert raw.startswith("shla_")
    assert raw not in repr(issued)
    with pytest.raises(SecretAlreadyTaken):
        issued.take()


def test_digest_is_stable_for_lookup_but_does_not_contain_the_credential() -> None:
    hasher = CredentialHasher(b"b" * 32)
    issued = hasher.issue("shlo")
    raw = issued.take()

    digest = hasher.digest(raw)

    assert digest == hasher.digest(raw)
    assert raw not in digest
    assert hasher.matches(raw, digest)
    assert not hasher.matches(f"{raw}x", digest)


def test_expiry_helper_treats_naive_sqlite_timestamps_as_utc() -> None:
    from shortlist.server.assistant_auth.credentials import credential_is_active

    assert credential_is_active(NOW.replace(tzinfo=None) + timedelta(seconds=1), None, now=NOW)
    assert not credential_is_active(NOW.replace(tzinfo=None) - timedelta(seconds=1), None, now=NOW)
    assert not credential_is_active(NOW + timedelta(seconds=1), NOW, now=NOW)
