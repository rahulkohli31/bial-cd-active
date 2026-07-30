"""U1 / R1 — the destructive-SQL sentinel over the improvisation channel (#12).

Test-first encoding of the 2026-07-22 walkthrough incident: during a change build, BRAIN issued
an unguarded ``DELETE FROM visitors`` through ``run_command`` and wiped real app data. The
sentinel (`you_shall_not_pass`) must block improvised destructive SQL carried in argv — psql
one-liners, node/bash payloads, heredocs — while the sanctioned migration channel
(`npm run db:migrate` / `npx drizzle-kit generate`, whose argv never carries SQL text) passes
untouched. Patterns are linear and the input is capped BEFORE scanning (the ReDoS learning);
a command too large to scan is refused outright rather than scanned partially (fail-closed —
a destructive statement could hide past the cap).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.services.orchestrator.agent import build_agent
from src.services.orchestrator.deps import BuildDeps, SandboxSession
from src.services.orchestrator.progress import ProgressEmitter
from src.services.orchestrator.sql_guard import GUARD_INPUT_MAX_CHARS, you_shall_not_pass
from src.services.sandbox import ExecResult
from tests.services.orchestrator.conftest import CollectingSink
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_FAKE_DSN = "postgresql://app:app@db.local/appdb"

# --- the walkthrough incident, verbatim shape --------------------------------


def test_the_walkthrough_delete_from_visitors_is_blocked() -> None:
    # The exact improvisation that destroyed real data on 2026-07-22: a psql one-liner
    # clearing a table "to clean up" during a change build.
    refusal = you_shall_not_pass(["psql", _FAKE_DSN, "-c", "DELETE FROM visitors"])
    assert refusal is not None
    # The message is corrective, not a dead-end: it names WHY and the sanctioned channel.
    lowered = refusal.lower()
    assert "blocked" in lowered
    assert "migration" in lowered
    assert "type-check" in lowered or "render" in lowered


# --- benign commands pass untouched ------------------------------------------


def test_normal_build_commands_pass() -> None:
    for command in (
        ["npm", "run", "build"],
        ["npx", "tsc", "--noEmit"],
        ["npm", "install", "zod"],
        ["ls", "app"],
        ["npx", "drizzle-kit", "generate"],
        ["npm", "run", "db:migrate"],
        ["grep", "-rn", "visitors", "app/"],
        ["node", "-e", "console.log(process.env.BIAL_APP_ID)"],
    ):
        assert you_shall_not_pass(command) is None, f"{command} should pass"


def test_migration_apply_passes_even_when_the_migration_drops_a_table() -> None:
    # The sanctioned channel: the DROP lives in the generated migration FILE, never in argv —
    # requirements legitimately evolve to remove features (user decision 2026-07-23, no
    # additive-only gate). The sentinel scans argv only, so this passes structurally.
    assert you_shall_not_pass(["npm", "run", "db:migrate"]) is None
    assert you_shall_not_pass(["npx", "drizzle-kit", "migrate"]) is None


# --- the destructive family is blocked across carriers -----------------------


def test_truncate_via_node_script_is_blocked() -> None:
    script = (
        "const{Client}=require('pg');const c=new Client(process.env.BIAL_DATABASE_URL);"
        "c.connect().then(()=>c.query('TRUNCATE TABLE visitors'))"
    )
    assert you_shall_not_pass(["node", "-e", script]) is not None


def test_drop_table_via_psql_is_blocked() -> None:
    assert you_shall_not_pass(["psql", _FAKE_DSN, "-c", "DROP TABLE visitors"]) is not None


def test_heredoc_carried_in_bash_argv_is_blocked() -> None:
    heredoc = "psql \"$BIAL_DATABASE_URL\" <<'EOF'\nDELETE FROM visitors;\nEOF"
    assert you_shall_not_pass(["bash", "-c", heredoc]) is not None


def test_update_set_via_psql_is_blocked() -> None:
    # Improvised UPDATEs mutate user records outside the app's own code path — blocked even
    # when scoped; the app and its migrations are the only sanctioned mutation channels.
    refusal = you_shall_not_pass(
        ["psql", _FAKE_DSN, "-c", "UPDATE visitors SET name = 'demo' WHERE id = 1"]
    )
    assert refusal is not None


def test_alter_table_drop_column_via_psql_is_blocked() -> None:
    assert (
        you_shall_not_pass(["psql", _FAKE_DSN, "-c", "ALTER TABLE visitors DROP COLUMN badge"])
        is not None
    )


def test_case_and_whitespace_variants_are_blocked() -> None:
    assert you_shall_not_pass(["psql", _FAKE_DSN, "-c", "delete\n   from visitors"]) is not None
    assert you_shall_not_pass(["psql", _FAKE_DSN, "-c", "Truncate visitors"]) is not None


@pytest.mark.parametrize(
    "statement",
    [
        # aliases and ONLY — ordinary SQL, not obfuscation
        "UPDATE t AS a SET x = 1",
        "UPDATE t a SET x = 1",
        "UPDATE ONLY t SET x = 1",
        "UPDATE ONLY visitors v SET name = 'demo'",
        # comments are legal whitespace to the server
        "DELETE/**/FROM visitors",
        "DELETE -- cleanup\nFROM visitors",
        "TRUNCATE/*c*/visitors",
        "DROP/**/TABLE visitors",
        "UPDATE/**/visitors SET name = 'demo'",
    ],
)
def test_equivalent_spellings_of_the_same_destructive_statement_are_blocked(
    statement: str,
) -> None:
    # A sentinel that only recognises `KEYWORD<space>KEYWORD` is a trivia quiz, not a guard:
    # every spelling below is the SAME destructive statement the server would happily run.
    assert you_shall_not_pass(["psql", _FAKE_DSN, "-c", statement]) is not None


def test_comment_normalisation_does_not_manufacture_false_positives() -> None:
    # Stripping comments must not turn benign build argv into a refusal — `--flag` tokens
    # look exactly like a SQL line comment.
    for command in (
        ["npx", "tsc", "--noEmit"],
        ["npm", "install", "--save-dev", "drizzle-kit"],
        ["npm", "run", "build", "--", "--verbose"],
        ["grep", "-rn", "truncate(", "app/"],
        ["node", "-e", "const truncate = (s) => s.slice(0, 10)"],
    ):
        assert you_shall_not_pass(command) is None, f"{command} should pass"


# --- fail-closed sizing + linearity (the ReDoS learning) ---------------------


def test_oversized_command_is_refused_not_partially_scanned() -> None:
    # A destructive statement hiding PAST the scan cap must not slip through: too-large-to-scan
    # means refused, full stop.
    padding = "x" * (GUARD_INPUT_MAX_CHARS + 100)
    refusal = you_shall_not_pass(["bash", "-c", padding + "; DELETE FROM visitors"])
    assert refusal is not None


def test_scan_is_linear_on_adversarial_payloads() -> None:
    # Near-miss payloads crafted at each pattern's backtracking seam, sized to the 64KB
    # walkthrough bound. Quadratic behaviour would blow the generous 5s ceiling loudly
    # (mirrors test_redaction_is_linear_on_an_adversarial_blob).
    payloads = [
        "_" * 64_000,
        "UPDATE " + "x" * 64_000,
        "UPDATE x " * 8_000,
        "UPDATE x AS " * 6_000,
        "ALTER TABLE " + "y" * 30_000,
        "DELETE FRO " * 6_000,
        # the comment-unmasking seam: many openers, never a closer
        ("/*" + "a" * 6) * 8_000,
        "--" * 32_000,
    ]
    start = time.perf_counter()
    for payload in payloads:
        you_shall_not_pass(["bash", "-c", payload])
    assert time.perf_counter() - start < 5.0


# --- in-run wiring: run_command consults the guard (integration) -------------


def _all_text(messages: list[ModelMessage]) -> str:
    out: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.extend(item for item in content if isinstance(item, str))
    return "\n".join(out)


def _capturing_model(turns: list[ModelResponse], captured: dict[str, Any]) -> FunctionModel:
    iterator = iter(turns)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured.setdefault("incoming", []).append(_all_text(messages))
        return next(iterator, text_turn("done"))

    return FunctionModel(respond)


def _deps(fake: FakeSandbox, sink: CollectingSink) -> BuildDeps:
    emitter = ProgressEmitter(sink)
    return BuildDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
    )


async def test_destructive_sql_is_blocked_in_run_and_the_build_self_heals(
    sink: CollectingSink,
) -> None:
    # The FunctionModel first improvises destructive SQL (blocked → ModelRetry re-enters the
    # loop), then self-corrects to a non-destructive verification — the plan's integration
    # scenario. The destructive command must NEVER reach the exec transport.
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="", stderr="", exit=0))
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [
            tool_turn(
                "run_command", {"command": ["psql", _FAKE_DSN, "-c", "DELETE FROM visitors"]}
            ),
            tool_turn("run_command", {"command": ["npx", "tsc", "--noEmit"]}),
            text_turn("verified without touching data"),
        ],
        captured,
    )
    result = await build_agent.run("tweak the app", deps=_deps(fake, sink), model=model)
    # Only the benign verification ever executed.
    assert fake.command_calls == [["npx", "tsc", "--noEmit"]]
    # The corrective refusal was fed back to the model in-run.
    all_incoming = "\n".join(captured.get("incoming", []))
    assert "blocked" in all_incoming.lower()
    assert result.output == "verified without touching data"
    # The blocked attempt is visible in the progress trace (the iteration-2 tripwire relies
    # on route-around behaviour being observable).
    assert any("blocked" in str(envelope).lower() for envelope in sink.events)
