"""E2E: the setup wizard, driven end to end in a real browser against the fake Plex.

This is the highest-value test in the suite: the wizard is the only path a new owner takes,
it crosses every boundary (plex.tv PIN, PMS probe, plex.tv user sync, SSE, the privacy probe,
the engine), and every one of those crossings is a contract two unit suites can each pass
while disagreeing with the other.

Everything except the plex.tv PIN handshake is real: the SPA calls the real API, which drives
the real engine against the fake PMS/plex.tv/TMDB.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import RowarrApp, stub_plex_pin
from tests.fakes.fake_plex import FakePlexState

pytestmark = pytest.mark.e2e

# The probe writes to plex.tv with a 1s throttle and polls the canary's hubs; the engine run
# then does history -> TMDB -> curate -> deliver -> filter-merge for every user. Seconds, not
# milliseconds — so every wait here is an expect() with a generous ceiling, never a sleep.
SLOW = 90_000
LOAD = 20_000


def _connect_plex(page: Page, pms_url: str) -> None:
    """Steps 0-1: welcome -> Login with Plex -> probe the server -> link it."""
    expect(page.get_by_role("heading", name="Welcome")).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Connect Plex").click()

    expect(page.get_by_role("heading", name="Connect Plex")).to_be_visible()
    page.get_by_role("button", name="Login with Plex").click()

    # The PIN is stubbed as already-linked; the SPA polls every 2s before it notices.
    url_field = page.get_by_label("Plex server URL")
    expect(url_field).to_be_visible(timeout=LOAD)
    url_field.fill(pms_url)
    page.get_by_role("button", name="Run checks").click()

    # The capability checklist is the whole point of this step — assert every line, and that
    # the libraries the fake PMS actually reports come back through the real probe endpoint.
    expect(page.get_by_text("Plex version:")).to_be_visible(timeout=LOAD)
    expect(page.get_by_text("Plex Media Server 1.43.3.10793 supports private rows")).to_be_visible()
    expect(page.get_by_text("Plex Pass active")).to_be_visible()
    expect(page.get_by_text("1 librarie(s) found")).to_be_visible()
    expect(page.get_by_text("1 libraries")).to_be_visible()
    expect(page.get_by_text("Movies (30 movies)")).to_be_visible()

    page.get_by_role("button", name="Link this server").click()
    expect(page.get_by_text("Linked to FakePlex")).to_be_visible(timeout=LOAD)


def _skip_history(page: Page) -> None:
    """Step 2: Tautulli is optional — the skip affordance must select Plex's own history."""
    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="History source")).to_be_visible()
    page.get_by_role("button", name="Skip — Plex's own history works without it").click()


def _choose_no_curator(page: Page) -> None:
    """Step 3: "None" is a first-class choice, not a degraded mode — the copy must say so."""
    expect(page.get_by_role("heading", name="Choose your curator")).to_be_visible()
    none_card = page.get_by_role("button", name=re.compile(r"^None\b"))
    expect(none_card).to_be_visible()
    expect(none_card).to_contain_text("Fully functional")
    none_card.click()
    expect(none_card).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_text("Heuristic mode is ready — no AI, no keys, no cloud.")).to_be_visible(timeout=LOAD)


def _pick_users(page: Page, *usernames: str) -> None:
    """Step 4: users sync from plex.tv on entry; enable the named ones."""
    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="Pick your users")).to_be_visible()

    # The owner caveat is a design requirement, not decoration: the owner CANNOT be hidden
    # from, and they must learn that here rather than from their own Home screen tonight.
    expect(page.get_by_text("Heads up, server owner")).to_be_visible()
    expect(page.get_by_text(re.compile("Plex cannot hide collections from the server owner"))).to_be_visible()

    for username in ("sarah", "mike", "canary"):
        expect(page.get_by_role("cell", name=username, exact=True)).to_be_visible(timeout=LOAD)

    for username in usernames:
        toggle = page.get_by_role("switch", name=f"Give {username} a row")
        toggle.click()
        expect(toggle).to_be_checked(timeout=LOAD)


def test_full_wizard_builds_real_rows(fresh_page: Page, fresh_app: RowarrApp, fake_plex):
    """The whole wizard, start to finish, exactly as an owner would walk it."""
    page, app = fresh_page, fresh_app
    pms_url, _, state = fake_plex
    stub_plex_pin(page)

    page.goto("/")
    expect(page).to_have_url(re.compile(r"/setup$"), timeout=LOAD)

    _connect_plex(page, pms_url)
    _skip_history(page)
    _choose_no_curator(page)
    # The canary must get a row too: the Privacy Probe needs an enabled Home user as its canary.
    _pick_users(page, "sarah", "mike", "canary")

    # --- Step 5: the Privacy Check (the real probe, against the fake server) ---------------
    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="Privacy Check")).to_be_visible()
    page.get_by_role("button", name=re.compile("^Run Privacy Check")).click()

    # Live log lines arrive over SSE while the probe runs — proof the stream is wired end to end.
    log = page.get_by_role("list", name="Privacy Check progress")
    expect(log).to_contain_text("creating probe collection", timeout=SLOW)
    expect(log).to_contain_text("excluding the probe label on the canary's share", timeout=SLOW)

    passed_panel = page.get_by_role("status")
    expect(passed_panel).to_contain_text("Your server keeps rows private", timeout=SLOW)
    expect(passed_panel).to_contain_text("PROBE: private")
    expect(log).to_contain_text("deleting the probe collection", timeout=SLOW)
    # Rule 7: probe artifacts never outlive the check.
    assert state.collections == {}, "the probe collection was not cleaned up"

    # --- Step 6: customize ---------------------------------------------------------------
    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="Make it yours")).to_be_visible()

    page.get_by_role("button", name=re.compile(r"^Because you watched \{top_seed\}")).click()
    # The live preview renders the template the way Plex will, not the raw template string.
    expect(page.get_by_text("Because you watched Fargo", exact=True)).to_be_visible()

    # 10, not the 15 default: the seeded library can suggest exactly 10 unwatched titles per
    # user, so this is the largest row every user can actually fill (the engine never invents).
    size_10 = page.get_by_role("button", name="10", exact=True)
    size_10.click()
    expect(size_10).to_have_attribute("aria-pressed", "true")
    page.get_by_label("Refresh rows nightly at").fill("02:15")
    page.get_by_role("button", name="Save & continue").click()

    # --- Step 7: the first real run -------------------------------------------------------
    expect(page.get_by_role("heading", name="First run")).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Build my rows").click()

    expect(page.get_by_text("Rows are live on Plex")).to_be_visible(timeout=SLOW)
    expect(page.get_by_text("run ok")).to_be_visible()

    # Per-user progress must have STREAMED: a card parked on its last pipeline stage proves the
    # run.user.stage events arrived. Without SSE the cards would all read "waiting…"/"done".
    for username in ("sarah", "mike", "canary"):
        expect(page.get_by_text(username, exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"^delivering"))).to_have_count(3)

    page.get_by_role("button", name="Finish setup").click()
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible(timeout=LOAD)

    # --- What actually happened on the server ---------------------------------------------
    setup_state = app.api("GET", "/api/setup/state").json()
    assert setup_state["completed"] is True

    settings = app.api("GET", "/api/settings").json()
    assert settings["row.size"] == 10
    assert settings["row.name_template"] == "Because you watched {top_seed}"
    assert settings["schedule.cron"] == "15 2 * * *"

    rows = {
        label.lower(): collection
        for collection in state.collections.values()
        for label in collection.labels
        if label.lower().startswith("rowarr_")
    }
    assert set(rows) == {"rowarr_sarah", "rowarr_mike", "rowarr_canary"}
    for label, collection in rows.items():
        assert len(collection.item_keys) == 10, f"{label} should hold the chosen row size"
        assert collection.promoted_shared_home, f"{label} was never promoted onto shared Home"

    # The dynamic template renders per user, from that user's own top seed.
    assert rows["rowarr_sarah"].title.startswith("Because you watched Movie")
    assert rows["rowarr_mike"].title.startswith("Because you watched Movie")
    # A cold-start user has no seed to fill {top_seed} with, so the row falls back to the default
    # title rather than putting the dangling half-sentence "Because you watched" on their Home.
    assert rows["rowarr_canary"].title == "✨ Picked for You"

    # Every user's share now excludes the OTHER users' labels — the whole point of the product.
    assert state.users[201].filters["filterMovies"] == "label!=Rowarr_canary,Rowarr_mike"
    assert state.users[202].filters["filterMovies"] == "label!=Rowarr_canary,Rowarr_sarah"
    assert state.users[203].filters["filterMovies"] == "label!=Rowarr_mike,Rowarr_sarah"

    run = app.api("GET", "/api/runs").json()[0]
    assert run["status"] == "ok"
    assert run["dry_run"] is False
    assert run["stats"]["users_ok"] == 3


def test_choosing_ollama_as_the_curator_saves_its_url(fresh_page: Page, fresh_app: RowarrApp, fake_plex):
    """Ollama takes a URL and no key. That key used to be unknown to the settings store, so the
    whole payload 422'd — taking curator.provider down with it and blocking setup entirely."""
    page, app = fresh_page, fresh_app
    pms_url, _, _ = fake_plex
    stub_plex_pin(page)

    page.goto("/")
    _connect_plex(page, pms_url)
    _skip_history(page)

    page.get_by_role("button", name=re.compile(r"^Ollama\b")).click()
    page.get_by_label("Ollama URL").fill("http://127.0.0.1:11434")
    page.get_by_role("button", name="Save & test").click()

    for _ in range(20):
        settings = app.api("GET", "/api/settings").json()
        if settings.get("curator.provider") == "ollama":
            break
        page.wait_for_timeout(250)
    settings = app.api("GET", "/api/settings").json()
    assert settings["curator.provider"] == "ollama"
    assert settings["curator.ollama_url"] == "http://127.0.0.1:11434"


def test_wizard_resumes_on_the_same_step_after_a_reload(fresh_page: Page, fresh_app: RowarrApp, fake_plex):
    """A refresh mid-wizard must not restart it: progress is persisted server-side per step."""
    page = fresh_page
    pms_url, _, _ = fake_plex
    stub_plex_pin(page)

    page.goto("/")
    _connect_plex(page, pms_url)
    _skip_history(page)
    _choose_no_curator(page)

    saved = fresh_app.api("GET", "/api/setup/state").json()
    assert saved["step"] == 3
    assert saved["state"]["curator_provider"] == "none"
    assert saved["completed"] is False

    page.reload()

    # Same step, same choices — and the link survives without re-authenticating with Plex.
    expect(page.get_by_role("heading", name="Choose your curator")).to_be_visible(timeout=LOAD)
    expect(page.get_by_role("button", name=re.compile(r"^None\b"))).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Back").click()
    page.get_by_role("button", name="Back").click()
    expect(page.get_by_text("Linked to FakePlex")).to_be_visible()


def test_skipping_the_privacy_check_forces_a_dry_run(
    fresh_page: Page,
    fresh_app: RowarrApp,
    fake_plex,
    reset_fake_plex: FakePlexState,
):
    """Skip the check and the server MUST refuse to write — the wizard says so and dry-runs.

    The failure here is real, not stubbed: with no Home canary enabled, the probe cannot run.
    That is exactly the situation in which an owner reaches for the skip affordance.
    """
    page, app, state = fresh_page, fresh_app, reset_fake_plex
    pms_url, _, _ = fake_plex
    stub_plex_pin(page)

    page.goto("/")
    _connect_plex(page, pms_url)
    _skip_history(page)
    _choose_no_curator(page)
    _pick_users(page, "sarah", "mike")  # no canary -> the probe has nobody to check against

    page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name=re.compile("^Run Privacy Check")).click()
    expect(page.get_by_text("Privacy Check failed")).to_be_visible(timeout=SLOW)

    # The escape hatch is deliberately behind a fold, and says what it costs.
    page.get_by_text("I understand the risk").click()
    expect(page.get_by_text(re.compile("every user's row may be visible to every other user"))).to_be_visible()
    page.get_by_role("button", name="Skip — continue without privacy verification").click()
    expect(page.get_by_text(re.compile("Privacy verification skipped"))).to_be_visible()

    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="Make it yours")).to_be_visible()
    page.get_by_role("button", name="Save & continue").click()

    # Step 7 must not offer to write. It offers a preview, and says why.
    expect(page.get_by_role("heading", name="First run")).to_be_visible(timeout=LOAD)
    expect(page.get_by_text(re.compile("Rowarr will not write to Plex"))).to_be_visible()
    expect(page.get_by_role("button", name="Build my rows")).to_have_count(0)

    run_bodies: list[str] = []
    page.on(
        "request",
        lambda r: run_bodies.append(r.post_data or "") if r.method == "POST" and r.url.endswith("/api/runs") else None,
    )
    page.get_by_role("button", name="Preview my rows (dry run)").click()
    expect(page.get_by_text("Rows are live on Plex")).to_be_visible(timeout=SLOW)

    assert run_bodies == ['{"dry_run":true}'], f"the SPA must ask for a DRY run, sent: {run_bodies}"
    assert state.collections == {}, "a dry run wrote collections to Plex"
    assert state.users[201].filters["filterMovies"] == "", "a dry run rewrote a user's share filters"

    run = app.api("GET", "/api/runs").json()[0]
    assert run["dry_run"] is True

    # Fail-closed, server-side: even a direct real-run request is refused without a passing check
    # (plex-safety rule 1). The UI's dry-run-only copy is a courtesy; THIS is the guarantee.
    created = app.api("POST", "/api/runs", json={"dry_run": False}).json()
    refused = app.wait_for_run(created["run_id"])
    assert refused["status"] == "error"
    assert "privacy gate" in refused["stats"]["error"]
    assert state.collections == {}
