"""The destructive-SQL sentinel over the improvisation channel (U1 / R1 / #12).

`run_command` is the one place BRAIN can improvise SQL against the app's REAL database (the
per-app `BIAL_DATABASE_URL` is injected into the sandbox for every build — ADR-0028 provisions
the database at project create, so "first build" is no safe harbour). The 2026-07-22 walkthrough
proved the stakes: an unguarded `DELETE FROM visitors` issued "to clean up" wiped real records.

The sentinel scans the argv text — the joined token stream, which also covers `bash -c` scripts
and heredocs carried inside a single token — for improvised destructive SQL: `DELETE FROM`,
`TRUNCATE`, `DROP <object>`, `UPDATE … SET`, `ALTER TABLE … DROP`. It deliberately guards EVERY
build, not only change builds: improvised DML is never part of a build, and an empty database
loses nothing by refusing it (simpler than plumbing a build-kind flag for a weaker guard).

Generated Drizzle migrations remain the sanctioned channel for schema changes INCLUDING drops
(user decision 2026-07-23 — no additive-only gate; requirements legitimately evolve to remove
features). The exemption is structural, not a carve-out: applying migrations
(`npm run db:migrate`, `npx drizzle-kit generate|migrate`) carries no SQL text in argv, so the
sanctioned path passes without special-casing. The sentinel never reads file contents —
iteration 1 by explicit decision; the write-a-script-then-run-it bypass is a named, accepted
follow-up (iteration 2), with BRAIN-trace monitoring as the tripwire.

Spelling coverage: the same destructive statement has many legal spellings, and the sentinel
must not be a keyword-shape trivia quiz. Comments are legal whitespace to the server, so the
text is scanned both raw and comment-stripped (`DELETE/**/FROM t`, `DELETE -- x\\nFROM t`), and
the UPDATE pattern tolerates `ONLY` and a table alias (`UPDATE t AS a SET`, `UPDATE t a SET`).

Hardening (the ReDoS learning, `security-issues/redos-secret-redaction-regex-2026-07-14.md`):
every pattern is linear (bounded quantifiers over disjoint character classes), and the input is
length-capped BEFORE any regex runs. A command too large to scan is REFUSED rather than
partially scanned — a destructive statement could hide past the cap (fail-closed).
"""

from __future__ import annotations

import re

from src.core.prompt_blocks import MIGRATION_GENERATE_CMD
from src.services.orchestrator.constants import REDACT_INPUT_MAX_CHARS

GUARD_INPUT_MAX_CHARS = REDACT_INPUT_MAX_CHARS
"""Scan cap, shared with the redactor's input bound: a legitimate command line never approaches
32KB (file content travels through `write_file`, not argv), so anything larger is refused
outright instead of scanned partially."""

# Each pattern is LINEAR: `\s+` between literal words, bounded identifier runs (`{1,128}`) where
# a name must follow, and no nested quantifiers. IGNORECASE covers case games; `\s` covers
# newline/tab splits inside a quoted statement.
_FORBIDDEN_SPELLS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE), "DELETE FROM"),
    # TRUNCATE must be followed by a table reference so a lone word (a grep for "truncate",
    # a JS `truncate(` helper) never trips it.
    (re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?[\"A-Za-z_]", re.IGNORECASE), "TRUNCATE"),
    (
        re.compile(
            r"\bDROP\s+(?:TABLE|SCHEMA|DATABASE|VIEW|INDEX|SEQUENCE|TYPE|ROLE|OWNED)\b",
            re.IGNORECASE,
        ),
        "DROP",
    ),
    # The table reference may wear `ONLY` and/or an alias (`UPDATE t AS a SET`, `UPDATE t a
    # SET`) — all ordinary spellings of the same destructive statement, and all of them slid
    # past the earlier keyword-whitespace-keyword shape.
    (
        re.compile(
            r"\bUPDATE\s+(?:ONLY\s+)?[\"A-Za-z_][\w\".]{0,128}"
            r"(?:\s+(?:AS\s+)?[A-Za-z_]\w{0,63})?\s+SET\b",
            re.IGNORECASE,
        ),
        "UPDATE … SET",
    ),
    (re.compile(r"\bALTER\s+TABLE\s+[^\s;]{1,128}\s+DROP\b", re.IGNORECASE), "ALTER TABLE … DROP"),
)

# A SQL comment is legal whitespace to the server (`DELETE/**/FROM t`, `DELETE -- x\nFROM t`),
# so scanning the raw text alone is trivially evaded. Both substitutions are BOUNDED and
# non-nested, holding the linearity constraint above.
_COMMENT_DISGUISES: tuple[re.Pattern[str], ...] = (
    re.compile(r"/\*.{0,4096}?\*/", re.DOTALL),
    re.compile(r"--[^\n]{0,4096}"),
)


def _unmask(text: str) -> str:
    """Replace SQL comments with a single space, so a comment can never impersonate the
    whitespace the patterns require between keywords. Never the ONLY thing scanned: the raw
    text is scanned too, so an unmasking miss (an unterminated comment, a comment past the
    bound) can only ever ADD a detection, never remove one — fail-closed."""
    for disguise in _COMMENT_DISGUISES:
        text = disguise.sub(" ", text)
    return text


def _refusal(offense: str) -> str:
    """The corrective message — it must teach, not dead-end: WHY the command was blocked, what
    verification looks like instead, and the one sanctioned channel for schema changes."""
    return (
        f"This command was blocked before it ran: it carries destructive SQL ({offense}) aimed "
        "at the app's real database, which may already hold the user's records. Improvised data "
        "mutations are never part of a build — never DELETE, TRUNCATE, DROP, or UPDATE records "
        "to test, demo, or clean up. Verify your work by type-checking and rendering instead. "
        "If the user's requirements change the schema (including removing a table or column), "
        f"edit `db/schema.ts` and generate a migration (`{MIGRATION_GENERATE_CMD}`, then "
        "`npm run db:migrate`) — that is the sanctioned channel — and state plainly in your "
        "done-summary that the removed feature's data goes with it."
    )


def you_shall_not_pass(command: list[str]) -> str | None:
    """The Gandalf gate: returns the corrective refusal when `command` carries improvised
    destructive SQL (or is too large to scan — fail-closed), `None` when it may proceed.

    Scans the space-joined argv, which subsumes per-token content (a statement split across
    tokens rejoins across the separators; a `bash -c` script or heredoc rides inside one token
    and is scanned verbatim)."""
    joined = " ".join(command)
    if len(joined) > GUARD_INPUT_MAX_CHARS:
        return (
            "This command was blocked before it ran: it is too large to scan for destructive "
            f"SQL ({len(joined)} chars; the cap is {GUARD_INPUT_MAX_CHARS}). Put file content "
            "in a file with `write_file` and keep commands short."
        )
    unmasked = _unmask(joined)
    scans = (joined,) if unmasked == joined else (joined, unmasked)
    for pattern, offense in _FORBIDDEN_SPELLS:
        if any(pattern.search(scan) for scan in scans):
            return _refusal(offense)
    return None
