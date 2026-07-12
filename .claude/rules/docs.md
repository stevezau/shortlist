---
globs: "**/*.md"
---

# Documentation Updates

When modifying code, check if documentation needs updating.

## Trigger Conditions

Update docs when: new features are added, APIs change, breaking changes occur,
dependencies/requirements change, config options or env vars are modified, or code examples become
outdated.

## What to Update

- **README.md**: Features list, installation steps, config examples
- **docs/reference.md**: Endpoint signatures, request/response examples, env vars, config options, defaults
- **docs/guides.md**: Web interface, schedules, troubleshooting
- **Code examples**: Verify snippets still work after signature changes; update imports

## Breaking Changes

Document what changed, provide before/after examples, and include migration steps (+ Alembic
migration for schema changes).

## Never

Never let environment-specific details from CLAUDE.local.md (hostnames, IPs, personal paths) leak
into committed docs — the public repo stays environment-agnostic.
