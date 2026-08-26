"""Re-check every PMS behaviour the watching-account transfer was BUILT on.

Run this after any Plex upgrade. Each of these was measured, not looked up, and each one silently
breaks the transfer in a different way if the server changes its mind:

  1. a scrobble writes NO row to the play-history log
       -> if it starts writing, a transfer injects ~11,000 fake plays into `watch_events` and every
          figure in the effectiveness report inflates, with nothing to notice it
  2. a scrobble on an EPISODE key leaves the show at 1/N
       -> if it marks the whole show, the One Piece bug is back
  3. `/:/progress` sets an exact offset
       -> if not, no partial watch can be replicated
  4. `/:/progress?time=0` does NOT clear an offset
       -> if it starts working, the undo's reset path is doing more than it needs to (harmless), but
          the comment explaining why it uses unscrobble becomes wrong
  5. `/:/unscrobble` clears BOTH the count and the offset
       -> if it stops clearing the offset, undo leaves items part-watched again
  6. a scrobble CLEARS an existing offset
       -> if it stops, the planner writes a redundant reposition after every mark (harmless), but the
          matrix that proves the fixed point is wrong

Run it against a THROWAWAY Home account, never a real one — it writes and then undoes watch state.
The account id and the server address are read from the environment:

    PMS_URL=http://your-server:32400 \
    PLEX_PREFS="/path/to/Preferences.xml" \
    TEST_ACCOUNT_ID=123456 \
    python scripts/recheck_pms_assumptions.py

This directory is NOT copied into the Docker image — it is maintenance tooling, not runtime. To run
it against a containerised install, copy it in first:

    docker cp scripts/recheck_pms_assumptions.py shortlist:/tmp/recheck.py
    docker exec -e PLEX_PREFS=... -e PMS_URL=... -e TEST_ACCOUNT_ID=... shortlist python /tmp/recheck.py

Everything is undone in the finally block, and the residue is reported. Prints facts, never a token
(plex-safety rule 9).

Exit code is 0 only when every assumption still holds AND the account was left clean — a residue,
or a residue read that failed, fails it too. So it can gate a deploy.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# No environment-specific defaults in the committed repo (docs rule): a hostname or an account id
# baked in here would be one person's server in everybody's checkout.
PREFS = os.environ.get("PLEX_PREFS", "")
PMS = os.environ.get("PMS_URL", "").rstrip("/")
TESTER = int(os.environ.get("TEST_ACCOUNT_ID") or 0)
if not (PREFS and PMS and TESTER):
    raise SystemExit("set PLEX_PREFS, PMS_URL and TEST_ACCOUNT_ID — see the module docstring")

prefs = Path(PREFS).read_text()
ADMIN = re.search(r'PlexOnlineToken="([^"]+)"', prefs).group(1)
MACHINE = re.search(r'ProcessedMachineIdentifier="([^"]+)"', prefs).group(1)


def plextv(path, token, method="GET"):
    req = urllib.request.Request(
        "https://plex.tv" + path,
        headers={"X-Plex-Token": token, "X-Plex-Client-Identifier": "shortlist", "Accept": "application/json"},
        method=method,
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def call(url, token, **params):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Plex-Token": token, "Accept": "application/xml"})
    body = urllib.request.urlopen(req, timeout=60).read()
    return ET.fromstring(body) if body else None


def page(path, token, size=500, **params):
    req = urllib.request.Request(
        PMS + path + ("?" + urllib.parse.urlencode(params) if params else ""),
        headers={
            "X-Plex-Token": token,
            "Accept": "application/xml",
            "X-Plex-Container-Start": "0",
            "X-Plex-Container-Size": str(size),
        },
    )
    return list(ET.fromstring(urllib.request.urlopen(req, timeout=180).read()))


users = plextv("/api/v2/home/users", ADMIN)
me = next(u for u in (users if isinstance(users, list) else users["users"]) if int(u["id"]) == TESTER)
switch = plextv(f"/api/v2/home/users/{me['uuid']}/switch", ADMIN, method="POST")["authToken"]
TOK = next(
    x["accessToken"] for x in plextv("/api/v2/resources?includeHttps=1", switch) if x.get("clientIdentifier") == MACHINE
)

version = ET.fromstring(
    urllib.request.urlopen(
        urllib.request.Request(PMS + "/", headers={"X-Plex-Token": ADMIN, "Accept": "application/xml"}), timeout=60
    ).read()
).get("version")
print(f"PMS version: {version}\n")


def state(rk, token=TOK):
    el = next(iter(call(f"{PMS}/library/metadata/{rk}", token)))
    return (int(el.get("viewCount") or 0), int(el.get("viewOffset") or 0))


def show_state(rk):
    el = next(iter(call(f"{PMS}/library/metadata/{rk}", TOK)))
    return f"{el.get('viewedLeafCount')}/{el.get('leafCount')}"


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK ' if ok else 'CHANGED'}] {name}: {detail}")


def leaf_sections() -> list[tuple[str, str]]:
    """`(section_key, leaf_type)` for every library, DISCOVERED — never hardcoded.

    The first version hardcoded 1/2/12, which are one server's section keys, in a file whose own
    comment cites the rule against exactly that. On any other server every residue query 404s — and
    since those were wrapped in `except: pass`, the script reported a spotless cleanup derived
    entirely from errors.
    """
    # Enumerated with the TESTER's own token, not the admin's. A throwaway Home account is usually
    # not shared every library — the normal way you would create one — and listing as admin then
    # reading as the tester made the precondition raise on the first unshared section, aborting with
    # a traceback before a single assumption ran, and again in `finally` where it masked whatever the
    # try block had really raised. Listing as the tester returns exactly what it is entitled to.
    out = []
    for el in page("/library/sections", TOK):
        if el.get("type") == "movie":
            out.append((el.get("key"), "1"))
        elif el.get("type") == "show":
            out.append((el.get("key"), "4"))
    return out


SECTIONS = leaf_sections()


def watch_state_count(token=TOK) -> int:
    """How many leaves this account has watched or started — a FLOOR, not a total.

    One page per query, so a heavy account reports the cap rather than its real size. That is fine for
    both callers: the precondition refuses on any non-zero, and residue is expected to be zero.

    Deliberately NOT exception-guarded: a read that fails must never be reported as "nothing there".
    """
    total = 0
    for key, leaf_type in SECTIONS:
        total += len(page(f"/library/sections/{key}/all", token, type=leaf_type, unwatched="0"))
        total += len(page(f"/library/sections/{key}/all", token, type=leaf_type, **{"viewOffset>": "0"}))
    return total


# REFUSE a populated account. The only thing between TEST_ACCOUNT_ID and a real Home user was a
# sentence in the docstring — and the titles this scrobbles are picked from `unwatched=1`, which on a
# real account includes items somebody is part-way through.
existing = watch_state_count()
if existing:
    raise SystemExit(
        f"account {TESTER} already has at least {existing} watched/in-progress item(s) — this script writes "
        "and then clears watch state, so it must point at a THROWAWAY Home account with none"
    )

movie_section = next((k for k, t in SECTIONS if t == "1"), None)
show_section = next((k for k, t in SECTIONS if t == "4"), None)
if not (movie_section and show_section):
    raise SystemExit("need one movie library and one show library to check these assumptions")

movie = page(f"/library/sections/{movie_section}/all", TOK, size=2, type="1", unwatched="1")
m1, m2 = movie[0].get("ratingKey"), movie[1].get("ratingKey")
shows = [
    d
    for d in page(f"/library/sections/{show_section}/all", TOK, size=60, type="2", unwatched="1")
    if int(d.get("leafCount") or 0) > 5
]
show = shows[0]
srk = show.get("ratingKey")
ep = page(f"/library/metadata/{srk}/allLeaves", TOK, size=1)[0].get("ratingKey")

# NOT `srk`. Un-scrobbling a show key clears every episode under it, and this script scrobbles only
# ONE episode — so including it made the cleanup a wider delete than the writes it was undoing.
touched = [m1, m2, ep]
try:
    before_history = len(page("/status/sessions/history/all", ADMIN, accountID=str(TESTER), sort="viewedAt:desc"))

    print("=== assumptions ===")
    call(f"{PMS}/:/scrobble", TOK, key=m1, identifier="com.plexapp.plugins.library")
    time.sleep(2)
    check("a scrobble marks it watched", state(m1)[0] >= 1, f"viewCount={state(m1)[0]}")

    call(f"{PMS}/:/progress", TOK, key=m1, identifier="com.plexapp.plugins.library", time="480000", state="stopped")
    time.sleep(2)
    check("/:/progress sets an exact offset", state(m1)[1] == 480000, f"offset={state(m1)[1]}")

    call(f"{PMS}/:/progress", TOK, key=m1, identifier="com.plexapp.plugins.library", time="0", state="stopped")
    time.sleep(2)
    check("time=0 does NOT clear an offset", state(m1)[1] == 480000, f"offset={state(m1)[1]}")

    call(f"{PMS}/:/scrobble", TOK, key=m1, identifier="com.plexapp.plugins.library")
    time.sleep(2)
    check("a scrobble CLEARS an existing offset", state(m1)[1] == 0, f"offset={state(m1)[1]}")

    call(f"{PMS}/:/progress", TOK, key=m2, identifier="com.plexapp.plugins.library", time="300000", state="stopped")
    time.sleep(2)
    call(f"{PMS}/:/unscrobble", TOK, key=m2, identifier="com.plexapp.plugins.library")
    time.sleep(2)
    check("unscrobble clears count AND offset", state(m2) == (0, 0), f"state={state(m2)}")

    call(f"{PMS}/:/scrobble", TOK, key=ep, identifier="com.plexapp.plugins.library")
    time.sleep(3)
    after = show_state(srk)
    check("an episode scrobble leaves the show partial", after.split("/")[0] == "1", f"show reads {after}")

    time.sleep(3)
    now_history = len(page("/status/sessions/history/all", ADMIN, accountID=str(TESTER), sort="viewedAt:desc"))
    check(
        "a scrobble writes NO history row",
        now_history == before_history,
        f"{before_history} -> {now_history}",
    )
finally:
    print("\n=== cleanup ===")
    for rk in touched:
        try:
            call(f"{PMS}/:/unscrobble", TOK, key=rk, identifier="com.plexapp.plugins.library")
        except Exception as e:
            print(f"  {rk}: {type(e).__name__}")
    time.sleep(3)
    # Two properties, and the first attempt at this kept only one of them.
    #
    #   * a FAILED read must never print as "residue: 0" — a clean bill of health made entirely of
    #     errors, which is what `except: pass` produced;
    #   * and it must not replace the real assumption failure with its own traceback, which is the
    #     `finally`-masking defect `leaf_sections` cites as the reason it exists.
    #
    # So: caught, reported as unknown, and still failing the exit status.
    try:
        left = watch_state_count()
        print(f"  residue: {left} item(s)" + ("" if left == 0 else "  <-- NOT CLEAN"))
    except Exception as exc:
        left = -1
        print(f"  residue: UNKNOWN ({type(exc).__name__})  <-- NOT CLEAN")

changed = [name for name, ok, _ in results if not ok]
print("\n" + ("ALL ASSUMPTIONS STILL HOLD" if not changed else f"{len(changed)} CHANGED: {', '.join(changed)}"))
# Residue counts too: a gate that exits 0 having left watch state on someone's account is not a gate.
sys.exit(1 if (changed or left) else 0)
