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

from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Collection, iso_utc
from shortlist.server.scheduler import DEFAULT_CRONS, effective_cron
from shortlist.server.services.jobs import CATALOG
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/schedule", tags=["schedule"], dependencies=[Depends(require_owner)])


class ScheduleJobOut(PassthroughModel):
    """One scheduled job.

    ``extra="allow"`` is on every model in this file, nested ones included: a strict Pydantic
    response model silently DROPS undeclared keys from the payload, so a field missed here would
    disappear from the response instead of failing loudly. The model documents; it never filters.
    """

    type: str  # always "job" — the discriminator against the "rows" entries below
    kind: str
    label: str
    description: str
    setting: str  # the settings key the UI writes to change this cron
    cron: str  # the cron it ACTUALLY runs on, defaults resolved; "" = not scheduled
    using_default: bool  # the cron came from the built-in default, not from something the owner set
    # The built-in cron this job falls back to when nothing is stored — the SPA has no copy of
    # `scheduler.DEFAULT_CRONS` and must not grow one, so "put this schedule back on its built-in
    # time" is only offerable if the server says what that time is. Send `null` for `setting` in
    # `PUT /api/settings` to go back to it.
    default_cron: str
    optional: bool
    writes_plex: bool
    next_run: str | None


class ScheduleRowOut(PassthroughModel):
    id: int
    slug: str
    name: str


class ScheduleRowsOut(PassthroughModel):
    """The rows sharing one cron — ONE trigger that builds all of them, not one entry each."""

    type: str  # always "rows"
    cron: str
    rows: list[ScheduleRowOut]
    next_run: str | None


class ScheduleOut(PassthroughModel):
    jobs: list[ScheduleJobOut]
    rows: list[ScheduleRowsOut]


@router.get("", response_model=ScheduleOut)
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
                # What that default IS. Sent because the alternative is a second copy of
                # DEFAULT_CRONS in the SPA, and the last time a cron default had two copies the
                # drift check was documented as off-by-default for months while it ran nightly.
                "default_cron": DEFAULT_CRONS.get(entry.schedule_setting, ""),
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
