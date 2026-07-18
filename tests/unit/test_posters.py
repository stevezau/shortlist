"""Row-poster tests: the engine's cosmetic apply step, text-field rendering, and the server-side
service (provider capability, the text/AI studio, image storage, generation cache)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.delivery import apply_poster, render_poster_text
from shortlist.engine.models import PosterSpec, UserProfile, UserType
from shortlist.server.db.models import Base
from shortlist.server.services.poster_service import (
    PosterStudio,
    clear_assets,
    image_provider_status,
    load_generated,
    load_upload,
    poster_seed,
    store_upload,
)


def _profile(name: str = "Alex") -> UserProfile:
    return UserProfile(username=name, plex_account_id=1, user_type=UserType.SHARED)


class TestApplyPoster:
    """Cosmetic apply step — must upload for real modes, skip safely otherwise, and never raise."""

    def test_upload_mode_uploads_the_stored_bytes(self):
        plex = MagicMock()
        apply_poster(
            plex,
            "COLL",
            PosterSpec(mode="upload", image=b"PNGDATA"),
            _profile(),
            [],
            library_name="Movies",
            artist=None,
            dry_run=False,
        )
        plex.upload_poster.assert_called_once_with("COLL", b"PNGDATA")

    def test_text_mode_renders_via_the_artist_then_uploads(self):
        plex = MagicMock()
        artist = MagicMock()
        artist.render.return_value = b"IMG"
        apply_poster(
            plex,
            "COLL",
            PosterSpec(mode="text", title="{user}'s picks", style="neon"),
            _profile("Sam"),
            [],
            library_name="Movies",
            artist=artist,
            dry_run=False,
        )
        # The engine renders placeholders and dispatches to the "text" engine.
        assert artist.render.call_args.kwargs == {
            "title": "Sam's picks",
            "subtitle": "",
            "style": "neon",
            "engine": "text",
        }
        plex.upload_poster.assert_called_once_with("COLL", b"IMG")

    def test_ai_mode_dispatches_to_the_ai_engine(self):
        plex = MagicMock()
        artist = MagicMock()
        artist.render.return_value = b"IMG"
        apply_poster(
            plex,
            "COLL",
            PosterSpec(mode="ai", title="Hi"),
            _profile(),
            [],
            library_name="Movies",
            artist=artist,
            dry_run=False,
        )
        assert artist.render.call_args.kwargs["engine"] == "ai"

    def test_legacy_generate_mode_still_maps_to_ai(self):
        plex = MagicMock()
        artist = MagicMock()
        artist.render.return_value = b"IMG"
        apply_poster(
            plex,
            "COLL",
            PosterSpec(mode="generate", title="Hi"),
            _profile(),
            [],
            library_name="Movies",
            artist=artist,
            dry_run=False,
        )
        assert artist.render.call_args.kwargs["engine"] == "ai"

    def test_no_artist_skips_rendering(self):
        plex = MagicMock()
        apply_poster(
            plex,
            "COLL",
            PosterSpec(mode="text", title="x"),
            _profile(),
            [],
            library_name="Movies",
            artist=None,
            dry_run=False,
        )
        plex.upload_poster.assert_not_called()

    def test_dry_run_never_writes_or_renders(self):
        plex, artist = MagicMock(), MagicMock()
        apply_poster(
            plex,
            "COLL",
            PosterSpec(mode="upload", image=b"x"),
            _profile(),
            [],
            library_name="Movies",
            artist=artist,
            dry_run=True,
        )
        plex.upload_poster.assert_not_called()
        artist.render.assert_not_called()

    @pytest.mark.parametrize("poster", [None, PosterSpec(mode="")])
    def test_no_poster_is_a_noop(self, poster):
        plex = MagicMock()
        apply_poster(plex, "COLL", poster, _profile(), [], library_name="Movies", artist=None, dry_run=False)
        plex.upload_poster.assert_not_called()

    def test_upload_failure_never_breaks_delivery(self):
        plex = MagicMock()
        plex.upload_poster.side_effect = RuntimeError("PMS down")
        apply_poster(  # must not raise — a poster is cosmetic
            plex,
            "COLL",
            PosterSpec(mode="upload", image=b"x"),
            _profile(),
            [],
            library_name="Movies",
            artist=None,
            dry_run=False,
        )

    def test_empty_rendered_image_leaves_plex_artwork(self):
        plex, artist = MagicMock(), MagicMock()
        artist.render.return_value = None
        apply_poster(
            plex,
            "COLL",
            PosterSpec(mode="text", title="x"),
            _profile(),
            [],
            library_name="Movies",
            artist=artist,
            dry_run=False,
        )
        plex.upload_poster.assert_not_called()


class TestRenderPosterText:
    def test_fills_user_and_library(self):
        assert render_poster_text("{user} in {library_name}", _profile("Jo"), [], "Movies") == "Jo in Movies"

    def test_blank_field_collapses_to_empty(self):
        assert render_poster_text("   ", _profile(), [], "Movies") == ""

    def test_top_seed_with_no_seed_is_dropped(self):
        assert render_poster_text("Because you watched {top_seed}", _profile(), [], "Movies") == ""


class TestImageProviderStatus:
    """Only OpenAI/Google with a key can generate AI images (the text engine always works)."""

    class _Store:
        def __init__(self, values: dict):
            self._values = values

        def get(self, key: str):
            return self._values.get(key)

    def test_anthropic_cannot(self):
        status = image_provider_status(self._Store({"curator.provider": "anthropic", "curator.api_key": "k"}))
        assert status["capable"] is False
        assert "OpenAI or Google" in status["reason"]

    def test_ollama_cannot(self):
        assert not image_provider_status(self._Store({"curator.provider": "ollama"}))["capable"]

    def test_no_provider_cannot(self):
        assert not image_provider_status(self._Store({"curator.provider": ""}))["capable"]

    def test_openai_needs_a_key(self):
        assert not image_provider_status(self._Store({"curator.provider": "openai", "curator.api_key": ""}))["capable"]

    def test_google_needs_a_key(self):
        assert not image_provider_status(self._Store({"curator.provider": "google", "curator.api_key": ""}))["capable"]

    def test_openai_with_key_is_capable(self):
        assert image_provider_status(self._Store({"curator.provider": "openai", "curator.api_key": "k"}))["capable"]

    def test_google_with_key_is_capable(self):
        assert image_provider_status(self._Store({"curator.provider": "google", "curator.api_key": "k"}))["capable"]


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


class TestImageStorage:
    def test_upload_roundtrip(self, sessions):
        with sessions() as session:
            store_upload(session, 5, b"IMG", "image/jpeg")
            session.commit()
        with sessions() as session:
            assert load_upload(session, 5) == (b"IMG", "image/jpeg")

    def test_missing_upload_is_none(self, sessions):
        with sessions() as session:
            assert load_upload(session, 999) is None

    def test_clear_removes_only_this_rows_upload(self, sessions):
        with sessions() as session:
            store_upload(session, 1, b"A", "image/png")
            store_upload(session, 2, b"B", "image/png")
            session.commit()
        with sessions() as session:
            clear_assets(session, 1)
            session.commit()
        with sessions() as session:
            assert load_upload(session, 1) is None
            assert load_upload(session, 2) == (b"B", "image/png")


class TestPosterStudio:
    """The studio dispatches by engine, caches by (engine, text, style), and returns None for AI with
    no provider."""

    def test_text_engine_caches_and_serves(self, sessions, monkeypatch):
        import shortlist.server.services.poster_service as svc

        renders = MagicMock(side_effect=[b"TEXTIMG"])
        monkeypatch.setattr(svc, "render_text_poster", renders)
        studio = PosterStudio(sessions, ai=None)
        assert studio.render(title="Hi", subtitle="", style="", engine="text") == b"TEXTIMG"
        assert studio.render(title="Hi", subtitle="", style="", engine="text") == b"TEXTIMG"
        renders.assert_called_once()  # second call hit the DB cache
        with sessions() as session:
            assert load_generated(session, poster_seed("text", "Hi", "", "")) == b"TEXTIMG"

    def test_ai_engine_without_provider_returns_none(self, sessions):
        studio = PosterStudio(sessions, ai=None)
        assert studio.render(title="Hi", subtitle="", style="", engine="ai") is None
        assert studio.ai_available is False

    def test_ai_engine_uses_the_injected_artist(self, sessions):
        ai = MagicMock()
        ai.render.return_value = b"AIIMG"
        studio = PosterStudio(sessions, ai=ai)
        assert studio.render(title="Hi", subtitle="Sub", style="noir", engine="ai") == b"AIIMG"
        assert ai.render.call_args.kwargs == {"title": "Hi", "subtitle": "Sub", "style": "noir", "engine": "ai"}


class TestProviderArtists:
    """The real render clients (SDKs mocked): assert the request kwargs the SUT controls and that the
    provider-specific response shape is decoded to bytes."""

    def test_openai_artist_sends_the_right_request_and_decodes_b64(self, monkeypatch):
        import base64
        import sys

        from shortlist.server.services.poster_service import OPENAI_IMAGE_MODEL, _OpenAIArtist

        fake_openai = MagicMock()
        client = fake_openai.OpenAI.return_value
        client.images.generate.return_value = MagicMock(
            data=[MagicMock(b64_json=base64.b64encode(b"IMGBYTES").decode())]
        )
        monkeypatch.setitem(sys.modules, "openai", fake_openai)

        out = _OpenAIArtist("sk-test").render(title="T", subtitle="S", style="noir", engine="ai")

        assert out == b"IMGBYTES"
        assert fake_openai.OpenAI.call_args.kwargs["api_key"] == "sk-test"
        call = client.images.generate.call_args
        assert call.kwargs["model"] == OPENAI_IMAGE_MODEL
        assert call.kwargs["size"] == "1024x1536"  # portrait 2:3
        assert call.kwargs["n"] == 1
        assert "T" in call.kwargs["prompt"] and "noir" in call.kwargs["prompt"]

    def test_openai_artist_returns_none_when_no_data(self, monkeypatch):
        import sys

        from shortlist.server.services.poster_service import _OpenAIArtist

        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value.images.generate.return_value = MagicMock(data=[])
        monkeypatch.setitem(sys.modules, "openai", fake_openai)
        assert _OpenAIArtist("sk").render(title="T", subtitle="", style="", engine="ai") is None

    def test_google_artist_sends_the_right_request_and_reads_bytes(self, monkeypatch):
        import sys

        from shortlist.server.services.poster_service import GOOGLE_IMAGE_MODEL, _GoogleArtist

        fake_genai = MagicMock()
        fake_genai.types = MagicMock()
        client = fake_genai.Client.return_value
        image = MagicMock()
        image.image.image_bytes = b"GIMG"
        client.models.generate_images.return_value = MagicMock(generated_images=[image])
        google_pkg = MagicMock()
        google_pkg.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

        out = _GoogleArtist("g-key").render(title="T", subtitle="", style="", engine="ai")

        assert out == b"GIMG"
        assert fake_genai.Client.call_args.kwargs["api_key"] == "g-key"
        call = client.models.generate_images.call_args
        assert call.kwargs["model"] == GOOGLE_IMAGE_MODEL
        assert fake_genai.types.GenerateImagesConfig.call_args.kwargs["aspect_ratio"] == "3:4"


class TestRenderTextPoster:
    def test_produces_a_portrait_png(self):
        pytest.importorskip("PIL")
        from shortlist.server.services.poster_service import render_text_poster

        image = render_text_poster("Weekend Picks", "Hand-picked for you", "vibrant")
        assert image is not None and image[:8] == b"\x89PNG\r\n\x1a\n"
        from PIL import Image

        with Image.open(__import__("io").BytesIO(image)) as img:
            assert img.size == (1000, 1500)  # 2:3 portrait

    def test_same_text_is_deterministic(self):
        pytest.importorskip("PIL")
        from shortlist.server.services.poster_service import render_text_poster

        assert render_text_poster("A", "B", "c") == render_text_poster("A", "B", "c")


class TestNormalizeUpload:
    def test_converts_and_downscales_with_pillow(self):
        pytest.importorskip("PIL")
        import io

        from PIL import Image

        from shortlist.server.services.poster_service import normalize_upload

        buf = io.BytesIO()
        Image.new("RGB", (4000, 6000), "navy").save(buf, format="PNG")
        image, content_type = normalize_upload(buf.getvalue())
        assert content_type == "image/jpeg"
        with Image.open(io.BytesIO(image)) as out:
            assert max(out.size) <= 1500

    def test_rejects_non_image_with_pillow(self):
        pytest.importorskip("PIL")
        from shortlist.server.services.poster_service import normalize_upload

        with pytest.raises(ValueError):
            normalize_upload(b"this is not an image")
