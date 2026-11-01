"""`recency` is a signed dial, -1 (lean older) .. 1 (lean newer), globally and per row (#174)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from shortlist.server.services.row_editing import CollectionIn
from shortlist.server.services.settings_validation import validate_values


def _row(**extra: object) -> CollectionIn:
    return CollectionIn(name="Row", **extra)  # type: ignore[arg-type]


class TestGlobalRecency:
    @pytest.mark.parametrize("value", [-1, -0.5, 0, 0.5, 1])
    def test_the_whole_range_is_accepted(self, value: float) -> None:
        validate_values({"recommendations.recency": value})

    @pytest.mark.parametrize("value", [-1.01, -2, 1.01])
    def test_outside_the_range_is_rejected(self, value: float) -> None:
        with pytest.raises(HTTPException) as err:
            validate_values({"recommendations.recency": value})
        assert err.value.status_code == 422


class TestRowRecency:
    @pytest.mark.parametrize("value", [-1.0, -0.25, 0.0, 1.0])
    def test_the_whole_range_is_accepted(self, value: float) -> None:
        assert _row(recency=value).recency == value

    @pytest.mark.parametrize("value", [-1.01, 1.01])
    def test_outside_the_range_is_rejected(self, value: float) -> None:
        with pytest.raises(ValidationError):
            _row(recency=value)

    def test_unset_still_inherits(self) -> None:
        assert _row().recency is None
