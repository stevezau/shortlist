"""What fires tonight — rows and jobs in one chronological list.

Every recurring thing in Shortlist already had an editable cron, but they lived in two different
places: a job's cron inside that job's expanded settings on the Jobs page, a row's inside the row
editor. So "what happens overnight, and in what order?" — the question anyone actually has — could
only be answered by opening a dozen panels and doing the arithmetic yourself.

Read-only. Editing still goes through `PUT /api/settings` (jobs) and `PATCH /api/collections/{id}`
(rows); this endpoint deliberately owns no writes, so there is exactly one place each cron is
validated.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from shortlist.server.auth import require_owner
from shortlist.server.db.models import Collection, iso_utc
from shortlist.server.scheduler import effective_cron
from shortlist.server.services.jobs import CATALOG
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/schedule", tags=["schedule"], dependencies=[Depends(require_owner)])


@router.get("")
async def schedule(request: Request) -> dict:
    """Everything on a timer, each with its cron, its next fire time, and how to change it.

    Rows are grouped by shared cron, exactly as the scheduler groups them — three rows on `30 3 * * *`
    are ONE trigger that builds all three, and listing them as three separate 03:30 entries would
    misrepresent what the server does.
    """
    scheduler = getattr(request.app.state, "scheduler", None)

    def next_run(job_id: str | None) -> str | None:
        if not (scheduler and job_id):
            return None
        scheduled = scheduler.get_job(job_id)
        return iso_utc(scheduled.next_run_time) if scheduled and scheduled.next_run_time else None

    with request.app.state.sessions() as session:
        store = SettingsStore(session)
        jobs = [
            {
                "type": "job",
                "kind": entry.kind,
                "label": entry.label,
                "description": entry.description,
                # The settings key the UI writes to change this cron — so the Schedule view needs no
                # hard-coded map from kind to key.
                "setting": entry.schedule_setting,
                # The cron this job ACTUALLY runs on, not the raw setting. A blank setting means "use
                # the built-in default" for every kind except the opt-in ones, so returning the raw
                # value made a backup that runs nightly at 03:00 read as "Not scheduled".
                "cron": effective_cron(request.app, entry.schedule_setting),
                # Whether that came from the default rather than something the owner set, so the UI
                # can say "built-in default" instead of implying they chose it.
                "using_default": not str(store.get(entry.schedule_setting) or "").strip(),
                "optional": entry.schedule_optional,
                "writes_plex": entry.writes_plex,
                "next_run": next_run(entry.schedule_job_id),
            }
            for entry in CATALOG
            if entry.schedule_setting
        ]

        groups: dict[str, list[dict]] = {}
        for row in session.query(Collection).filter_by(enabled=True).all():
            cron = (row.schedule or "").strip()
            if not cron:
                continue
            groups.setdefault(cron, []).append({"id": row.id, "slug": row.slug, "name": row.name})

    rows = [
        {
            "type": "rows",
            "cron": cron,
            "rows": members,
            # One APScheduler job per distinct cron — see scheduler._job_id.
            "next_run": next_run(f"row-schedule::{cron}"),
        }
        for cron, members in sorted(groups.items())
    ]

    return {"jobs": jobs, "rows": rows}
