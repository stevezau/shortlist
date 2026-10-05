# Resolve Plex owner identity for watch attribution

## Evidence and scope

A genuine Plex Web playback decoded more than 112 seconds and stopped normally. The listener
persisted it under PMS-local account ID 1 instead of the linked owner's plex.tv account. The
session therefore could not join the owner or earn a shared-row credit. The owner had personal
rows disabled, which must not prevent attribution to an eligible shared row.

Normalize only this explicit local-owner alias. Obtain the canonical ID from the server ownership
record established during verified setup, and require its machine ID to match the authenticated
PMS client's actual machine identity. Unknown owner identity remains unresolved. Other accounts
keep their IDs. Do not infer identity from names or rewrite historical events.

## Implementation and verification

1. Exercise the real PMS XML parser through the production listener read path, session persistence
   and shared-row credit with an owner whose personal rows are disabled. The live failure is the
   original reproduction; the XML fixture is representative of its confirmed shape.
2. Add guarded owner normalization to the active-session parser and supply verified owner metadata
   from the listener's existing worker-thread read. Keep this independent of recommendation settings.
3. Cover missing, mismatched and unknown server identity; missing or invalid canonical owner IDs;
   unresolved users; and unchanged shared/Home account IDs.
4. Run focused parser, listener, attribution and clear-history tests, then the required backend/static
   checks on the compatible deployment source. Reuse frontend evidence only while its assets remain
   byte-identical. Obtain independent identity/attribution review before publication or deployment.
5. Publish only the isolated watch patch on the official watch line. Preserve the deployed MCP source,
   schema and runtime configuration in the separate compatible local artifact. Do not replace a newer
   deployed schema with the watch-only image or rewind its database.
6. After the guarded update, repeat genuine owner playback with a new eligible shared title and prove
   decoded playback, the PMS session, canonical persisted session and a fresh Shortlist credit. Keep
   the failed first play unchanged as evidence; do not synthesize progress, completion or credit.

## Completed-history fallback

The guarded live-session correction passed real normal playback, canonical session/shared credit,
empty-history clear retention and independent server verification. A final bounded review found
that the completed-history feed still persisted the same raw owner alias unchanged.

Use one strict authenticated-machine/verified-owner resolver for both ingestion paths. Normalize
only newly ingested owner events, retaining their exact event timestamps; do not rewrite existing
keyed or keyless evidence. For keyless overlap, compare both the raw and canonical natural keys so
the cursor rewind cannot duplicate an already-persisted unresolved event. Unknown authority and
other users preserve their existing behavior. No migration, cursor reset or live history repair.

The history regression must drive the real parser through ingestion, persisted WatchEvent and
personal/shared attribution with run history absent. Cover disabled personal rows, no/mismatched
server authority, invalid owners, other users, exact delivery intervals and repeated/keyless reads.
Both primary regressions failed on the old code: stored owner1 instead of verified99. Run focused
tests, independent review and required full backend/static checks before freezing a new compatible
artifact; publish only the matching watch-only patch and preserve all MCP schema/runtime state.
