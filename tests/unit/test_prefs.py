"""Readers for the JSON blobs on `users.prefs`.

`blocked_seeds` shipped as a bare `list[int]`, so the UI could only ever render "tmdb 346648" — a
number nobody recognises, which is most of why the feature went unused. Entries now carry the title.
An existing install's list is valid data and must keep working for ever, so both shapes are read.
"""

from __future__ import annotations

from shortlist.server.prefs import blocked_entries, blocked_ids


class TestBlockedSeeds:
    def test_reads_the_original_bare_int_shape(self):
        entries = blocked_entries({"blocked_seeds": [346648, 136315]})
        assert [e["tmdb_id"] for e in entries] == [346648, 136315]
        assert all(e["title"] == "" for e in entries), "an id carries no name — say so, don't invent one"
        assert blocked_ids({"blocked_seeds": [346648]}) == {346648}

    def test_reads_the_richer_record_shape(self):
        entries = blocked_entries(
            {"blocked_seeds": [{"tmdb_id": 1, "title": "Paddington 2", "media_type": "movie", "year": 2017}]}
        )
        assert entries == [{"tmdb_id": 1, "title": "Paddington 2", "media_type": "movie", "year": 2017}]

    def test_reads_a_list_holding_both_shapes(self):
        """Exactly what an install looks like the moment after the first title is blocked."""
        entries = blocked_entries({"blocked_seeds": [42, {"tmdb_id": 7, "title": "The Bear"}]})
        assert blocked_ids({"blocked_seeds": [42, {"tmdb_id": 7, "title": "The Bear"}]}) == {42, 7}
        assert entries[1]["title"] == "The Bear"
        assert entries[1]["media_type"] == ""  # absent, not guessed

    def test_ignores_entries_it_cannot_make_sense_of(self):
        """prefs is free-form JSON any client can PATCH — a malformed entry must not 500 the page."""
        assert blocked_ids({"blocked_seeds": ["nope", None, {}, {"title": "no id"}, True]}) == set()

    def test_no_prefs_at_all_is_simply_nothing_blocked(self):
        assert blocked_entries(None) == []
        assert blocked_ids({}) == set()
