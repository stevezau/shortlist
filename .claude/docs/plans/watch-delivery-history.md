# Delivered membership survives clearing run history

## Scope

Keep the delivered membership used by watch attribution independent of deletable run logs. Preserve matching against the row at playback time, current enabled-row/delivery guards, personal/shared identity, and existing outcome ledgers. The user subsequently authorized deployment and server verification. Prepare and verify an isolated release containing only this fix before publishing; preserve unrelated checkout changes.

## Plan

1. Add regression coverage proving a personal and shared watch still credits after clearing run history, and the current personal pick set survives.
2. Add migration 0102 (official predecessor 0101) and a compact independent delivery-snapshot table. Store row/person/library identity, delivery interval, collection key, exact typed title keys, personal pick IDs, and shared audience/mutes. No foreign key to runs.
3. Record confirmed per-library delivery results atomically with picks and the delivery ledger; preserve partial successes and ignore dry/omitted/failed library writes. Keep delivery and pick timestamps aligned. Repeated persistence is idempotent.
4. Read durable intervals for at-play membership and current picks. Preserve existing enabled/current-ledger and removed-person gates. Capture empty/removal boundaries. Close records only after actual removal; retain current snapshots when run logs expire.
5. Backfill trustworthy still-linked legacy delivery records in the frozen migration; never infer deleted provenance from detached picks. Clearing/pruning leaves the independent records alone. Update test fixtures to represent actual delivered state. Already-cleared installations require a new delivery to establish membership again.
6. Cover clear/retention, replacement timing, empty/no-write/dry/partial deliveries, shared audiences/mutes, identity namespaces, removal/recreation, idempotence/run-ID reuse, and safe legacy conversion.
7. Coordinate focused tests through the verifier, then independent architecture review. Root owns UI; verifier owns public docs and frontend checks.

## Constraints

- Current-only records are insufficient: late events need delivery intervals.
- Existing personal/shared watch credits remain in their current tables.
- Legacy unknown shared audience/type must not become public by inference.
- Preserve unrelated assistant feature edits in their original checkout; none are included in this release. The release migration uses the next official revision, 0102.
- Current enabled-row/delivery guards remain intentionally conservative for rows that have been removed entirely.
- Legacy writes without a trustworthy delivery or execution clock make their row/library history uncreditable until a new delivery; enqueue time cannot establish ordering.

## Original development-tree verification

- Original clear-runs regression failed after a successful pre-clear membership check.
- Independent snapshot writer/reader regressions passed; genuine migration tests passed (35 cases) against that development tree. The isolated release renumbers the migration from 0108 to 0102 and must be verified independently against official predecessor 0101.
- Existing affected tests: 1,388 passed on the first broad pass; 11 fixture transitions corrected for durable delivery evidence.
- Current user-row cards, collection artwork previews, and engine keep-out history read the retained delivery state; diagnostic run links may be empty after clearing logs.
- Corrected fixture and current-reader focused rerun passed (163 tests), including API serialization and personal/shared post-clear credits.
- Final independent architecture review found no blocking issues. Frontend suite passed (3,132 tests), with typecheck, lint, build and migration fingerprint checks passing.
- Full backend pass completed: 7,064 passed, 2 skipped, 92.95% coverage. All 20 failures/setup errors then passed on a focused rerun: two remaining run-service fixture conversions, the regenerated OpenAPI description, and 17 setup failures caused by the frontend build temporarily replacing `dist/assets` during backend startup. The rerun used a stable build.
- Final Runs browser checks passed (4 cases). Typecheck, lint, format, production build, migration fingerprints and final diff checks all passed. Implementation and verification are complete; no commit or deployment performed.

## Isolated release verification

- Release branch starts at official dev commit `d7b9a57a1067a532cd0c6e43d03f610ca32ca666`.
- Only watch-owned files and named hunks were transplanted; unrelated assistant schema, jobs, API and provider changes were excluded. The original checkout is unchanged.
- Regenerated the OpenAPI snapshot/types, documentation corpus and migration fingerprint in the isolated tree.
- The isolated release passed 6,777 backend tests (2 skipped, 95.76% coverage), 3,112 frontend tests, 234 focused delivery/migration tests and 4 Runs browser checks; static checks and the production build passed.
- Official CI found two additional browser fixtures still seeding only run-linked recommendations. A test-only correction passed all 11 watch-outcomes browser tests; runtime files were unchanged. Final commit `fb062850d7e2b514f694217297c0fe1da0c35393` passed every official CI gate, including Website, and published the verified official image.
- Deployment verification matched the running source files and served web assets to that official release. Existing delivery records survive the migration-free correction below; no additional refresh is required by its installation.

## Retry confirmation correction

A sibling-library failure can retry an already delivered library. Retain its first confirmation only within that person's current row-retry chain, after an explicit unchanged-membership reread and exact collection, typed title, audience and mute equality. Changed or uncertain attempts get a new confirmation time.

Keep the earliest confirmed boundary separately from the replaceable run breakdown. Persistence closes only older personal snapshots for the exact stored user ID, slug, row and library; boundary-only evidence never creates membership or edits the delivery ledger. Replaying it preserves snapshots beginning at that boundary or later. A genuinely changed or uncertain intermediate state remains uncredited.

Three real delivery-path regressions failed before implementation: unchanged retry lost a new title's earlier eligibility; changed retry and exhausted retry kept an old title eligible too long. Implemented the chain-local confirmation cache and separate report boundaries without a schema change. The affected engine and persistence suites passed 741 tests. After two review corrections, all 32 snapshot tests passed, including changed/rebuilt/uncertain state, terminal failure, cancellation, skipped and unattempted libraries, exact owner scoping, and report replay. Final independent review found no issues. Full backend verification is pending before another publication.
