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

## After any docs/ change

Regenerate the agent-facing corpus and commit it with the same change:

```bash
python scripts/build_llms_full.py
```

`docs/llms-full.txt` is every page's text in one file, for AI agents that would otherwise crawl 25
pages. GitHub Pages runs only its allow-listed plugins, so nothing builds it at deploy time — it is
committed, and `tests/unit/test_llms_full.py` fails if it drifts from `docs/`.

## Breaking Changes

Document what changed, provide before/after examples, and include migration steps (+ Alembic
migration for schema changes).

## Never

Never let environment-specific details from CLAUDE.local.md (hostnames, IPs, personal paths) leak
into committed docs — the public repo stays environment-agnostic.
