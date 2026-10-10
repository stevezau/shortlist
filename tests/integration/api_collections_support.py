"""Helpers shared by the split test modules; moved verbatim from the original file."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _plex_jobs(client: TestClient) -> list[dict]:
    """Inspect actual persisted obligations, preserving ordered steps and physical job state.

    Schedule rebuilds are local work; they do not touch Plex. All other steps remain
    visible so a newly introduced external effect cannot disappear from these assertions.
    """
    jobs = sorted(client.get("/api/system/jobs").json(), key=lambda job: job["id"])
    return [
        {**job, "kind": step["kind"], "payload": step["payload"]}
        for job in jobs
        for step in (job["payload"]["steps"] if job["kind"] == "assistant.converge" else [job])
        if step["kind"] != "schedule.rebuild"
    ]


#: Every key `row_editing.poster_view` renders, nested under `poster`.
POSTER_KEYS = {"mode", "title", "subtitle", "style", "has_image"}
