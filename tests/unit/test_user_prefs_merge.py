"""`PATCH /api/users/{id}` must tell "don't touch this field" apart from "clear this field".

Those two arrive over the wire as an ABSENT key and an explicit `null`, and collapsing them is a bug
shape this codebase has fixed everywhere else (`PATCH /collections`, `PUT …/rows` and `PUT /settings`
all read `model_fields_set`). The user-prefs merge was the one place still filtering on `is not None`.
"""

from __future__ import annotations

from pydantic import BaseModel

from shortlist.server.api.users import merged_prefs


class _PrefsWithADefault(BaseModel):
    """A prefs model whose field does NOT default to `None`.

    Every real `UserPrefs` field happens to default to `None` today, which is the only reason a
    `is not None` filter looked correct. This model is the shape the codebase is one field away from,
    and it is the whole point of the regression test below.
    """

    tone: str = "cheerful"
    note: str | None = None


class TestMergedPrefs:
    def test_a_field_the_request_omits_is_left_alone(self):
        stored = {"tone": "dry", "paused": True}

        merged = merged_prefs(stored, _PrefsWithADefault(note="hi"))

        assert merged["tone"] == "dry", "an unmentioned field must keep its stored value"
        assert merged["note"] == "hi"
        assert merged["paused"] is True, "keys the model knows nothing about survive untouched"

    def test_a_non_none_default_is_never_written_by_an_unrelated_patch(self):
        """The clobber. `model_dump()` renders `tone="cheerful"` whether or not the client sent it, so
        a `is not None` filter writes that default into every user on every unrelated PATCH."""
        stored = {"tone": "dry"}

        merged = merged_prefs(stored, _PrefsWithADefault(note="hi"))

        assert merged["tone"] == "dry", "the model's default must not overwrite what the user stored"

    def test_an_explicit_null_clears_the_stored_value(self):
        stored = {"tone": "dry", "note": "keep me?"}

        merged = merged_prefs(stored, _PrefsWithADefault(note=None))

        assert "note" not in merged, "an explicit null means clear, not 'leave alone'"
        assert merged["tone"] == "dry"

    def test_clearing_a_field_that_was_never_stored_is_not_an_error(self):
        assert merged_prefs({}, _PrefsWithADefault(note=None)) == {}

    def test_a_nested_model_is_stored_as_plain_json_not_as_a_model(self):
        """`prefs` is a JSON column. `blocked_seeds` accepts objects, so the merge has to hand
        SQLAlchemy dicts — reading the field off the model gives `BlockSeedBody` instances, which is
        not JSON and which nothing else in the app knows how to read back."""
        from shortlist.server.api.users import UserPrefs

        sent = UserPrefs.model_validate({"blocked_seeds": [{"tmdb_id": 42, "title": "Dune", "media_type": "movie"}]})

        merged = merged_prefs({}, sent)

        assert all(isinstance(entry, dict) for entry in merged["blocked_seeds"])
        # Exactly what the client sent, no invented keys: `exclude_unset` reaches the nested model
        # too, so an omitted `year` stays omitted. `prefs.blocked_entries` reads it with `.get`, so
        # an absent `year` and a stored `None` normalise to the same record either way.
        assert merged["blocked_seeds"] == [{"tmdb_id": 42, "title": "Dune", "media_type": "movie"}]

    def test_the_legacy_bare_int_shape_still_round_trips(self):
        """An old install's `blocked_seeds` is a list of bare ints, and that shape is accepted for
        ever — the union must not be normalised into objects on the way through."""
        from shortlist.server.api.users import UserPrefs

        merged = merged_prefs({}, UserPrefs.model_validate({"blocked_seeds": [111, 222]}))

        assert merged["blocked_seeds"] == [111, 222]

    def test_the_stored_mapping_is_not_mutated_in_place(self):
        """`user.prefs` is a JSON column: mutating the dict SQLAlchemy handed back can leave the
        session unaware anything changed, so the merge must build a new mapping."""
        stored = {"tone": "dry"}

        merged_prefs(stored, _PrefsWithADefault(tone="loud"))

        assert stored == {"tone": "dry"}
