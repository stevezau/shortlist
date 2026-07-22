from shortlist.engine.models import UserProfile, UserType, slugify


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
