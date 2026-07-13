"""One definition of "is privacy currently verified" — shared by the status API and the run gate.

Keeping these in one place matters: if the dashboard badge and the write gate disagreed, the
UI could show green while runs are blocked (or worse, the reverse).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from rowarr.server.db.models import PrivacyCheck


def latest_by_tier(session: Session, limit: int = 20) -> dict[str, PrivacyCheck]:
    """Most recent check per tier (T1/T2/PROBE)."""
    latest: dict[str, PrivacyCheck] = {}
    for check in session.query(PrivacyCheck).order_by(PrivacyCheck.id.desc()).limit(limit).all():
        latest.setdefault(check.tier, check)
    return latest


def privacy_summary(session: Session) -> dict:
    """The dashboard/status answer: passing only if EVERY tier's latest result passed."""
    latest = latest_by_tier(session)
    if not latest:
        return {"last_check": None, "passed": None, "tiers": {}}
    newest = max(latest.values(), key=lambda c: c.id)
    return {
        "last_check": _aware(newest.ran_at).isoformat(),
        "passed": all(c.passed for c in latest.values()),
        "tiers": {tier: c.passed for tier, c in latest.items()},
    }


def gate_error(session: Session, server_version: str | None, *, max_age_days: int = 7) -> str | None:
    """Why real writes must be refused right now, or None when the gate is open."""
    from rowarr.engine.clients.plex_pms import MIN_PMS_VERSION, parse_pms_version

    latest = latest_by_tier(session)
    if not latest:
        return "no Privacy Check on record — run one from Settings (or use a dry run) first"
    failed = sorted(tier for tier, check in latest.items() if not check.passed)
    if failed:
        return f"the last Privacy Check FAILED ({', '.join(failed)}) — fix it and re-run the check"
    # Age from the OLDEST tier: a freshly re-run T1 must not carry a months-old T2 along with it.
    oldest = min(latest.values(), key=lambda c: _aware(c.ran_at))
    age_days = (datetime.now(UTC) - _aware(oldest.ran_at)).days
    if age_days > max_age_days:
        return (
            f"the {oldest.tier} Privacy Check last passed {age_days} days ago (max {max_age_days}) — re-run the check"
        )
    if not server_version or parse_pms_version(server_version) < MIN_PMS_VERSION:
        return "the linked Plex server predates the label-restriction privacy fix — upgrade Plex"
    return None


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes even for timezone=True columns."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
