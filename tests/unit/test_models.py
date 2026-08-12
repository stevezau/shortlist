import pytest

from shortlist.engine.models import MAX_ROW_SIZE, EngineConfig, RowSpec, UserProfile, UserType, slugify
from shortlist.engine.rows import _KEEP_FRACTION


class TestSlugify:
    def test_lowercases_and_replaces_punctuation(self):
        assert slugify("Sarah O'Brien") == "sarah_o_brien"

    def test_strips_accents(self):
        assert slugify("José-André") == "jose_andre"

    def test_empty_or_symbol_only_falls_back(self):
        assert slugify("!!!") == "user"


class TestUserProfile:
    def test_slug_and_label_derived_from_username(self):
        profile = UserProfile(username="TheDen", plex_account_id=1, user_type=UserType.SHARED)
        assert profile.slug == "theden"
        assert profile.label == "shortlist_theden"

    def test_explicit_slug_wins(self):
        profile = UserProfile(username="TheDen", plex_account_id=1, user_type=UserType.SHARED, slug="den")
        assert profile.label == "shortlist_den"


class TestDisplayName:
    """`{user}` renders the nickname, but the SLUG — and so the privacy label — never moves."""

    def test_nickname_wins_over_the_plex_username(self):
        profile = UserProfile(username="mrjohnpoz", plex_account_id=1, user_type=UserType.SHARED, nickname="John")
        assert profile.display_name == "John"

    def test_falls_back_to_the_plex_username(self):
        assert UserProfile(username="mrjohnpoz", plex_account_id=1, user_type=UserType.SHARED).display_name == (
            "mrjohnpoz"
        )

    def test_a_blank_or_spaces_only_nickname_is_not_a_name(self):
        profile = UserProfile(username="mrjohnpoz", plex_account_id=1, user_type=UserType.SHARED, nickname="   ")
        assert profile.display_name == "mrjohnpoz"

    def test_a_nickname_never_moves_the_label(self):
        """The label is what every other account's share filter excludes. If a rename moved it, the
        old exclusions would point at nothing and the row would be visible to everyone."""
        plain = UserProfile(username="mrjohnpoz", plex_account_id=1, user_type=UserType.SHARED)
        renamed = UserProfile(username="mrjohnpoz", plex_account_id=1, user_type=UserType.SHARED, nickname="John")
        assert renamed.label == plain.label == "shortlist_mrjohnpoz"
        assert renamed.slug == plain.slug


class TestRowSpecPlacement:
    """Placement decodes to Plex's three flags, split by whose collection it is.

    Full matrix per audience: both / home / library / off, plus the inherit path
    (`placement_friends=None`) that keeps pre-0042 rows behaving as they did.
    """

    def _spec(self, placement: str = "both", placement_friends: str | None = None) -> RowSpec:
        return RowSpec(
            slug="x",
            name_template="",
            size=10,
            placement=placement,
            placement_friends=placement_friends,
        )

    @pytest.mark.parametrize(
        ("placement", "home", "library"),
        [
            ("both", True, True),
            ("home", True, False),
            ("library", False, True),
            ("off", False, False),
        ],
    )
    def test_the_owner_side_decodes_to_its_two_flags(self, placement: str, home: bool, library: bool):
        spec = self._spec(placement, placement_friends="off")
        assert spec.show_home is home
        assert spec.show_owner_library is library

    @pytest.mark.parametrize(
        ("placement_friends", "home", "library"),
        [
            ("both", True, True),
            ("home", True, False),
            ("library", False, True),
            ("off", False, False),
        ],
    )
    def test_the_friends_side_decodes_to_its_two_flags(self, placement_friends: str, home: bool, library: bool):
        spec = self._spec("off", placement_friends)
        assert spec.show_friends_home is home
        assert spec.show_friends_library is library

    def test_the_two_sides_are_independent(self):
        """The whole point of issue #6: the owner can keep their own row on the Recommended shelf
        while friends' rows stay off it, and vice versa."""
        owner_only = self._spec("library", "home")
        assert owner_only.show_owner_library and not owner_only.show_friends_library

        friends_only = self._spec("home", "library")
        assert friends_only.show_friends_library and not friends_only.show_owner_library

    def test_a_null_friends_placement_inherits_the_owner_side(self):
        """Pre-0042 rows carry no friends placement; they must keep behaving exactly as before."""
        spec = self._spec("library", placement_friends=None)
        assert spec.show_friends_library is True
        assert spec.show_friends_home is False

    def test_show_library_unions_both_sides_for_a_shared_row(self):
        """A shared row is ONE public collection, so there is no per-person split to make — either
        side asking for the shelf puts it there."""
        assert self._spec("library", "off").show_library is True
        assert self._spec("off", "library").show_library is True
        assert self._spec("off", "off").show_library is False


class TestPoolClearsTheRowCeiling:
    """`candidates_pre_rank` vs `MAX_ROW_SIZE` — the invariant that used to be a comment.

    The pool cap was a flat 40 while `row.size` validated up to 40, so a row at the top of its legal
    range was the same size as the pool it was drawn from: every surviving candidate had to go in, and
    a refresh night had nothing spare to swap the weakest third for. Two numbers that had to agree,
    coupled only by a comment in `api/settings.py`.
    """

    def test_the_pool_leaves_room_for_a_refresh_at_the_largest_legal_row(self):
        """A refresh keeps ~2/3 of the row and must find the rest among candidates NOT already in it.
        At the ceiling that needs pool > row, with slack — equality is the bug this pins."""
        pool = EngineConfig().candidates_pre_rank
        assert pool > MAX_ROW_SIZE, f"pool {pool} must exceed the largest legal row {MAX_ROW_SIZE}"
        spare = pool - MAX_ROW_SIZE
        needed = MAX_ROW_SIZE - round(_KEEP_FRACTION * MAX_ROW_SIZE)  # the weakest third, swapped out
        assert spare >= needed, f"{spare} spare candidates cannot refill {needed} slots on a refresh"

    def test_every_size_validator_uses_the_one_ceiling(self):
        """Three validators bound a row's size. Each must read MAX_ROW_SIZE, not restate the number —
        restating it is exactly how the pool cap drifted into matching it."""
        from shortlist.server.api.collections import CollectionIn
        from shortlist.server.api.settings import VALIDATORS
        from shortlist.server.api.user_rows import RowOverridePatch

        assert VALIDATORS["row.size"](MAX_ROW_SIZE) is None
        assert VALIDATORS["row.size"](MAX_ROW_SIZE + 1) is not None
        for model, field in ((CollectionIn, "size"), (RowOverridePatch, "row_size")):
            meta = model.model_fields[field].metadata
            ceiling = next(m.le for m in meta if hasattr(m, "le"))
            assert ceiling == MAX_ROW_SIZE, f"{model.__name__}.{field} restates {ceiling}"
