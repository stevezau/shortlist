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

Everything is undone in the finally block, and the residue is reported. Prints facts, never a token
(plex-safety rule 9).

Exit code is 0 when every assumption still holds, 1 otherwise — so it can gate a deploy.
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


movie = page("/library/sections/1/all", TOK, size=2, type="1", unwatched="1")
m1, m2 = movie[0].get("ratingKey"), movie[1].get("ratingKey")
shows = [
    d
    for d in page("/library/sections/2/all", TOK, size=60, type="2", unwatched="1")
    if int(d.get("leafCount") or 0) > 5
]
show = shows[0]
srk = show.get("ratingKey")
ep = page(f"/library/metadata/{srk}/allLeaves", TOK, size=1)[0].get("ratingKey")

touched = [m1, m2, ep, srk]
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
    left = 0
    for section, kind in (("1", "1"), ("2", "2"), ("2", "4"), ("12", "4")):
        try:
            left += len(page(f"/library/sections/{section}/all", TOK, type=kind, unwatched="0"))
            left += len(page(f"/library/sections/{section}/all", TOK, type=kind, **{"viewOffset>": "0"}))
        except Exception:
            pass
    print(f"  Tester residue: {left} item(s)" + ("" if left == 0 else "  <-- NOT CLEAN"))

changed = [name for name, ok, _ in results if not ok]
print("\n" + ("ALL ASSUMPTIONS STILL HOLD" if not changed else f"{len(changed)} CHANGED: {', '.join(changed)}"))
sys.exit(1 if changed else 0)
