"""Row-poster service: text/AI rendering, image storage, and provider capability checks.

A poster is cosmetic — it only ever changes the artwork of a collection Shortlist owns, never a share
filter or promotion. Two render engines share one ``PosterStudio``:

- **text** — a built-in Pillow renderer (title/subtitle over a gradient). No API key, works for every
  provider, instant and free. The default.
- **ai** — an image model (OpenAI images / Google Imagen) reusing the AI curator's provider/key. An
  optional upgrade; unavailable providers (Anthropic, Ollama) fall back to nothing (the caller then
  leaves Plex's own artwork).

Uploaded originals and rendered images live in the ``poster_assets`` table (under /config) so they
survive a container recreate and a config backup carries them. Rendered images are cached by
(engine, text, style) so an identical poster is produced once, not every night per person.
"""

from __future__ import annotations

import base64
import colorsys
import hashlib
import io

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.clients.poster import PosterArtist
from shortlist.server.db.models import PosterAsset
from shortlist.server.settings_store import SettingsStore

# Curator providers that can generate images (so ai-engine posters reuse the curator key).
IMAGE_PROVIDERS = ("openai", "google")
OPENAI_IMAGE_MODEL = "gpt-image-1"
GOOGLE_IMAGE_MODEL = "imagen-3.0-generate-002"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # a poster is a poster; reject anything absurd before it hits the DB
# A poster is portrait (Plex artwork is ~2:3). OpenAI takes an explicit pixel size; Imagen an aspect ratio.
_OPENAI_SIZE = "1024x1536"
_GOOGLE_ASPECT = "3:4"
_POSTER_W, _POSTER_H = 1000, 1500  # 2:3 portrait for the built-in text renderer


def image_provider_status(store: SettingsStore) -> dict:
    """Whether the configured AI curator provider can generate poster images, and why not if it can't.

    Returns ``{"capable": bool, "provider": str, "reason": str}``. Only relevant to the *AI* engine —
    the built-in text engine always works. ``reason`` is a plain-English, user-facing sentence.
    """
    provider = (store.get("curator.provider") or "").strip()
    if provider not in IMAGE_PROVIDERS:
        label = provider or "none"
        return {
            "capable": False,
            "provider": provider,
            "reason": (
                f"Your AI provider ({label}) can't create images. Switch your AI curator to OpenAI or "
                "Google in Settings -> Curation to use AI artwork — or use a built-in text poster instead."
            ),
        }
    if not store.get("curator.api_key"):
        return {
            "capable": False,
            "provider": provider,
            "reason": f"Add your {provider} API key in Settings -> Curation to use AI artwork.",
        }
    return {"capable": True, "provider": provider, "reason": ""}


def make_studio(store: SettingsStore, sessions: sessionmaker[Session]) -> PosterStudio:
    """Build the poster studio: the built-in text renderer always, plus the AI engine when the curator
    provider can generate images. Rendered images are cached in the DB by (engine, text, style)."""
    ai: PosterArtist | None = None
    if image_provider_status(store)["capable"]:
        key = store.get("curator.api_key")
        ai = _OpenAIArtist(key) if store.get("curator.provider") == "openai" else _GoogleArtist(key)
    return PosterStudio(sessions, ai)


class PosterStudio:
    """Renders a poster by ``engine`` ("text" | "ai"), caching each result by its (engine, text, style)."""

    def __init__(self, sessions: sessionmaker[Session], ai: PosterArtist | None):
        self._sessions = sessions
        self._ai = ai

    @property
    def ai_available(self) -> bool:
        return self._ai is not None

    def render(self, *, title: str, subtitle: str, style: str, engine: str) -> bytes | None:
        seed = poster_seed(engine if engine == "ai" else "text", title, subtitle, style)
        with self._sessions() as session:
            cached = load_generated(session, seed)
            if cached is not None:
                return cached
        if engine == "ai":
            if self._ai is None:
                return None
            image = self._ai.render(title=title, subtitle=subtitle, style=style, engine="ai")
        else:
            image = render_text_poster(title, subtitle, style)
        if image:
            with self._sessions() as session:
                _put_asset(session, _gen_key(seed), image, "image/png")
                session.commit()
        return image


def ai_image_prompt(title: str, subtitle: str, style: str) -> str:
    """Compose the text-to-image prompt the AI engine sends from the (already-rendered) poster text."""
    parts = ["A vertical movie/TV streaming collection poster, 2:3 portrait, cinematic, high quality."]
    if title.strip():
        parts.append(f'Show the title text exactly: "{title.strip()}".')
    if subtitle.strip():
        parts.append(f'Show smaller subtitle text exactly: "{subtitle.strip()}".')
    if style.strip():
        parts.append(f"Art style: {style.strip()}.")
    parts.append("Any text must be spelled correctly and clearly legible. No watermarks, no borders.")
    return " ".join(parts)


class _OpenAIArtist:
    """OpenAI Images (gpt-image-1). Always returns base64 in ``data[0].b64_json``."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    def render(self, *, title: str, subtitle: str, style: str, engine: str) -> bytes | None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - image extra missing from the runtime
            raise ImportError("OpenAI image generation needs `pip install shortlist[openai]`") from exc
        client = openai.OpenAI(api_key=self._api_key, timeout=120.0, max_retries=2)
        resp = client.images.generate(
            model=OPENAI_IMAGE_MODEL, prompt=ai_image_prompt(title, subtitle, style), n=1, size=_OPENAI_SIZE
        )
        b64 = resp.data[0].b64_json if resp.data else None
        return base64.b64decode(b64) if b64 else None


class _GoogleArtist:
    """Google Imagen via google-genai. Bytes live in ``generated_images[0].image.image_bytes``."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    def render(self, *, title: str, subtitle: str, style: str, engine: str) -> bytes | None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - image extra missing from the runtime
            raise ImportError("Google image generation needs `pip install shortlist[google]`") from exc
        client = genai.Client(api_key=self._api_key)
        resp = client.models.generate_images(
            model=GOOGLE_IMAGE_MODEL,
            prompt=ai_image_prompt(title, subtitle, style),
            config=types.GenerateImagesConfig(
                number_of_images=1, aspect_ratio=_GOOGLE_ASPECT, output_mime_type="image/jpeg"
            ),
        )
        images = getattr(resp, "generated_images", None) or []
        return images[0].image.image_bytes if images else None


def render_text_poster(title: str, subtitle: str, style: str) -> bytes | None:
    """Render a clean text poster (title/subtitle over a gradient) with Pillow — no AI, no key.

    Colours are derived deterministically from the text + style, so a given poster looks the same
    every time and different posters look different. Returns PNG bytes, or None if Pillow is absent.
    """
    try:
        from PIL import ImageDraw, ImageFont
    except ImportError:  # pragma: no cover - posters extra missing from the runtime
        logger.warning("Pillow not installed — cannot render a text poster (install shortlist[posters])")
        return None

    top, bottom = _palette(f"{title}|{subtitle}|{style}")
    image = _gradient(_POSTER_W, _POSTER_H, top, bottom)
    draw = ImageDraw.Draw(image)
    margin = 90
    max_w = _POSTER_W - 2 * margin

    title_font = ImageFont.load_default(size=96)
    subtitle_font = ImageFont.load_default(size=46)
    title_lines = _wrap(draw, title.strip() or "Picked for You", title_font, max_w)
    subtitle_lines = _wrap(draw, subtitle.strip(), subtitle_font, max_w) if subtitle.strip() else []

    def _line_h(font) -> int:
        box = draw.textbbox((0, 0), "Ag", font=font)
        return int((box[3] - box[1]) * 1.35)

    title_h, sub_h = _line_h(title_font), _line_h(subtitle_font)
    block_h = len(title_lines) * title_h + (len(subtitle_lines) * sub_h + 30 if subtitle_lines else 0)
    y = int(_POSTER_H * 0.62) - block_h // 2  # sit the text in the lower-middle, like a movie poster

    # A subtle accent bar above the title.
    draw.rectangle([margin, y - 34, margin + 120, y - 26], fill=(255, 255, 255))
    for line in title_lines:
        _centered_line(draw, line, title_font, y, _POSTER_W, (245, 245, 250))
        y += title_h
    if subtitle_lines:
        y += 30
        for line in subtitle_lines:
            _centered_line(draw, line, subtitle_font, y, _POSTER_W, (205, 205, 215))
            y += sub_h

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _palette(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Two dark, cinematic gradient colours derived deterministically from ``seed``."""
    hue = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % 360
    top = tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue / 360, 0.24, 0.55))
    bottom = tuple(int(c * 255) for c in colorsys.hls_to_rgb(((hue + 35) % 360) / 360, 0.07, 0.5))
    return top, bottom  # type: ignore[return-value]


def _gradient(w: int, h: int, top: tuple, bottom: tuple):
    from PIL import Image

    strip = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        strip.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return strip.resize((w, h))


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _centered_line(draw, text: str, font, y: int, width: int, fill: tuple) -> None:
    x = (width - draw.textlength(text, font=font)) / 2
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0))  # shadow for legibility
    draw.text((x, y), text, font=font, fill=fill)


# ---- storage + cache -------------------------------------------------------------------------------


def poster_seed(engine: str, title: str, subtitle: str, style: str) -> str:
    """The cache seed for a rendered poster — same inputs, same image, across users and runs."""
    return f"{engine}|{title}|{subtitle}|{style}"


def _gen_key(seed: str) -> str:
    return "gen:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


def _upload_key(collection_id: int) -> str:
    return f"upload:{collection_id}"


def _put_asset(session: Session, key: str, image: bytes, content_type: str) -> None:
    asset = session.get(PosterAsset, key)
    if asset is None:
        session.add(PosterAsset(key=key, image=image, content_type=content_type))
    else:
        asset.image = image
        asset.content_type = content_type


def store_upload(session: Session, collection_id: int, image: bytes, content_type: str) -> None:
    """Persist a user-uploaded poster image for a row (caller commits)."""
    _put_asset(session, _upload_key(collection_id), image, content_type)


def load_upload(session: Session, collection_id: int) -> tuple[bytes, str] | None:
    """The stored uploaded image for a row as (bytes, content_type), or None if none was uploaded."""
    asset = session.get(PosterAsset, _upload_key(collection_id))
    return (asset.image, asset.content_type) if asset is not None else None


def load_generated(session: Session, seed: str) -> bytes | None:
    """The cached rendered image for a (engine, text, style) ``seed`` (prior render), or None."""
    asset = session.get(PosterAsset, _gen_key(seed))
    return asset.image if asset is not None else None


def clear_assets(session: Session, collection_id: int) -> None:
    """Drop a row's uploaded image (caller commits). Generated images are shared by seed, so left."""
    asset = session.get(PosterAsset, _upload_key(collection_id))
    if asset is not None:
        session.delete(asset)


def normalize_upload(raw: bytes) -> tuple[bytes, str]:
    """Validate + downscale an uploaded image to a sane portrait poster (JPEG).

    Uses Pillow when available: rejects non-images, flattens to RGB, and caps the longest side so a
    40-megapixel phone photo doesn't bloat the DB. If Pillow is missing it stores the bytes as-is —
    a poster is cosmetic, so a best-effort store beats a hard failure.

    Raises:
        ValueError: the bytes aren't a decodable image (only when Pillow is available to tell).
    """
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - posters extra missing from the runtime
        return raw, "image/png"
    try:
        image = Image.open(io.BytesIO(raw))
        image = image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("that file isn't an image we can read") from exc
    image.thumbnail((1000, 1500))  # keep aspect; poster-sized ceiling
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=88)
    logger.debug("normalized uploaded poster to {} bytes JPEG", out.tell())
    return out.getvalue(), "image/jpeg"


# ---- preview (editor) ------------------------------------------------------------------------------

# Sample values the editor preview substitutes for {user}/{library_name} so the owner sees a real poster.
_SAMPLE_USER = "Alex"
_SAMPLE_LIBRARY = "Movies"


def _sample_text(field_value: str) -> str:
    from shortlist.engine.delivery import render_poster_text
    from shortlist.engine.models import UserProfile, UserType

    who = UserProfile(username=_SAMPLE_USER, plex_account_id=0, user_type=UserType.SHARED)
    return render_poster_text(field_value, who, [], _SAMPLE_LIBRARY)


def preview_engine(mode: str) -> str:
    """Map a poster mode to a render engine ("text" | "ai"); legacy "generate" is "ai"."""
    return "ai" if mode in ("ai", "generate") else "text"


def preview_poster(studio: PosterStudio, mode: str, title: str, subtitle: str, style: str) -> bytes | None:
    """Render (or serve cached) a preview for the editor, with sample placeholder values filled in."""
    return studio.render(
        title=_sample_text(title), subtitle=_sample_text(subtitle), style=style, engine=preview_engine(mode)
    )


def load_preview(session: Session, mode: str, title: str, subtitle: str, style: str) -> bytes | None:
    """The cached preview image for a generate-mode row (from a prior preview/run), or None."""
    engine = preview_engine(mode)
    return load_generated(session, poster_seed(engine, _sample_text(title), _sample_text(subtitle), style))
