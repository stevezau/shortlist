"""Regression coverage for runs created by the assistant queue."""

from shortlist.server.db.models import Run


def test_assistant_trigger_survives_runs_list_and_detail_response_models(client):
    """The MCP run queue writes ``assistant`` and both endpoints must serialize it."""
    with client.app.state.sessions() as session:
        run = Run(trigger="assistant", status="ok", dry_run=True)
        session.add(run)
        session.commit()
        run_id = run.id

    listed = client.get("/api/runs")
    assert listed.status_code == 200, listed.text
    summary = next(item for item in listed.json() if item["id"] == run_id)
    assert summary["trigger"] == "assistant"

    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["trigger"] == "assistant"
