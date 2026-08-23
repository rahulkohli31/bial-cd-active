"""The open-sandbox system prompt + repair template (U3) — a cheap, COARSE guard against prompt
drift on the load-bearing bits (R18): the injected ENV, the don't-restart-dev-server rule, the SAS
server-side rule, and the real-data-only rule (R4). Prompt copy is not behavioral, so the
assertions stay loose to avoid brittleness."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.api.v1.build_sessions.schemas import BuildError, ErrorSource
from src.core.prompt_blocks import WRITE_TOOL_SURFACE
from src.db.models.conversation import ConversationMode
from src.services.agent.toolsets import (
    first_sentence,
    registered_tool_definitions,
    render_tool_surface,
)
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.prompt import BUILD_SYSTEM_PROMPT, build_repair_prompt
from src.services.orchestrator.tools import sandbox_toolset

_THE_SANDBOX_FACTORY = "src.services.agent.toolsets.sandbox_toolset"
"""The name `toolsets_for_mode` reaches the sandbox six through — the seam the two
deliberate mutations below swap out. Patched by dotted path so the test never has to
reach through the registry module for a name it only re-imports."""

_SandboxOf = Callable[[RunContext[Any]], SandboxSession]
"""The accessor shape `sandbox_toolset` takes — spelled once so the mutation wrappers below
can wrap the real factory without a type suppression."""

# Repo-root/sandbox/template — the hand-maintained golden template the manifest mirrors (KD-10).
# test file: backend/tests/services/orchestrator/test_prompt.py → parents[4] is the repo root.
_TEMPLATE_ROOT = Path(__file__).resolve().parents[4] / "sandbox" / "template"


def test_system_prompt_reflects_the_open_sandbox_model() -> None:
    prompt = BUILD_SYSTEM_PROMPT
    lowered = prompt.lower()
    # Retired constrained-model language is gone (R18).
    assert "no shell or command access" not in lowered
    assert "never run `npm install`" not in lowered
    assert "single swappable module" not in lowered
    # The open model is documented: a real shell + on-demand install + the new tool.
    assert "run_command" in prompt
    assert "npm install" in prompt  # now a capability, not a prohibition
    # The injected app ENV the model writes its own data/storage code against (R20). The old
    # shared-data-service pair is deliberately NOT here: the prompt stopped teaching it in U4,
    # and U6 retired the injection itself (ADR-0028).
    for name in (
        "BIAL_APP_ID",
        "BIAL_DATABASE_URL",
        "BIAL_BLOB_CONTAINER_URL",
        "BIAL_BLOB_SAS",
    ):
        assert name in prompt
    # The dev server must still NOT be restarted (load-bearing for the harness verify).
    assert "already running" in lowered and "restart" in lowered
    # The write-capable SAS is flagged server-side-only (R13/R14).
    assert "server-side" in lowered
    assert "declare_done" in prompt


def test_system_prompt_forbids_seeded_dummy_data() -> None:
    """R4 — the build agent must never seed invented records; it builds honest empty/loading/error
    states and lets real data arrive by upload or user entry. This rule existed in the POC prompt,
    was lost in the open-sandbox rewrite, and is a client-collateral promise."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    assert "data integrity" in lowered
    # The prohibition names the whole family of invented-record words the model reaches for.
    for banned in ("dummy", "sample", "fake", "mock", "placeholder"):
        assert banned in lowered, f"the rule should name {banned!r} records explicitly"
    assert "never hardcode, seed, or generate" in lowered
    # The prescribed alternative: honest states, real data by upload or entry.
    assert "empty state" in lowered
    assert "loading state" in lowered
    assert "error state" in lowered
    assert "uploads it" in lowered and "enters it" in lowered


def test_data_integrity_is_truthful_and_carries_the_never_mutate_rule() -> None:
    """U1/R1 (#12) — the walkthrough's prompt findings: the old "ships with NO data" claim was
    FALSE for a change build against a live database (it licensed the model to treat rows as
    disposable), and no rule forbade improvised mutations. The rewrite must state the truthful
    may-hold-records reality, the never-mutate verification rule, and the feature-removal
    condition for drops (migrations are the sanctioned channel — no additive-only gate)."""
    from src.services.orchestrator.prompt import DATA_INTEGRITY_RULES

    lowered = BUILD_SYSTEM_PROMPT.lower()
    # The false claim is gone.
    assert "ships with no data" not in lowered
    # The truthful claim + the never-mutate rule + the sanctioned-drop condition are present.
    assert "may already hold" in lowered
    assert "truncate" in lowered
    assert "type-checking and rendering" in lowered
    assert "requirements remove that feature" in lowered
    assert "say so plainly" in lowered
    # Single source: the rules block is the reusable constant (U9 composes it into Write mode).
    assert DATA_INTEGRITY_RULES in BUILD_SYSTEM_PROMPT


def test_system_prompt_carries_the_generated_app_quality_rules() -> None:
    """U1/U11 (#46/#47/#45) — the additive rules the generated apps inherit: AFTER A WRITE (the
    user sees their own mutation without a reload), HONEST UI (no false "live"/"shared" claims
    without a real refetch), REMOVE SCAFFOLDING (ship only the requested feature), and RESPONSIVE
    (no horizontal overflow at 390px). Coarse marker check — the copy is a probabilistic nudge,
    not a behavioral contract, so assert the load-bearing phrases only."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    # AFTER A WRITE (U11): the unconditional own-mutation refetch, hoisted out of HONEST UI.
    assert "after a write" in lowered
    # HONEST UI (#46): names the no-realtime reality and the required refetch remedy.
    assert "honest ui" in lowered
    assert "no realtime channel" in lowered
    assert "refetch" in lowered
    # REMOVE SCAFFOLDING (#47): the model must ship only what the user asked for.
    assert "remove scaffolding" in lowered
    # RESPONSIVE (#45): the concrete phone-width target, not a vague "make it responsive".
    assert "responsive" in lowered
    assert "390px" in lowered


def test_system_prompt_tells_the_agent_who_is_reading() -> None:
    """U15 / R20 / R22 — the build prompt narrates to a citizen, so it carries the audience block
    from the one shared source (`prompt_blocks.NARRATION_VOICE`) that the live Write segment also
    composes. Counted, not merely present: the block reaches this prompt through
    `BUILD_WORKING_RULES_TAIL`, so a future author naming it again beside the TAIL would emit the
    whole rule twice here and stutter at the model."""
    from src.core.prompt_blocks import NARRATION_VOICE

    assert NARRATION_VOICE in BUILD_SYSTEM_PROMPT
    assert BUILD_SYSTEM_PROMPT.count(NARRATION_VOICE) == 1
    assert BUILD_SYSTEM_PROMPT.count("A couple of lines at each milestone") == 1
    lowered = BUILD_SYSTEM_PROMPT.lower()
    assert "talking to the user" in lowered
    assert "plain, everyday words" in lowered
    # The register holds on the turns where jargon actually leaks — the failures (R20).
    assert "when something goes wrong" in lowered


def _completion_block(prompt: str) -> str:
    """The COMPLETION paragraph, sliced out of the composed prompt.

    SLICED RATHER THAN SEARCHED WHOLE-PROMPT, because the retired phrasing this unit removes
    ("type-check the app") is legitimate copy elsewhere: `BUILD_WORKING_RULES_HEAD` still tells
    the model the harness type-checks after every turn, which is TRUE and must stay. A
    prompt-wide `not in` would either go permanently red on that true sentence or have to be
    weakened until it proved nothing."""
    return prompt[prompt.index("COMPLETION \u2014") :].split("\n\n", 1)[0]


def test_completion_promises_no_round_trip_after_declare_done() -> None:
    """★ U18 / R30 — THE PROMPT MOVED WITH THE BEHAVIOUR, WHICH IS THE WHOLE POINT.

    `declare_done` is terminal on a passing check now. A model still told "the harness then
    verifies … if it is not green yet you will receive the diagnostic" reads that as an
    invitation to write its closing message in the reply that follows — a reply this unit has
    just stopped buying. It would then withhold that message from `summary`, the harness would
    render the fallback, and the citizen would end a working build on a generic sentence while
    the good one was thrown away with the round-trip. The stale instruction does not merely
    mislead here; it defeats the feature.

    TWO HALVES, DELIBERATELY. The inertness half searches for the retired phrasing and requires
    zero hits. The liveness half requires the repair arm's promise to still be there, because it
    is still TRUE (ASM14) — and an inertness guard alone would pass just as happily against a
    COMPLETION block someone had deleted outright.

    Asserted on the COMPOSED prompt rather than on `prompt_blocks`, so a composition site that
    stopped including the block would be caught here too. The Write mode segment composes the
    same single source (`BUILD_WORKING_RULES_TAIL`), which is what makes one assertion enough."""
    completion = _completion_block(BUILD_SYSTEM_PROMPT)

    # INERTNESS — the retired round-trip promise, gone.
    for retired in (
        "The harness then verifies",
        "if it is not green yet",
        "type-check",
    ):
        assert retired not in completion, f"{retired!r} still promises a follow-up round-trip"

    # THE TERMINAL CONDITION, said out loud and said conditionally (the verdict still decides).
    assert "ENDS THE TURN" in completion
    assert "passing check" in completion

    # R22/AE13 — and what the summary must BE, since it is now the last thing the user reads.
    assert "the last thing the user reads" in completion
    assert "what they can now do" in completion
    assert "no file names, commands, libraries or frameworks" in completion

    # LIVENESS — the repair arm's promise is unchanged and still made.
    assert "does NOT check out you will receive the diagnostic" in completion
    assert "Do not declare done prematurely" in completion


def test_the_type_check_is_prohibited_not_merely_unnecessary() -> None:
    """★ U19 / R25 — THE INVITATION IS NOW A PROHIBITION.

    The line used to end "That is your verification signal — you do not need to run `tsc`
    yourself, though you may." That is an invitation wearing a reassurance, and the model took it:
    it spent a slow command and a screenful of output per turn re-deriving the exact diagnostic
    the harness hands it for free the moment the turn ends. The agent does not do work the
    platform already does.

    THREE PARTS, and the third is why this is not one `not in`. The retired permission must be
    gone; the replacement must actually FORBID rather than merely omit (a block someone deleted
    passes an inertness check just as happily); and the TRUE half — the harness really does
    type-check between turns — must survive, or the model is left with no verification story at
    all and starts inventing one."""
    prompt = BUILD_SYSTEM_PROMPT
    lowered = prompt.lower()

    # INERTNESS — the permission, in either of its halves.
    assert "you do not need to run `tsc` yourself" not in lowered
    assert "though you may" not in lowered

    # THE PROHIBITION, plus the stand-in a model reaches for once `tsc` is closed off.
    assert "do not run `tsc` yourself" in lowered
    assert "do not reach for `npm run build` as a stand-in" in lowered

    # LIVENESS — the harness's own check is still described, because it is still what happens.
    assert "the harness type-checks the app (`tsc --noemit`)" in lowered


def test_completion_never_makes_type_checking_the_agents_job() -> None:
    """U19 / R25, the other half of the same rule: the closing guidance must not hand the
    verification back to the model at the last moment.

    Sliced to the COMPLETION block on purpose (see `_completion_block`): "type-check" is
    legitimate copy elsewhere in this prompt — DATA INTEGRITY prescribes verifying by
    type-checking and rendering rather than by mutating rows, and ENVIRONMENT describes what the
    harness does — so a prompt-wide search would either be permanently red or have to be watered
    down until it proved nothing."""
    completion = _completion_block(BUILD_SYSTEM_PROMPT).lower()
    assert "type-check" not in completion
    assert "tsc" not in completion
    # LIVENESS beside it — the block still says what ends the turn and what the summary must be.
    assert "declare_done" in completion
    assert "ends the turn" in completion


def test_prompt_has_no_stale_app_records_demo_reference() -> None:
    """U11/R16 — the `app/records` demo route was removed from the template (commit d51ebfa), so
    the prompt must no longer tell the model to hunt for and delete it. Only the stale REMOVE
    SCAFFOLDING parenthetical ever referenced it, and it is gone."""
    assert "app/records" not in BUILD_SYSTEM_PROMPT


def test_prompt_names_no_demonstration_data_model_or_example_component() -> None:
    """U21/R27/AE15 — two inertness guards modelled on
    `test_prompt_has_no_stale_app_records_demo_reference` just above. The template's
    demonstration data model (`items`/`item_status`/`audit_events`, the tables the old baseline
    migration created) and the deleted worked-reference component must never resurface in the
    composed prompt: the first is the demonstration table AE15 forbids the agent's first schema
    change from dropping, the second is the 271-line file the agent no longer has any reason to
    open."""
    prompt = BUILD_SYSTEM_PROMPT
    lowered = prompt.lower()
    # GUARD 1 — the deleted worked-reference component, in either spelling.
    assert "example-request-board" not in lowered
    assert "examplerequestboard" not in lowered
    # GUARD 2 — the demo table/enum identifiers the deleted baseline migration created.
    assert re.search(r"\bitems\b", lowered) is None
    assert "audit_events" not in lowered
    assert "item_status" not in lowered


def test_the_golden_template_manifest_names_no_removed_path() -> None:
    """U21/R27 — the other half of the manifest tripwire.
    `test_every_golden_template_manifest_file_exists` proves every path the manifest names still
    exists; it says nothing about a path the template used to ship staying named after it is
    deleted. Pinned separately so reverting only the manifest edit (and not the file deletions)
    still trips something."""
    manifest = BUILD_SYSTEM_PROMPT[BUILD_SYSTEM_PROMPT.index("The app starts from a minimal") :]
    for removed in ("0000_baseline.sql", "0000_snapshot.json", "example-request-board.tsx"):
        assert removed not in manifest, f"the manifest still names the removed path {removed!r}"


def test_responsive_advice_survives_the_deleted_reference_component() -> None:
    """U21/R27 — deleting `components/example-request-board.tsx` must not silently delete the
    three-patterns advice it introduced (the RESPONSIVE block used to point at the file's own
    docstring for this). TWO HALVES: the inertness half is the "copy from it, into your own
    route" sentence naming the now-deleted file; the liveness half beside it is the toolbar/
    table/form guidance itself, restated inline where the reference used to be — an inertness
    guard alone would pass just as happily against a RESPONSIVE block someone had gutted
    outright."""
    prompt = BUILD_SYSTEM_PROMPT
    lowered = prompt.lower()

    # INERTNESS — the sentence that pointed at the deleted file.
    assert "copy from it, into your own route" not in lowered
    assert "example-request-board" not in lowered

    # LIVENESS — the three patterns it taught, restated where the reference used to be.
    assert "toolbar" in lowered
    assert "flex-col sm:flex-row" in prompt
    assert "overflow-x-auto" in prompt
    assert "grid sm:grid-cols-2" in prompt


def test_the_publish_path_still_finds_a_drizzle_directory() -> None:
    """U21/ASM11 — Prerequisite 4's answer, recorded as a passing assertion rather than built as
    a guard: `backend/src/services/deploy/assets/next.config.ts`'s `outputFileTracingIncludes`
    globs `./drizzle/**`, and ASM27 keeps `drizzle/meta/_journal.json` (emptied, not deleted), so
    `drizzle/` is never an empty directory and that glob always matches something. No change to
    `next.config.ts` was needed — this just proves the premise still holds against the actual
    template tree.

    Checked as FILES, not merely directory entries: `rglob("*")` also yields the empty `meta/`
    subdirectory itself, which would make this pass even with the journal file gone — the exact
    state `./drizzle/**` needs a real file under, to actually match something."""
    drizzle_dir = _TEMPLATE_ROOT / "drizzle"
    assert drizzle_dir.is_dir()
    files = [path for path in drizzle_dir.rglob("*") if path.is_file()]
    assert files, "drizzle/ has no files — the outputFileTracingIncludes glob would match nothing"
    assert (drizzle_dir / "meta" / "_journal.json") in files


def test_app_boots_with_the_emptied_journal_and_prints_no_migration_failure() -> None:
    """★ AE15 / ASM27 — THE ASSERTION THAT MUST NOT BE SKIPPED.

    Runs the REAL, unmodified `scripts/db-migrate.mjs` exactly the way `npm run dev` runs it on
    every boot, against the template's actual (emptied, not deleted) `drizzle/meta/_journal.json`.
    Drizzle's vendored `readMigrationFiles` (`node_modules/drizzle-orm/migrator.js`) throws
    `Can't find meta/_journal.json file` when the journal is absent, and `db-migrate.mjs` catches
    every failure and still exits 0 by design (see its own file header) — so a missing journal
    would print that failure into every app's dev-server stdout on EVERY boot, silently, straight
    into the stream the self-heal verify tails.

    `BIAL_DATABASE_URL` points at a closed local port so this needs no live Postgres: the journal
    read happens before any database round-trip (`migrate()` calls `readMigrationFiles`
    synchronously first), so a fast, deterministic connection-refused failure AFTER a successful
    journal read is exactly the boundary this test is pinned to.

    Mutation-checked by hand: with `meta/_journal.json` renamed away, this identical invocation
    prints `Can't find meta/_journal.json file` to stderr and still exits 0 — the regression this
    guards against. Skipped, not failed, when `node` is not on PATH, so an environment gap can
    never read as a pass; it is not skipped when the environment (this repo's checked-in
    `sandbox/template/node_modules`) is present, which it is."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH — cannot boot the template's db-migrate.mjs")

    script = _TEMPLATE_ROOT / "scripts" / "db-migrate.mjs"
    result = subprocess.run(
        [node, str(script)],
        env={**os.environ, "BIAL_DATABASE_URL": "postgres://u:p@127.0.0.1:1/nonexistent"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0  # db-migrate.mjs always exits 0 — see its own file header.
    assert "Can't find meta/_journal.json" not in output, (
        "the journal read failed — see ASM27: this is what prints a migration failure into "
        f"every app's dev-server stdout on every boot. Full output:\n{output}"
    )
    # LIVENESS — the run actually reached the migrate step (the env var arrived at the
    # subprocess and the connection was attempted), or the inertness assertion above would pass
    # vacuously against a run that never got that far.
    assert "migrations failed" in output.lower() or "migrations up to date" in output.lower()


def test_prompt_teaches_the_drizzle_migration_discipline() -> None:
    """U4/R5 — the app owns its schema through Drizzle, and the migration files are the only
    thing that carries that schema across a snapshot restore. Three load-bearing claims:
    `generate` writes a versioned file, the files under `drizzle/` stay in the workspace, and the
    schema-mutating `push` shortcut is banned (it applies changes with no migration file, so a
    restore returns code that expects tables the database does not have)."""
    prompt = BUILD_SYSTEM_PROMPT
    lowered = prompt.lower()
    assert "db/schema.ts" in prompt
    assert "drizzle-kit" in prompt and "generate" in lowered
    assert "db:migrate" in prompt
    # The ban is doctrine — and it is stated WITHOUT the literal command, so a repo-wide
    # `grep "drizzle-kit push"` stays a clean "nothing invokes it" check.
    assert "`push` command" in prompt
    assert "drizzle-kit push" not in prompt
    # The DSN is server-only and must never be copied into a file the snapshot carries.
    assert "next_public_" in lowered
    assert "server-side from `process.env`" in prompt


def test_prompt_makes_no_claim_that_the_starter_ships_demo_routes() -> None:
    """U11/R16 — the template ships only `app/{globals.css,layout.tsx,page.tsx}` plus lib/config,
    NO example or demo routes. REMOVE SCAFFOLDING's old premise ("the starter ships example and
    demo routes") is false and must not send the model hunting scaffolding that does not exist."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    assert "example and demo routes" not in lowered
    assert "ships example" not in lowered


def test_refetch_after_every_write_appears_exactly_once() -> None:
    """U11/R15 — exactly ONE rule owns the after-write mechanic. The clause was hoisted OUT of
    HONEST UI into the unconditional AFTER A WRITE rule; if a future edit re-adds it to HONEST UI
    (two rules prescribing the same behaviour) this count trips."""
    assert BUILD_SYSTEM_PROMPT.lower().count("refetch after every write") == 1


def test_after_write_requirement_is_unconditional() -> None:
    """U11/R14 — the after-write refetch applies to EVERY app, not only ones that claim liveness.
    It lives in its own AFTER A WRITE rule, above HONEST UI, and is NOT gated behind the
    live/shared/real-time conditional that owns interval/focus refetch."""
    prompt = BUILD_SYSTEM_PROMPT
    after_write = prompt[prompt.index("AFTER A WRITE") : prompt.index("HONEST UI")].lower()
    honest_ui = prompt[prompt.index("HONEST UI") : prompt.index("REMOVE SCAFFOLDING")].lower()
    assert "refetch after every write" in after_write
    # The AFTER A WRITE rule carries no "if your copy claims ..." guard — it is unconditional.
    assert "if your copy" not in after_write
    # And HONEST UI no longer owns the mechanic — it moved out.
    assert "refetch after every write" not in honest_ui


def test_honest_ui_keeps_its_claim_matching_argument() -> None:
    """U11 — HONEST UI keeps ONLY the claim-matching argument: interval and window-focus refetch
    stay CONDITIONAL on actually claiming a view is live/shared/real-time (the price of the
    claim), which is distinct from the unconditional own-mutation rule."""
    prompt = BUILD_SYSTEM_PROMPT
    honest_ui = prompt[prompt.index("HONEST UI") : prompt.index("REMOVE SCAFFOLDING")].lower()
    assert "if your copy" in honest_ui
    assert "interval" in honest_ui
    assert "focus" in honest_ui
    assert "real-time" in honest_ui


def test_every_golden_template_manifest_file_exists() -> None:
    """U11/R16 durable guard: every path the manifest advertises as an editable starting point
    must actually exist under `sandbox/template/`, so a future template change that drops or
    renames a file cannot leave the prompt pointing at a phantom. Walks the manifest text in the
    rendered prompt and stats each path — the `components/ui/*.tsx` glob and the comma-list line
    included.

    `sql` is in the extension set on purpose: the generated migrations under `drizzle/` are the
    one manifest entry that is BUILT rather than hand-written, so it is the entry most likely to
    go missing (an over-eager `.gitignore` line, a fresh clone). Without `sql` here the manifest
    could advertise a migrations directory that does not exist and nothing would notice."""
    manifest = BUILD_SYSTEM_PROMPT[BUILD_SYSTEM_PROMPT.index("The app starts from a minimal") :]
    tokens = re.findall(r"[\w./*-]+\.(?:tsx|ts|css|json|mjs|sql)", manifest)
    assert tokens, "manifest path extraction found nothing — the regex drifted from the manifest"
    for token in tokens:
        if "*" in token:
            assert list(_TEMPLATE_ROOT.glob(token)), (
                f"manifest glob {token!r} matched no file under {_TEMPLATE_ROOT}"
            )
        else:
            assert (_TEMPLATE_ROOT / token).is_file(), (
                f"manifest lists {token!r} but it is missing from {_TEMPLATE_ROOT}"
            )


def test_system_prompt_never_instructs_the_app_to_authenticate() -> None:
    """Regression guard for the opaque-origin sandbox learning
    (docs/solutions/architecture-patterns/sandboxed-app-auth-session-injection-2026-07-09.md):
    the host owns authentication and injects identity downward. A prompt that tells generated code
    to sign users in produces an in-sandbox login form that can never reach an auth endpoint from
    `origin: null`. The prompt is part of the trust boundary — keep sign-in out of it entirely."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    for banned in (
        "login",
        "log in",
        "sign in",
        "sign-in",
        "signin",
        "username",
        "password",
        "authenticate",
        "authentication",
    ):
        assert banned not in lowered, f"the prompt must not instruct the app about {banned!r}"


def test_repair_prompt_embeds_the_redacted_diagnostic() -> None:
    error = BuildError(
        source=ErrorSource.TSC,
        title="app/records/page.tsx(12,5): error TS2322: Type mismatch.",
        cleaned_stack="app/records/page.tsx(12,5): error TS2322: Type mismatch.",
    )
    repair = build_repair_prompt(error)
    assert "tsc" in repair
    assert error.title in repair
    assert error.cleaned_stack in repair
    assert "declare_done" in repair
    # The retired 'do not run any commands' line is gone — run_command exists now (R18).
    assert "run any commands" not in repair.lower()


# F4 — TWO MODEL-FACING SOURCES TELL THE MODEL HOW TO CHANGE THE SCHEMA, and they must agree.
# The re-test patched the build prompt and missed the SQL sentinel's refusal, so the model was
# corrected by one voice and mis-taught by the other. That is the half-landed-fix failure mode
# this file's assertion exists to make impossible.


def _database_block(prompt: str) -> str:
    """The DATABASE paragraph, sliced out of the composed prompt — the one place the migration
    loop is taught. Sliced for the same reason `_completion_block` is: `--name`, `generate` and
    `rename` are legitimate copy elsewhere (the sentinel's refusal quotes the whole command),
    so a prompt-wide search would prove nothing about the sentence under test."""
    return prompt[prompt.index("DATABASE \u2014") :].split("\n\n", 1)[0]


def test_both_model_facing_sources_prescribe_the_same_named_generate() -> None:
    from src.core.prompt_blocks import MIGRATION_GENERATE_CMD
    from src.services.orchestrator.sql_guard import _refusal

    refusal = _refusal("DELETE without WHERE")
    assert MIGRATION_GENERATE_CMD in refusal
    assert "--name" in MIGRATION_GENERATE_CMD
    # The build prompt teaches the same flag, in its own argv spelling.
    assert '"--name"' in BUILD_SYSTEM_PROMPT
    # Neither source may prescribe the BARE generate — not because it hangs (it does not; see
    # the test below), but because it names the file at random and the two voices must match.
    assert "`npx drizzle-kit generate`" not in refusal


_A_GENERATE_SPELLING = re.compile(r"drizzle-kit[\"\',\s]+generate(.{0,24})")


def test_every_generate_the_model_reads_carries_the_name_flag() -> None:
    """★ U20 / ASM28 — ONE SPELLING, EVERYWHERE THE MODEL LOOKS.

    The TOOL SURFACE block used to carry `["npx","drizzle-kit","generate"]` as its `run_command`
    example — the bare spelling the DATABASE block forbids two blocks earlier, in the same
    prompt. The block is generated from the tools' own docstrings now, so the contradicting
    example is gone rather than corrected; this pins that no future edit re-adds one."""
    spellings = _A_GENERATE_SPELLING.findall(BUILD_SYSTEM_PROMPT)
    assert spellings, "the prompt stopped teaching the generate command at all"
    for tail in spellings:
        assert "--name" in tail, f"a bare `drizzle-kit generate` survives in the prompt: {tail!r}"


def test_the_template_offers_no_second_spelling_of_the_generate_command() -> None:
    """★ U20 / ASM28, the template half. `package.json` shipped `"db:generate": "drizzle-kit
    generate"` — the bare spelling again, this time as a script the model could reach for by
    name. It cannot be repaired in place (`--name <what_changed>` needs a value per invocation,
    which no fixed npm script can carry), so the script is gone and
    `MIGRATION_GENERATE_CMD` is the single spelling. Written as a rule rather than an absence
    so re-adding it correctly would pass and re-adding it bare would not."""
    scripts = json.loads((_TEMPLATE_ROOT / "package.json").read_text(encoding="utf-8"))["scripts"]
    for name, script in scripts.items():
        if "drizzle-kit generate" in script:
            assert "--name" in script, f"`{name}` ships the bare generate the prompt forbids"
    # LIVENESS beside it — the script the prompt DOES name must still be there.
    assert "db:migrate" in scripts


def test_the_name_flag_claims_only_what_the_flag_actually_does() -> None:
    """★ U20 / R26 / ASM28 — THE FLIPPED CLAIM.

    This sentence used to read "ALWAYS pass `--name`: without it the command PROMPTS when the
    diff is ambiguous ... so it hangs until it is killed." A smoke against the template's pinned
    `drizzle-kit@0.31.10` says otherwise: a bare generate over an unambiguous diff exits 0 and
    writes `drizzle/0001_special_fantastic_four.sql` — a RANDOM NAME, not a hang. The flag stays
    (a migration history nobody can read is a real cost) but it may only claim what it buys, or
    the model reasons from a mechanism that does not exist."""
    database = _database_block(BUILD_SYSTEM_PROMPT)
    name_rule = database[database.index("ALWAYS pass") :].split("\n", 1)[0]

    # INERTNESS — the hang, and the ambiguity mechanism, are not this sentence's business.
    assert "hang" not in name_rule.lower()
    assert "prompts" not in name_rule.lower()

    # THE REAL COST, stated concretely enough to be checkable.
    assert "named at random" in name_rule
    assert "--name" in name_rule


def test_the_prompt_teaches_the_split_that_actually_unblocked_the_wedged_build() -> None:
    """★ U20 / ASM28 — the one-change-per-generate rule now owns the REAL reason.

    Verified by smoke, both ways round: under a TTY the rename resolver ("is `label` created, or
    renamed from `title`?") waits forever — that is the 4m09s stall — and `--name` does not
    answer it. Under the sandbox's real `stdin=DEVNULL` it is worse: drizzle-kit prints
    "Interactive prompts require a TTY terminal" to stderr, writes no migration, and EXITS 0. A
    model taught only "it hangs" reads that zero exit as success and builds on a schema change
    that never happened, so the zero exit is the half that must be said out loud."""
    database = _database_block(BUILD_SYSTEM_PROMPT).lower()
    assert "one kind of change per generate" in database
    assert "rename" in database
    # The mechanism: an interactive question, and no flag answers it.
    assert "asks" in database
    assert "no flag answers" in database
    # …and the failure MODE, which is the part that actually costs a build.
    assert "no migration file" in database
    assert "zero exit code" in database


def test_the_drizzle_artifacts_instruction_is_emitted_exactly_once() -> None:
    """U20 — the same rule was printed twice in one prompt: once in the golden-template manifest
    (`drizzle/*.sql … versioned artifacts that must stay in the workspace`) and once in the
    DATABASE block. Counting is the point — an `in` assertion is green at one copy and at two."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    assert lowered.count("versioned artifacts") == 1
    assert lowered.count("travel with the snapshot") == 1
    # LIVENESS — the surviving copy is the DATABASE one, which carries the extra rule.
    assert "never hand-edit one that has already been applied" in lowered


# --- U20 / R26: the TOOL SURFACE block is GENERATED, and this is the check that keeps it so ---
#
# R26 asks for a check that fails when a DESCRIBED behaviour and the actual behaviour diverge.
# A name-set comparison cannot make that promise, and U18 is the proof: it changed what
# `declare_done` does while the sentence describing it still promised a follow-up round-trip, and
# every name-based assertion in this repo stayed green. So the block is rendered from the tool
# definitions pydantic-ai hands the model at registration, and the drift check is a snapshot
# assertion over that rendering plus a per-mode membership assertion against `toolsets_for_mode`.


def _tool_surface_block(prompt: str) -> str:
    """The TOOL SURFACE block, sliced out of the composed prompt."""
    return prompt[prompt.index("TOOL SURFACE:") :].split("\n\n", 1)[0]


async def _the_drift_check() -> None:
    """THE DRIFT CHECK ITSELF, factored out so the mutation tests can require it to go RED.

    An equality assertion proves the snapshot is right today; it does not prove the assertion
    would notice if it stopped being — which is exactly the property that failed under U18. The
    two mutation tests below run THIS function against a deliberately-mutated registry."""
    generated = await render_tool_surface(ConversationMode.WRITE)
    assert WRITE_TOOL_SURFACE == generated, (
        "the TOOL SURFACE block in `core/prompt_blocks.py` no longer matches the tools the Write "
        "arm registers. Regenerate it with the one-liner in `services/agent/toolsets.py`'s U20 "
        f"comment and paste the result over `WRITE_TOOL_SURFACE`.\n\ngenerated:\n{generated}"
    )


async def test_the_tool_surface_is_generated_from_the_tools_the_write_arm_registers() -> None:
    """★ The snapshot half. Counted in the composed prompt as well, because a block that reached
    zero composition sites would satisfy the equality assertion perfectly well."""
    await _the_drift_check()
    assert BUILD_SYSTEM_PROMPT.count(WRITE_TOOL_SURFACE) == 1


async def test_the_prompts_tool_list_is_exactly_what_the_write_arm_registers() -> None:
    """★ THE MEMBERSHIP HALF, asserted against `toolsets_for_mode` rather than a hand-kept list.

    The hand-written block named six tools while the Write arm handed the model eight: the two
    structured reads it borrows off `read_only_toolset` (`_WRITE_STRUCTURED_READS`) were absent
    from the prompt for their entire life, so the model was never told it could list or search
    the tree and paid for that in `run_command` round-trips."""
    registered = set(await registered_tool_definitions(ConversationMode.WRITE))
    named = set(re.findall(r"^- `(\w+)` \u2014 ", _tool_surface_block(BUILD_SYSTEM_PROMPT), re.M))
    assert named == registered
    # The two the old prose omitted, named explicitly so the failure reads as itself.
    assert {"list_files", "search_files"} <= named
    # …and nothing from a mode Write is not: Plan's confirmation tool is uncallable here.
    assert "present_plan_options" not in named


async def test_every_tool_line_is_its_registered_descriptions_first_sentence() -> None:
    """★ THE DESCRIPTION HALF — the one a name-set comparison cannot make.

    Each line must be the tool's OWN words, not a paraphrase of them, so the prompt and the tool
    schema cannot say different things about the same tool."""
    for name, definition in (await registered_tool_definitions(ConversationMode.WRITE)).items():
        assert definition.description, f"`{name}` reaches the model with no description"
        line = f"- `{name}` \u2014 {first_sentence(definition.description)}"
        assert line in BUILD_SYSTEM_PROMPT, f"the prompt paraphrases `{name}`; expected {line!r}"


async def test_the_drift_check_fails_when_a_tool_joins_a_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ DELIBERATE MUTATION 1 — a tool is added to Write and nobody regenerates the block.

    Verified by mutating, not by reading: the check has to be shown failing, or "it would catch
    that" is a claim about code nobody ran."""
    registers_the_six = sandbox_toolset

    def registers_a_seventh(sandbox_of: _SandboxOf) -> FunctionToolset[Any]:
        toolset = registers_the_six(sandbox_of)

        async def summon_a_pony(_ctx: RunContext[Any]) -> str:
            """Summon a pony into the workspace."""
            return "neigh"

        toolset.add_function(summon_a_pony)
        return toolset

    monkeypatch.setattr(_THE_SANDBOX_FACTORY, registers_a_seventh)
    assert "summon_a_pony" in await render_tool_surface(ConversationMode.WRITE)
    with pytest.raises(AssertionError, match="no longer matches the tools"):
        await _the_drift_check()


async def test_the_drift_check_fails_when_a_tools_docstring_is_reworded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ DELIBERATE MUTATION 2 — the same tools, one of them described differently. THIS is the
    assertion U18 proves is necessary, so the mutation is U18's own regression put back: a
    `declare_done` that promises the follow-up round-trip the harness stopped buying. Every
    name-based check in this repo is green against it; this one is not."""
    registers_the_six = sandbox_toolset

    def describes_declare_done_the_old_way(sandbox_of: _SandboxOf) -> FunctionToolset[Any]:
        toolset = registers_the_six(sandbox_of)
        toolset.tools["declare_done"].description = (
            "Declare the build finished. The harness then verifies the app, and if it is not "
            "green yet you will receive the diagnostic and can carry on."
        )
        return toolset

    monkeypatch.setattr(_THE_SANDBOX_FACTORY, describes_declare_done_the_old_way)
    reworded = await render_tool_surface(ConversationMode.WRITE)
    # Same eight tools — a membership check sees nothing at all here.
    assert set(re.findall(r"^- `(\w+)`", reworded, re.M)) == set(
        re.findall(r"^- `(\w+)`", WRITE_TOOL_SURFACE, re.M)
    )
    with pytest.raises(AssertionError, match="no longer matches the tools"):
        await _the_drift_check()


async def test_a_tool_without_a_docstring_fails_the_render_rather_than_shipping_blank() -> None:
    """Fail-first: a tool registered with no description would otherwise reach the prompt as
    `- \u0060thing\u0060 \u2014 ` and reach the model with no explanation either."""
    registers_the_six = sandbox_toolset

    def registers_a_mute_tool(sandbox_of: _SandboxOf) -> FunctionToolset[Any]:
        toolset = registers_the_six(sandbox_of)
        toolset.tools["declare_done"].description = None
        return toolset

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_THE_SANDBOX_FACTORY, registers_a_mute_tool)
        with pytest.raises(ValueError, match="registered with no description"):
            await render_tool_surface(ConversationMode.WRITE)


async def test_run_commands_dev_server_rule_is_registered_copy_as_well_as_prompt_copy() -> None:
    """★ THE LIVENESS GUARD ON THE DOCSTRINGS THEMSELVES (U14 / U19 carried into U20).

    The docstrings are prompt text now, so trimming one is a prompt edit. This sentence is what
    covers the agent starting a dev server through `/exec` without U14's marker — the supervisor's
    child env carries nothing that would tell a second `next dev` apart from the real one — so it
    must survive in BOTH voices: the tool's own description, and the ENVIRONMENT block."""
    definitions = await registered_tool_definitions(ConversationMode.WRITE)
    described = (definitions["run_command"].description or "").lower()
    assert "do not start or restart the dev server" in described
    assert "already running" in described
    # And the prompt's own wording (U19's guard, restated here because the two travel together).
    assert "do not start, restart, or kill it" in BUILD_SYSTEM_PROMPT.lower()
