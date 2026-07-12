# FAQ

**How is this private? Plex doesn't have per-user collections.**
Since PMS 1.43.2, label-based share restrictions are enforced on Home, Recommended and
Related hubs. Rowarr gives each user's collection a unique label and excludes that label on
every _other_ user's share. The built-in Privacy Check proves this works on your server —
with a canary account's own eyes — before anything real is written.

**What can the server owner see?**
Everything — Plex cannot restrict the owner. Your Home shows all users' rows. Use a
separate viewing account if you want the clean experience.

**Does the AI hallucinate recommendations you don't have?**
It can't. The LLM only re-ranks candidates that are already verified to exist in your
library and unwatched by that user. Anything else it returns is dropped and logged. And
with provider "None", Rowarr works with no AI at all.

**Will it fight with Kometa?**
No. Rowarr only ever modifies or deletes collections carrying a `rowarr_*` label. Anything
else — Kometa overlays, your own collections — is detected and left alone.

**What does it send to the LLM?**
Titles, years and genres only. No usernames, no account ids, no viewing timestamps.

**What if I uninstall?**
One flow, with a preview: every user's share filters are restored from the snapshot taken
before Rowarr's first write, every rowarr collection is deleted, and the report says exactly
what changed. Your server is as we found it.

**Managed users / kids' accounts?**
Supported as canaries and recommendation targets. Their _restriction profiles_ (parental
controls) are never modified — Rowarr only merges label filters on shares.

**What happens if Plex breaks label restrictions in an update?**
The weekly scheduled re-verification catches it and flips the dashboard privacy badge red
with a notification. The README carries version advisories.
