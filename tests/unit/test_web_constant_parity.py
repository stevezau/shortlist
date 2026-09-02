"""Numbers the engine and the SPA both hold — pinned so neither can move alone.

Some engine constants have to exist in TypeScript too: the SPA draws the curve it advertises and
validates input before the API sees it. A copy that silently disagrees is the failure mode, and it is
quiet — the UI keeps describing behaviour the engine stopped having. `RECENCY_HALF_LIFE_YEARS` was
the only pair with any guard, and even that one was one-sided: the TS test asserts the literal 8, so
changing the PYTHON constant left both suites green and the two curves apart.

This reads the real `constants.ts` and compares it against the real Python, so either side moving
alone fails here. Parsing the file (rather than duplicating its values a third time) is the point —
a third copy is another thing to drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shortlist.engine.models import MAX_REFRESH_DAYS, MAX_ROW_SIZE, MIN_ROW_SIZE
from shortlist.engine.ranking import RECENCY_HALF_LIFE_YEARS
from shortlist.server.settings_store import DEFAULTS

WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"
CONSTANTS_TS = WEB_SRC / "lib" / "constants.ts"


def _exported_number(source: str, name: str) -> float:
    """The value of `export const <name> = <number>;` in a TypeScript source."""
    match = re.search(rf"^export const {re.escape(name)} = (-?[\d.]+);", source, re.MULTILINE)
    assert match, f"{name} is not an exported numeric constant in constants.ts — did it get renamed?"
    return float(match.group(1))


@pytest.fixture(scope="module")
def ts_source() -> str:
    assert CONSTANTS_TS.is_file(), f"expected the SPA constants at {CONSTANTS_TS}"
    return CONSTANTS_TS.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("ts_name", "python_value", "owner"),
    [
        ("ROW_SIZE_MIN", MIN_ROW_SIZE, "engine.models.MIN_ROW_SIZE"),
        ("ROW_SIZE_MAX", MAX_ROW_SIZE, "engine.models.MAX_ROW_SIZE"),
        ("RECENCY_HALF_LIFE_YEARS", RECENCY_HALF_LIFE_YEARS, "engine.ranking.RECENCY_HALF_LIFE_YEARS"),
        ("MAX_REFRESH_DAYS", MAX_REFRESH_DAYS, "engine.models.MAX_REFRESH_DAYS"),
        (
            "REFRESH_DAYS_DEFAULT",
            DEFAULTS["recommendations.refresh_days"],
            'settings_store.DEFAULTS["recommendations.refresh_days"]',
        ),
        (
            "IDLE_HOLD_DAYS_DEFAULT",
            DEFAULTS["recommendations.idle_hold_days"],
            'settings_store.DEFAULTS["recommendations.idle_hold_days"]',
        ),
    ],
)
def test_the_spa_mirrors_the_engine(ts_source: str, ts_name: str, python_value: float, owner: str) -> None:
    """Each SPA constant equals the engine constant it mirrors. Python is the authority; if this
    fails, change constants.ts to match it — not the other way round."""
    assert _exported_number(ts_source, ts_name) == pytest.approx(float(python_value)), (
        f"constants.ts {ts_name} disagrees with {owner}"
    )


def test_no_copy_of_the_retired_freshness_curve_survives() -> None:
    """The fraction->days curve had FOUR copies (constants.ts, row-globals.ts, row-preview.tsx and a
    row-templates test), none pinned to the engine's. Replacing the stored fraction with a day count
    deleted the need for all of them, so its signature must not reappear.

    Scans the whole SPA source, not just `constants.ts`. This originally read one file while naming
    four, so three of the sites it warned about were outside what it could see. A guard that names
    more ground than it covers is worse than no guard: it reads as proof.

    CODE only. Comments are where the removal gets explained, and several of them quote the formula
    to say it is gone — flagging those would force the explanation out of the codebase to keep the
    test green, which is the opposite of what this is for.
    """
    offenders = [
        f"{path.relative_to(WEB_SRC)}:{n}"
        for path in sorted(WEB_SRC.rglob("*.ts")) + sorted(WEB_SRC.rglob("*.tsx"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "* 13" in line
        and "* 13 *" not in line  # the cron test's "0 3 * 13 *" is not the curve
        and not line.lstrip().startswith(("*", "//", "/*"))
    ]
    assert not offenders, f"the fraction->days curve is gone; do not reintroduce it: {offenders}"
