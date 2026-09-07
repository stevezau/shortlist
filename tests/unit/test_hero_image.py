"""The docs hero: the maintainer's own Plex shelf, with Plex's watched badges taken off it.

`docs/images/plex-picked-for-you.jpg` is the first thing a visitor sees, on the site and in the
README. It is derived from `tests/e2e/assets/plex-shelf-source.jpg` — a real screenshot of a real
"Picked for You" row — rather than from the fake harness, because the harness's honest gradient
placeholders read as an unfinished mock-up on exactly the image that has to look finished. The
owner's words: "they do not show actual movie posters etc so it looks a bit odd."

What the source cannot ship as: four of its eight posters carry Plex's watched tick, so the row
Shortlist exists to fill was advertising films the viewer had already seen. Three are repaired,
because the badge sits on artwork. The fourth is not, and is dropped rather than invented.

This is a plain unit test on purpose. It drives no browser and no Plex, so it does not belong under
`tests/e2e/` behind that suite's built-SPA gate — and being unconditional means CI re-runs the
badge check on every commit, not only when someone regenerates the picture. Regenerate with:

    SHOTS_DIR=docs/images .venv/bin/python -m pytest tests/unit/test_hero_image.py --no-cov

Its siblings — `two-account.png` and `social-preview.png` — are captured by
`tests/e2e/test_marketing_assets.py`, which does need a browser.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

SOURCE = Path(__file__).resolve().parents[1] / "e2e" / "assets" / "plex-shelf-source.jpg"

#: Geometry of `assets/plex-shelf-source.jpg`, measured off the file rather than guessed: tiles sit
#: on a 213px pitch from x=18, each poster 174x260 (2:3) starting at y=74. Plex's watched badge is a
#: rounded square pinned to a poster's top-right corner, 45x46 from x=129.
SHELF_PITCH, SHELF_X0, SHELF_Y0, POSTER_W = 213, 18, 74, 174
BADGE_X0, BADGE_H = 129, 46
#: Width of the badge-free strip beside the badge that the repair takes its colour from.
BADGE_SRC = 30
#: The checkmark glyph inside the badge, poster-local, and what counts as it being there.
GLYPH_X0, GLYPH_X1, GLYPH_Y0, GLYPH_Y1 = 141, 163, 13, 30
GLYPH_WHITE, GLYPH_MIN_PIXELS = 205, 30
#: Above this, the corner is bright artwork rather than the badge's dark plate.
PLATE_MAX_MEDIAN = 90
#: Slots carrying a watched badge in the source, left to right.
WATCHED_SLOTS = (0, 1, 2, 6)
#: Slot 6 is Schindler's List, and its badge sits on the title lettering rather than on artwork, so
#: nothing can be put back that is not invented. It is dropped instead: the shelf is cut after slot
#: 5 and the clipped final poster slid into its place, which also keeps the "this row continues"
#: cue the source had at its right edge.
DROP_SLOT, LAST_SLOT = 6, 7


def _row_colours(strip: Image.Image, width: int) -> Image.Image:
    """`strip` reduced to one averaged colour per row, stretched back out to `width`.

    A BOX downscale to a single column IS the per-row mean, which is why this needs no array
    library — and it carries no horizontal structure, so nothing recognisable can be copied.
    """
    return strip.resize((1, strip.height), Image.BOX).resize((width, strip.height), Image.BILINEAR)


def _grain(size: tuple[int, int], sigma: float, rng: random.Random) -> Image.Image:
    """Gaussian grain centred on 128, from a SEEDED generator.

    `Image.effect_noise` would do this in one call but seeds itself, and a capture that is not
    byte-identical on a re-run churns the repo for no change.
    """
    width, height = size
    data = bytes(max(0, min(255, int(rng.gauss(128, sigma)))) for _ in range(width * height))
    return Image.frombytes("L", size, data).convert("RGB")


def _unwatch(shelf: Image.Image, slot: int, rng: random.Random) -> None:
    """Take Plex's watched badge off one poster, in place.

    Four earlier attempts, each failing differently — recorded because the next person will try them
    in the same order. Mirroring the strip beside the badge duplicated recognisable detail (Brando's
    hair, the Dark Knight's towers). Blurring a region that CONTAINED the badge smeared the badge's
    own darkness across the corner. A flat per-row colour matched the hue but read as a blemish
    against Shawshank's rain. Copying the patch below the badge gave Shawshank its texture back and
    lifted the Dark Knight's flames up into the sky.

    What holds for all of them: the colour comes from the art BESIDE the badge, one value per row,
    so no structure can be carried in; the grain is synthesised at the amplitude that art actually
    has, so a rainy poster gets rain-ish noise and a black one gets almost none.
    """
    px = SHELF_X0 + slot * SHELF_PITCH
    box = (px + BADGE_X0 - BADGE_SRC, SHELF_Y0, px + POSTER_W, SHELF_Y0 + BADGE_H + 18)
    region = shelf.crop(box)
    beside = region.crop((0, 0, BADGE_SRC, region.height))

    # How grainy the art is right here: what is left of it once its own per-row colour is removed.
    # A smooth gradient contributes nothing; rain contributes a lot.
    residual = ImageChops.difference(beside, _row_colours(beside, BADGE_SRC))
    sigma = min(26.0, 1.25 * max(ImageStat.Stat(residual).rms))

    flat = _row_colours(beside, region.width)
    patch = ImageChops.add(flat, _grain(region.size, sigma, rng), offset=-128)
    patch = patch.filter(ImageFilter.GaussianBlur(0.6))

    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((BADGE_SRC - 2, -10, region.width + 10, BADGE_H), radius=12, fill=255)
    region.paste(patch, (0, 0), mask.filter(ImageFilter.GaussianBlur(5)))
    shelf.paste(region, box[:2])


def _has_badge(shelf: Image.Image, slot: int) -> bool:
    """Whether Plex's watched badge is on that poster.

    Two conditions, because neither alone works on this shelf. "Bright pixels in the corner" calls
    12 Angry Men watched — it is a near-white poster, and its corner has more white in it than any
    real badge. "Dark corner" calls The Godfather Part II watched, whose corner is simply black art.
    The badge is a white glyph ON a dark plate, so both have to hold at once, and measured over these
    eight posters the two tests separate 46-60 glyph pixels from 0-13, and a plate median of 0-47
    from artwork's 230.
    """
    left = SHELF_X0 + slot * SHELF_PITCH + BADGE_X0
    right = min(SHELF_X0 + slot * SHELF_PITCH + POSTER_W, shelf.width)
    # The last poster is clipped by the image edge, so its corner — badge and all — is not in the
    # picture. Nothing to find, and cropping past the edge raises rather than clamping.
    if right <= left:
        return False

    plate = shelf.crop((left, SHELF_Y0, right, SHELF_Y0 + BADGE_H)).convert("L")
    glyph = shelf.crop(
        (
            SHELF_X0 + slot * SHELF_PITCH + GLYPH_X0,
            SHELF_Y0 + GLYPH_Y0,
            min(SHELF_X0 + slot * SHELF_PITCH + GLYPH_X1, shelf.width),
            SHELF_Y0 + GLYPH_Y1,
        )
    ).convert("L")

    glyph_hist = glyph.histogram()
    if sum(glyph_hist[GLYPH_WHITE:]) <= GLYPH_MIN_PIXELS:
        return False
    # The plate around the glyph, by subtracting one histogram from the other — the glyph box sits
    # wholly inside the plate, so what is left is the plate's own darkness.
    around = [p - g for p, g in zip(plate.histogram(), glyph_hist, strict=True)]
    half = sum(around) / 2
    seen = 0
    for value, count in enumerate(around):
        seen += count
        if seen >= half:
            return value < PLATE_MAX_MEDIAN
    return False


def build_hero() -> Image.Image:
    """The shelf as it ships: badges off, the unrepairable poster dropped, the row still continuing."""
    shelf = Image.open(SOURCE).convert("RGB")
    carrying = [slot for slot in range(8) if _has_badge(shelf, slot)]
    assert carrying == list(WATCHED_SLOTS), (
        f"the source carries watched badges on slots {carrying}, not {list(WATCHED_SLOTS)} — "
        "re-measure the geometry constants above against the new file before trusting this"
    )

    # Seeded: a re-run must be byte-identical, or regenerating the picture churns the repo.
    rng = random.Random(20260907)
    for slot in WATCHED_SLOTS:
        if slot != DROP_SLOT:
            _unwatch(shelf, slot, rng)

    clipped = shelf.crop((SHELF_X0 + LAST_SLOT * SHELF_PITCH, 0, shelf.width, shelf.height))
    width = SHELF_X0 + DROP_SLOT * SHELF_PITCH + clipped.width
    hero = shelf.crop((0, 0, width, shelf.height))
    hero.paste(clipped, (SHELF_X0 + DROP_SLOT * SHELF_PITCH, 0))
    # The scroll chevrons sat at the far right of the source header; bring them to the new one.
    hero.paste(shelf.crop((1500, 0, 1590, SHELF_Y0)), (width - 100, 0))
    return hero


def test_no_poster_on_the_hero_says_the_viewer_already_watched_it() -> None:
    """The whole reason this file exists, checked on every commit rather than every regeneration.

    A "Picked for You" row selling four films the viewer has already seen is the exact failure the
    product exists to prevent, and it led the site for a month because nobody can diff a JPEG.
    """
    hero = build_hero()

    still_badged = [slot for slot in range(DROP_SLOT + 1) if _has_badge(hero, slot)]
    assert not still_badged, f"slots {still_badged} still carry Plex's watched badge"


def test_the_shelf_keeps_its_continues_past_the_edge_cue() -> None:
    """Dropping a poster must not turn a scrollable row into one that stops.

    The source ended mid-poster at its right edge, which is what a Plex shelf looks like. Cutting the
    unrepairable poster out of the middle and closing the gap would have ended the row flush.
    """
    hero = build_hero()

    last = SHELF_X0 + DROP_SLOT * SHELF_PITCH
    assert last < hero.width < last + POSTER_W, (
        f"the final poster runs {last}..{last + POSTER_W} but the image is {hero.width} wide — "
        "it is either cut off entirely or shown whole, and neither reads as 'this row continues'"
    )


def test_regenerates_the_committed_image_when_asked() -> None:
    """Writes only under SHOTS_DIR, so an ordinary test run never touches `docs/`."""
    out = os.environ.get("SHOTS_DIR")
    if not out:
        pytest.skip("set SHOTS_DIR to write the image")
    path = Path(out) / "plex-picked-for-you.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    # JPEG, not PNG: this is photography, and the same picture costs 613KB as a PNG against 120KB
    # here. It is the first image on the landing page, so that difference is the page's load time.
    build_hero().save(path, "JPEG", quality=88, optimize=True, progressive=True)
    assert path.exists()
