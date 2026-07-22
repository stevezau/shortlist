"""Read, redact, and package the rotating log file for the in-app Logs view.

The whole point of this view is that a user can hand their logs to someone else — that is what the
"copy" and "download" buttons are for, and it is exactly what a beta user did in issue #1. So
everything served here goes through `scrub()` first, which is deliberately broader than the
`redact()` used on exception text: a log line can carry a token in header form, a Bearer credential,
or a provider API key, none of which look like a query parameter.

Over-redaction is fine. A leaked token is not (plex-safety rule 9).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from shortlist.engine.clients.http_retry import redact
from shortlist.logging_config import LOG_LEVELS

# Loguru's default line, which is what the file sink writes:
#   2026-07-21 19:06:47.886 | INFO     | shortlist.server.db.session:run_migrations:99 - message
_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.,]\d+)\s\|\s"
    r"(?P<level>[A-Z]+)\s*\|\s(?P<source>\S*?)\s-\s(?P<message>.*)$"
)

# Credentials that can appear in a log line but NOT as a query parameter, so `redact()` misses them.
# Each pattern keeps its label and replaces only the secret, so the line still reads sensibly.
_EXTRA_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Header form: `X-Plex-Token: abc123`, `'X-Plex-Token': 'abc123'`
    (
        re.compile(r"((?:X-Plex-Token|X-Plex-Client-Identifier)['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}\]]+", re.I),
        r"\1REDACTED",
    ),
    # `Authorization: Bearer abc123` — our own API token, and any other bearer credential.
    (re.compile(r"((?:Authorization['\"]?\s*[:=]\s*['\"]?)?Bearer\s+)[A-Za-z0-9._\-]{8,}", re.I), r"\1REDACTED"),
    # JSON/dict form: `"token": "abc"`, `'apikey': 'abc'`, `"api_key": "abc"`.
    (re.compile(r"(['\"](?:token|api_?key|authToken|accessToken)['\"]\s*:\s*['\"])[^'\"]+", re.I), r"\1REDACTED"),
    # Provider key shapes, wherever they appear: Anthropic, OpenAI, Google, xAI, Groq.
    # The OpenAI pattern must allow `-` and `_` INSIDE the key, not just after `sk-`: every key
    # issued since 2024 is `sk-proj-…`, and OpenRouter — which this provider now supports — uses
    # `sk-or-v1-…`. An alnum-only class stops dead at the hyphen after `proj`/`or` and matches
    # neither. Over-redaction is the safe direction here.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), "REDACTED"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "REDACTED"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "REDACTED"),
    (re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}"), "REDACTED"),
    (re.compile(r"\bgsk_[A-Za-z0-9_\-]{20,}"), "REDACTED"),
    # Plex tokens are 20-char alnum; catch the bare `token=`/`X-Plex-Token` path form too.
    (re.compile(r"(plex\.direct[^\s]*?token[=/])[A-Za-z0-9_\-]+", re.I), r"\1REDACTED"),
)


def scrub(text: str) -> str:
    """Strip every credential shape we know of from a log line (rule 9).

    Applied to EVERY line served or exported, not just ones we think are risky — the value of this
    view is that it can be shared, so the safe assumption is that all of it will be.
    """
    cleaned = redact(text)
    for pattern, replacement in _EXTRA_SECRETS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


@dataclass(frozen=True)
class LogLine:
    """One parsed log entry. A traceback is folded into the entry it belongs to, not left orphaned."""

    ts: str | None
    level: str
    source: str
    message: str

    def as_dict(self) -> dict:
        return {"ts": self.ts, "level": self.level, "source": self.source, "message": self.message}


def log_files(config_dir: Path) -> list[Path]:
    """The live log plus its rotated siblings, newest last."""
    logs = config_dir / "logs"
    if not logs.is_dir():
        return []
    files = sorted((p for p in logs.glob("*.log") if p.is_file()), key=lambda p: p.stat().st_mtime)
    return files


def tail_text(path: Path, *, max_bytes: int = 4_000_000) -> str:
    """The last ``max_bytes`` of a file, starting at a line boundary.

    The file rotates at 10 MB, so reading it whole to show the last few hundred lines would mean
    holding 10 MB in memory per request. Seeking from the end keeps this flat regardless of size.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # discard the partial line the seek landed inside
        return handle.read()


def parse(text: str) -> list[LogLine]:
    """Parse loguru output into entries, attaching continuation lines (tracebacks) to their entry."""
    entries: list[LogLine] = []
    for raw in text.splitlines():
        match = _LINE.match(raw)
        if match:
            entries.append(
                LogLine(
                    ts=match["ts"],
                    level=match["level"],
                    source=match["source"],
                    message=match["message"],
                )
            )
        elif entries and raw.strip():
            # A traceback frame or a wrapped message: it belongs to the entry above it. Dropping
            # these would throw away the stack trace, which is the most useful part of an error.
            previous = entries[-1]
            entries[-1] = LogLine(
                ts=previous.ts,
                level=previous.level,
                source=previous.source,
                message=f"{previous.message}\n{raw}",
            )
    return entries


def _at_least(level: str) -> set[str]:
    """Every level at or above ``level`` — "filter by level" conventionally means "and louder"."""
    if level not in LOG_LEVELS:
        return set(LOG_LEVELS)
    return set(LOG_LEVELS[LOG_LEVELS.index(level) :])


def read_lines(config_dir: Path, *, level: str = "DEBUG", query: str = "", limit: int = 1000) -> dict:
    """The most recent log entries, filtered, redacted, and newest-last.

    Args:
        config_dir: The app's config directory (its ``logs/`` subdirectory holds the files).
        level: Minimum level to include.
        query: Case-insensitive substring the message or source must contain.
        limit: Maximum entries to return (the newest are kept).

    Returns:
        ``{"lines": [...], "total_matched": int, "truncated": bool, "file": str | None}``
    """
    files = log_files(config_dir)
    if not files:
        return {"lines": [], "total_matched": 0, "truncated": False, "file": None}
    # The page polls this every 3s while it is open, and scrubbing dominates the cost (~0.2s per
    # 4 MB, on the shared default executor the nightly run also uses). 1 MB is ~7000 entries — far
    # more than `limit` can show — so the idle poll costs a quarter as much. A SEARCH is different:
    # it is user-initiated, one-off, and the whole point is finding something further back, so it
    # gets the full window rather than quietly reporting "no matches" for a line 2 MB ago.
    window = 4_000_000 if query.strip() else 1_000_000
    entries = parse(scrub(tail_text(files[-1], max_bytes=window)))
    wanted = _at_least(level)
    needle = query.strip().lower()
    matched = [
        e
        for e in entries
        if e.level in wanted and (not needle or needle in e.message.lower() or needle in e.source.lower())
    ]
    kept = matched[-limit:] if limit > 0 else matched
    return {
        "lines": [e.as_dict() for e in kept],
        "total_matched": len(matched),
        "truncated": len(kept) < len(matched),
        "file": files[-1].name,
    }


def build_zip(config_dir: Path) -> bytes:
    """Every log file, redacted, as a zip — what the operator attaches to a bug report.

    Redacted file-by-file rather than shipping the raw files: the export is the single most likely
    thing to end up in a public issue tracker.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        files = log_files(config_dir)
        for path in files:
            try:
                archive.writestr(f"logs/{path.name}", scrub(path.read_text(encoding="utf-8", errors="replace")))
            except OSError as e:  # a file that vanished under rotation shouldn't sink the export
                archive.writestr(f"logs/{path.name}.unreadable.txt", f"could not read: {type(e).__name__}: {e}")
        if not files:
            archive.writestr("logs/EMPTY.txt", "No log files were found in this instance's config directory.")
    return buffer.getvalue()
