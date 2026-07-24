# FAQ

**How is this private? Plex doesn't have per-user collections.**
Since PMS 1.43.2, label-based share restrictions are enforced on Home, Recommended and
Related hubs. Shortlist gives each user's collections a unique label and excludes that label on
every _other_ user's share, so only its owner ever sees it. Rows are delivered hidden and only
promoted once those exclusions are in place, and your share filters are snapshotted first so
Uninstall restores them exactly.

**Why do I get two rows — one under Movies and one under TV Shows?**
A Plex collection lives in exactly one library, and Plex applies share filters per library
(`filterMovies`, `filterTelevision`). So a user who watches both gets one row in each library,
both carrying the same label. This is not cosmetic: a collection holding the wrong type for
its library is matched by neither filter, which means it could not be hidden from anyone.
Your `row.size` is the budget across those rows: a library with at least one pick gets a row,
and a library with none gets none.

**Does Shortlist touch the share settings of users I haven't enabled?**
Yes — it adds label excludes to the share filter of **every** account your server is shared with,
not just the ones you gave a row to. It has to: a Plex collection is visible to anyone whose share
filter does not exclude its label, so if Shortlist only touched the accounts it manages, everyone
else would see those people's private rows. Nothing else in their filter is altered (the write is
a read-modify-write merge), the original is snapshotted first, and `uninstall` restores every one
of them byte-for-byte.

**Do I get a row myself?**
Yes. plex.tv's user list never includes the account that owns the server, so Shortlist adds you to
the Users page separately, badged `owner`. Turn yourself on and you get a Picked-for-You row built
from your own watch history — which is all a one-person server needs.

**What can the server owner see?**
Everything — Plex cannot restrict the owner. Your Home shows all users' rows, not just yours.
If you share the server with others and want the clean experience, watch on a Plex Home user and
keep the admin account for administration.

**Does the AI hallucinate recommendations you don't have?**
It can't. The LLM only re-ranks candidates that are already verified to exist in your
library and unwatched by that user. Anything else it returns is dropped and logged. And
with provider "None", Shortlist works with no AI at all.

**What does the AI actually do, and do I have to pay for it?**
No — Shortlist runs fine with no AI. Most titles are found by the free TMDB sources, and the final
pick and ranking are plain code (no AI, no per-title cost). The AI's one paid job is an optional
_web-search_ source that finds acclaimed titles TMDB misses — the one worth paying for. Set the AI
provider to "None" and you still get full private rows with plain "Because you watched…" reasons. See
[How Shortlist uses AI](guides.md#how-shortlist-uses-ai-and-how-to-control-the-cost) for the full
breakdown and cost-tuning tips.

**Will it fight with Kometa?**
No. Shortlist only ever modifies or deletes collections carrying a `shortlist_*` label. Anything
else — Kometa overlays, your own collections — is detected and left alone.

**What does it send to the LLM?**
Titles only — the AI's one job is a web search for what to watch next, so it sees a short list of
titles the person recently enjoyed. No usernames, account ids, genres, or viewing timestamps.

**What if I uninstall?**
One flow, with a preview: every user's share filters are restored from the snapshot taken
before Shortlist's first write, every shortlist collection is deleted, and the report says exactly
what changed. Your server is as we found it.

**Managed users / kids' accounts?**
Supported as canaries and recommendation targets. Their _restriction profiles_ (parental
controls) are never modified — Shortlist only merges label filters on shares.

**What happens if Plex breaks label restrictions in an update?**
Shortlist merges the label exclusions on every run, but it doesn't watch for Plex regressing that
behaviour — a broken update wouldn't be caught automatically. That's why the label-based hiding
needs PMS **≥ 1.43.2.10687**; older builds ignore the exclusion. Stay on that build or newer, and
watch the README for version advisories.
