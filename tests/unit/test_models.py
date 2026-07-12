from rowarr.engine.models import UserProfile, UserType, slugify


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
        assert profile.label == "rowarr_theden"

    def test_explicit_slug_wins(self):
        profile = UserProfile(username="TheDen", plex_account_id=1, user_type=UserType.SHARED, slug="den")
        assert profile.label == "rowarr_den"
