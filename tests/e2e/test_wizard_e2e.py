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

from shortlist.engine.delivery import row_marker
from tests.e2e.conftest import ShortlistApp, stub_plex_pin
from tests.fakes.fake_plex import FakePlexState

pytestmark = pytest.mark.e2e

# The probe writes to plex.tv with a 1s throttle and polls the canary's hubs; the engine run
# then does history -> TMDB -> curate -> deliver -> filter-merge for every user. Seconds, not
# milliseconds — so every wait here is an expect() with a generous ceiling, never a sleep.
SLOW = 90_000
LOAD = 20_000


def _connect_plex(page: Page, pms_url: str) -> None:
    """Steps 0-1: welcome -> connect the Plex account -> pick the server -> probe it -> link it.

    Nobody signed in to reach this wizard — on a fresh install there is nothing to sign in TO.
    Connecting your Plex account is step 1, and it is what claims the instance. Once connected,
    the picker opens straight onto your servers, with every address Plex advertises for them
    already tried from where Shortlist actually runs.
    """
    expect(page.get_by_role("heading", name="Welcome")).to_be_visible(timeout=LOAD)
    # The welcome CTA just advances to the sign-in step; it must NOT pre-empt it with "Connect Plex".
    page.get_by_role("button", name="Get started").click()
    expect(page.get_by_role("heading", name="Connect Plex")).to_be_visible()

    # THE sign-in — exactly one, and it happens here, inside the step that needs it.
    page.get_by_role("button", name="Sign in with Plex").click()
    expect(page.get_by_role("button", name="Sign in with Plex")).to_have_count(0, timeout=LOAD)

    # The picker lists the server and marks the address that answered. The unreachable one it
    # also advertises must be offered but disabled — a guess would have picked the wrong one.
    # SLOW, not LOAD: proving an address is unreachable means waiting for it to time out, which is
    # the entire point of testing rather than guessing.
    expect(page.get_by_text("FakePlex", exact=True).first).to_be_visible(timeout=SLOW)
    working = page.locator("button", has_text=pms_url).first
    expect(working).to_be_enabled(timeout=LOAD)
    # An unreachable address stays CLICKABLE by design — the probe only tried the plex.direct URL
    # and can be a false negative, so it's marked "couldn't reach" as a hint, not disabled outright.
    unreachable = page.locator("button", has_text="10.255.255.1").first
    expect(unreachable).to_contain_text("reach")

    # It preselects the address that worked, so the common case is one click.
    url_field = page.get_by_label("Plex server URL")
    expect(url_field).to_have_value(pms_url, timeout=LOAD)
    page.get_by_role("button", name="Run checks").click()

    # The capability checklist is the whole point of this step — assert every line, and that
    # the libraries the fake PMS actually reports come back through the real probe endpoint.
    expect(page.get_by_text("Plex version:")).to_be_visible(timeout=LOAD)
    expect(page.get_by_text("Plex Media Server 1.43.3.10793 supports private rows")).to_be_visible()
    expect(page.get_by_text("Plex Pass active")).to_be_visible()
    expect(page.get_by_text("2 librarie(s) found")).to_be_visible()
    expect(page.get_by_text("Movies (30 movies)")).to_be_visible()
    expect(page.get_by_text("TV Shows (30 shows)")).to_be_visible()

    page.get_by_role("button", name="Link this server").click()
    expect(page.get_by_text("Linked to FakePlex")).to_be_visible(timeout=LOAD)


def _skip_history(page: Page) -> None:
    """Step 2: TMDB is required, Tautulli is not.

    Without a TMDB key there is nothing to recommend FROM — every run dies at the first user
    with a 401 — so the wizard must not let you past this step until a key is on file. Tautulli
    stays optional: Plex's own history works.
    """
    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="Recommendations & history")).to_be_visible()

    # The gate: you cannot leave, or even skip Tautulli, without TMDB.
    expect(page.get_by_role("button", name="Skip — Plex's own history works without it")).to_be_disabled()

    page.get_by_label("TMDB API key (required)").fill("fake-tmdb-key")
    page.get_by_role("button", name="Save TMDB key").click()
    expect(page.get_by_text("TMDB key works")).to_be_visible(timeout=LOAD)

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

    # First sync pre-selects everyone (so the default click-through builds rows), so wait for that
    # to land, then turn OFF anyone this test doesn't want enabled.
    for username in ("sarah", "mike", "canary"):
        expect(page.get_by_role("switch", name=f"Give {username} a row")).to_be_checked(timeout=LOAD)
    for username in ("sarah", "mike", "canary"):
        if username not in usernames:
            toggle = page.get_by_role("switch", name=f"Give {username} a row")
            toggle.click()
            expect(toggle).not_to_be_checked(timeout=LOAD)


def test_full_wizard_builds_real_rows(fresh_page: Page, fresh_app: ShortlistApp, fake_plex):
    """The whole wizard, start to finish, exactly as an owner would walk it."""
    page, app = fresh_page, fresh_app
    pms_url, _, state = fake_plex
    stub_plex_pin(page, app)

    page.goto("/")
    expect(page).to_have_url(re.compile(r"/setup$"), timeout=LOAD)

    _connect_plex(page, pms_url)
    _skip_history(page)
    _choose_no_curator(page)
    # The canary must be enabled too: the automatic Privacy Probe (now run server-side at first-run
    # time, not as a wizard step) needs an enabled Home user as its canary.
    _pick_users(page, "sarah", "mike", "canary")

    # --- Privacy is verified automatically now — no wizard step. Users -> Make it yours directly;
    # the probe runs (and cleans up) server-side when the first run writes, asserted below. ---------
    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="Make it yours")).to_be_visible()

    page.get_by_role("button", name=re.compile(r"^Because you watched \{top_seed\}")).click()
    # The live preview renders the template the way Plex will, not the raw template string.
    expect(page.get_by_text("Because you watched Fargo", exact=True)).to_be_visible()

    # 10, not the 15 default: the seeded library can suggest exactly 10 unwatched titles per
    # user, so this is the largest row every user can actually fill (the engine never invents).
    size_field = page.get_by_label("Row size")
    size_field.fill("10")
    size_field.blur()  # the free number field commits on blur
    expect(size_field).to_have_value("10")
    page.get_by_label("Refresh rows nightly at").fill("02:15")
    page.get_by_role("button", name="Save & continue").click()

    # --- Step 7: the first real run -------------------------------------------------------
    expect(page.get_by_role("heading", name="First run")).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Build my rows").click()

    expect(page.get_by_text("Rows are live on Plex")).to_be_visible(timeout=SLOW)
    expect(page.get_by_text("run ok")).to_be_visible()

    # Per-user progress must have STREAMED: each card ends on its terminal STREAMED detail
    # ("row built — N picks"), which only renders from the run.user.stage 'done' event. Without SSE
    # a card falls back to a bare "done" with no counts. Asserting the streamed detail (not a
    # mid-run stage) is race-free — the earlier "parked on delivering" check flaked because the run
    # completes and transitions the card to done before the assertion runs.
    for username in ("sarah", "mike", "canary"):
        expect(page.get_by_text(username, exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"^row built — \d+ picks"))).to_have_count(3)

    page.get_by_role("button", name="Finish setup").click()
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible(timeout=LOAD)

    # --- What actually happened on the server ---------------------------------------------
    setup_state = app.api("GET", "/api/setup/state").json()
    assert setup_state["completed"] is True

    settings = app.api("GET", "/api/settings").json()
    assert settings["row.size"] == 10
    assert settings["row.name_template"] == "Because you watched {top_seed}"
    assert settings["schedule.cron"] == "15 2 * * *"

    # A user gets one row per library they have picks in, so a label can map to several rows.
    rows: dict[str, list] = {}
    for collection in state.collections.values():
        for label in collection.labels:
            if label.lower().startswith("shortlist_"):
                rows.setdefault(label.lower(), []).append(collection)

    assert set(rows) == {"shortlist_sarah", "shortlist_mike", "shortlist_canary"}
    for label, collections in rows.items():
        # A row runs PER LIBRARY: EACH library the user has picks in fills to the chosen size on its
        # own — not one budget of 10 split across libraries. So a 'both' watcher gets a full movie
        # row AND a full TV row, and every row is big enough for Plex to render.
        for collection in collections:
            assert len(collection.item_keys) <= 10, f"{label} library row exceeds the chosen size"
            assert len(collection.item_keys) >= 2, f"{label} has a row too small for Plex to render"
            assert collection.promoted_shared_home, f"{label} was never promoted onto shared Home"
    # sarah watches movies AND TV, so she gets a full row in each of her two libraries (10 + 10).
    assert sum(len(c.item_keys) for c in rows["shortlist_sarah"]) == 20, "sarah's two library rows each fill to 10"
    # mike watches only TV, so exactly one full row of 10.
    assert sum(len(c.item_keys) for c in rows["shortlist_mike"]) == 10, "mike's single TV row fills to 10"

    # The dynamic template renders per row, from the top seed of that row's own picks — so a TV
    # row says "Because you watched <a show>", not whatever movie happened to rank first.
    assert len(rows["shortlist_sarah"]) == 2, "sarah watches movies and TV: one row in each library"
    assert {c.section_id for c in rows["shortlist_sarah"]} == {state.section_id, state.show_section_id}
    for collection in rows["shortlist_sarah"]:
        kind = "Movie" if collection.section_id == state.section_id else "Show"
        assert collection.title.startswith(f"Because you watched {kind}"), collection.title
    assert rows["shortlist_mike"][0].title.startswith("Because you watched Show")  # mike only watches TV
    # A cold-start user has no seed to fill {top_seed} with, so the row falls back to the default
    # title rather than putting the dangling half-sentence "Because you watched" on their Home.
    assert rows["shortlist_canary"][0].title.startswith("✨ Picked for You")

    # Every row carries its owner's INVISIBLE marker, so no two rows in a library share a title —
    # and a Plex collection is a tag keyed by title, so identically-titled rows would be ONE tag
    # holding everyone's picks. Users still read exactly the title they were promised.
    accounts = {user.username.lower(): user.id for user in state.users.values()}
    for label, collections in rows.items():
        account_id = accounts[label.removeprefix("shortlist_")]
        for collection in collections:
            assert collection.title.endswith(row_marker(account_id)), (
                f"{label}'s row has no marker — it shares a collection tag with everyone else's"
            )
    for library in (state.section_id, state.show_section_id):
        titles = [c.title for c in state.collections.values() if c.section_id == library]
        assert len(titles) == len(set(titles)), f"two rows share a collection tag in library {library}"

    # Every user's share now excludes the OTHER users' labels — the whole point of the product.
    assert state.users[201].filters["filterMovies"] == "label!=Shortlist_canary,Shortlist_mike"
    assert state.users[202].filters["filterMovies"] == "label!=Shortlist_canary,Shortlist_sarah"
    assert state.users[203].filters["filterMovies"] == "label!=Shortlist_mike,Shortlist_sarah"

    run = app.api("GET", "/api/runs").json()[0]
    assert run["status"] == "ok"
    assert run["dry_run"] is False
    assert run["stats"]["users_ok"] == 3


def test_choosing_ollama_as_the_curator_saves_its_url(fresh_page: Page, fresh_app: ShortlistApp, fake_plex):
    """Ollama takes a URL and no key. That key used to be unknown to the settings store, so the
    whole payload 422'd — taking curator.provider down with it and blocking setup entirely."""
    page, app = fresh_page, fresh_app
    pms_url, _, _ = fake_plex
    stub_plex_pin(page, app)

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


def test_wizard_resumes_on_the_same_step_after_a_reload(fresh_page: Page, fresh_app: ShortlistApp, fake_plex):
    """A refresh mid-wizard must not restart it: progress is persisted server-side per step."""
    page, app = fresh_page, fresh_app
    pms_url, _, _ = fake_plex
    stub_plex_pin(page, app)

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


def test_a_canary_less_server_refuses_real_writes(
    fresh_page: Page,
    fresh_app: ShortlistApp,
    fake_plex,
    reset_fake_plex: FakePlexState,
):
    """Fail-closed, server-side: with no Home canary the automatic probe can't verify privacy, so a
    real run must build nothing (plex-safety rule 1). There is no skip affordance anymore — the
    guarantee is the gate, not the UI. Setup completes via the First-run "Skip for now".
    """
    page, app, state = fresh_page, fresh_app, reset_fake_plex
    pms_url, _, _ = fake_plex
    stub_plex_pin(page, app)

    page.goto("/")
    _connect_plex(page, pms_url)
    _skip_history(page)
    _choose_no_curator(page)
    _pick_users(page, "sarah", "mike")  # no canary -> the probe has nobody to check against

    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="Make it yours")).to_be_visible()
    page.get_by_role("button", name="Save & continue").click()
    expect(page.get_by_role("heading", name="First run")).to_be_visible(timeout=LOAD)

    # A real run is refused: the auto-probe records a failed PROBE (no canary), the gate stays shut,
    # and nothing is written — no UI courtesy required, this is the hard guarantee.
    created = app.api("POST", "/api/runs", json={"dry_run": False}).json()
    refused = app.wait_for_run(created["run_id"])
    assert refused["status"] == "error"
    assert "privacy gate" in refused["stats"]["error"]
    assert state.collections == {}, "a canary-less server must write no collections"
    assert state.users[201].filters["filterMovies"] == "", "a canary-less server must not rewrite share filters"
