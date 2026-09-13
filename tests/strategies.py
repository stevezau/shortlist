"""Hypothesis strategies shared by the share-filter test modules."""

from __future__ import annotations

from hypothesis import strategies as st

from shortlist.engine.privacy import FilterCondition, serialize_filter

# Raw filter values. `,` `|` `=` `!` really are syntax and never appear inside a value — but `&` is
# NOT in that list: it is a separator in Plex Web's dialect and an ordinary character in a label
# ("Kids & Family"), so a filter using both at once is genuinely ambiguous and no parser can resolve
# it. `&`-in-value is therefore covered by explicit `|`-separated cases below rather than generated
# here, where hypothesis would happily build the ambiguous combination.
value = st.text(alphabet=st.sampled_from("abcdefgXYZ0123456789_%."), min_size=1, max_size=12)
field_name = st.sampled_from(["label", "contentRating", "genre", "year"])
# Both separators of each kind, because plex.tv stores whatever the last writer used and hands it
# straight back — live-verified 2026-08-10. Plex Web writes `&` with `%2C`, plexapi writes `|` with
# `%2C`, Shortlist writes `|` with `,`; a filter can be in any of those shapes when we read it.
condition_sep = st.sampled_from(["|", "&"])
value_sep = st.sampled_from([",", "%2C"])


@st.composite
def _condition(draw):
    values = tuple(draw(st.lists(value, min_size=1, max_size=4)))
    return FilterCondition(
        draw(field_name),
        draw(st.sampled_from(["=", "!="])),
        values,
        sep=draw(condition_sep),
        value_seps=tuple(draw(st.lists(value_sep, min_size=len(values) - 1, max_size=len(values) - 1))),
    )


condition = _condition()
filter_string = st.lists(condition, min_size=0, max_size=5).map(serialize_filter)
