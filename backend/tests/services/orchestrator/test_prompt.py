"""The open-sandbox system prompt + repair template — a cheap, COARSE guard against prompt
drift on the load-bearing bits: the injected ENV, the don't-restart-dev-server rule, the SAS
server-side rule, and the real-data-only rule. Prompt copy is not behavioral, so the
assertions stay loose to avoid brittleness.

THE SUBJECT MOVED, THE BLOCKS DID NOT. These tests were written against the standalone build
harness's own system prompt, assembled from exactly the `core/prompt_blocks.py` pieces
`agent/mode_prompts._WRITE_SEGMENT` composes for a Build chat turn. The harness and its prompt
are deleted; the blocks and the composition were not, and the composed Build chat prompt is now
the one live carrier of every rule they pinned — so the file asserts against `_BUILD_PROMPT`
(`compose_kind_prompt(ChatKind.BUILD, …)`), the prompt a citizen's Build turn actually receives.

`tests/services/agent/test_mode_prompts.py` owns the COMPOSITION properties (BASE + one segment,
each block emitted exactly once, what Plan may not carry). This file owns what the Write blocks
must SAY: the golden-template manifest, the Drizzle/migration discipline, the DATABASE /
COMPLETION / TOOL SURFACE blocks, and the template filesystem those blocks describe."""

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
from src.core.prompt_blocks import APPLY_SCHEMA_CHANGE_TOOL, WRITE_TOOL_SURFACE
from src.db.models.conversation import ChatKind
from src.services.agent.mode_prompts import PromptContext, compose_kind_prompt
from src.services.agent.toolsets import (
    first_sentence,
    registered_tool_definitions,
    render_tool_surface,
)
from src.services.messages.projection import CONNECTOR_SCHEMA_TOOL
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.prompt import build_repair_prompt
from src.services.orchestrator.tools import sandbox_toolset
from tests.fakes import a_connected_system

_BUILD_PROMPT = compose_kind_prompt(
    ChatKind.BUILD,
    PromptContext(user_name="Asha", project_name="Visitor Log", project_description=None),
)
"""The prompt a Build chat turn is actually given, composed ONCE for the whole module.

The context values are arbitrary — nothing below asserts on the user or project name; they exist
because `_base()` needs them to build the identity paragraph. `project_description=None` keeps the
composed text to the blocks these tests are about.

Composed at import rather than per test because it is a pure function of the block constants: a
fixture would re-run the same string builder ~45 times and let a reader think the prompt varied
per test."""


_THE_SANDBOX_FACTORY = "src.services.agent.toolsets.sandbox_toolset"
"""The name `toolsets_for_kind` reaches the sandbox six through — the seam the two
deliberate mutations below swap out. Patched by dotted path so the test never has to
reach through the registry module for a name it only re-imports."""

_SandboxOf = Callable[[RunContext[Any]], SandboxSession]
"""The accessor shape `sandbox_toolset` takes — spelled once so the mutation wrappers below
can wrap the real factory without a type suppression."""

# Repo-root/sandbox/template — the hand-maintained golden template the manifest mirrors.
# test file: backend/tests/services/orchestrator/test_prompt.py → parents[4] is the repo root.
_TEMPLATE_ROOT = Path(__file__).resolve().parents[4] / "sandbox" / "template"


def test_system_prompt_reflects_the_open_sandbox_model() -> None:
    prompt = _BUILD_PROMPT
    lowered = prompt.lower()
    # Retired constrained-model language is gone.
    assert "no shell or command access" not in lowered
    assert "never run `npm install`" not in lowered
    assert "single swappable module" not in lowered
    # The open model is documented: a real shell + on-demand install + the new tool.
    assert "run_command" in prompt
    assert "npm install" in prompt  # now a capability, not a prohibition
    # The injected app ENV the model writes its own data/storage code against. The old
    # shared-data-service pair is deliberately NOT here: once generated apps got their own
    # per-project database, the prompt stopped teaching that pair, and the runtime stopped
    # injecting it.
    for name in (
        "BIAL_APP_ID",
        "BIAL_DATABASE_URL",
        "BIAL_BLOB_CONTAINER_URL",
        "BIAL_BLOB_SAS",
    ):
        assert name in prompt
    # The dev server must still NOT be restarted (load-bearing for the harness verify).
    assert "already running" in lowered and "restart" in lowered
    # The write-capable SAS is flagged server-side-only.
    assert "server-side" in lowered
    assert "declare_done" in prompt


def test_system_prompt_forbids_seeded_dummy_data() -> None:
    """The build agent must never seed invented records; it builds honest empty/loading/error
    states and lets real data arrive by upload or user entry. This rule existed in the POC prompt,
    was lost in the open-sandbox rewrite, and is a client-collateral promise."""
    lowered = _BUILD_PROMPT.lower()
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
    """The old "ships with NO data" claim was FALSE for a change build against a live
    database (it licensed the model to treat rows as disposable), and no rule forbade
    improvised mutations. The rewrite must state the truthful may-hold-records reality, the
    never-mutate verification rule, and the feature-removal condition for drops (migrations
    are the sanctioned channel — no additive-only gate)."""
    from src.core.prompt_blocks import DATA_INTEGRITY_RULES

    lowered = _BUILD_PROMPT.lower()
    # The false claim is gone.
    assert "ships with no data" not in lowered
    # The truthful claim + the never-mutate rule + the sanctioned-drop condition are present.
    assert "may already hold" in lowered
    assert "truncate" in lowered
    assert "type-checking and rendering" in lowered
    assert "requirements remove that feature" in lowered
    assert "say so plainly" in lowered
    # Single source: the rules block is the reusable constant that Write mode's prompt
    # composes too.
    assert DATA_INTEGRITY_RULES in _BUILD_PROMPT


def test_system_prompt_carries_the_generated_app_quality_rules() -> None:
    """The additive rules the generated apps inherit: AFTER A WRITE (the
    user sees their own mutation without a reload), HONEST UI (no false "live"/"shared" claims
    without a real refetch), REMOVE SCAFFOLDING (ship only the requested feature), and RESPONSIVE
    (no horizontal overflow at 390px). Coarse marker check — the copy is a probabilistic nudge,
    not a behavioral contract, so assert the load-bearing phrases only."""
    lowered = _BUILD_PROMPT.lower()
    # AFTER A WRITE: the unconditional own-mutation refetch, hoisted out of HONEST UI.
    assert "after a write" in lowered
    # HONEST UI: names the no-realtime reality and the required refetch remedy.
    assert "honest ui" in lowered
    assert "no realtime channel" in lowered
    assert "refetch" in lowered
    # REMOVE SCAFFOLDING: the model must ship only what the user asked for.
    assert "remove scaffolding" in lowered
    # RESPONSIVE: the concrete phone-width target, not a vague "make it responsive".
    assert "responsive" in lowered
    assert "390px" in lowered


def _completion_block(prompt: str) -> str:
    """The COMPLETION paragraph, sliced out of the composed prompt.

    SLICED RATHER THAN SEARCHED WHOLE-PROMPT, because the retired phrasing this unit removes
    ("type-check the app") is legitimate copy elsewhere: `BUILD_WORKING_RULES_HEAD` still tells
    the model the harness type-checks after every turn, which is TRUE and must stay. A
    prompt-wide `not in` would either go permanently red on that true sentence or have to be
    weakened until it proved nothing."""
    return prompt[prompt.index("COMPLETION \u2014") :].split("\n\n", 1)[0]


def test_completion_promises_no_round_trip_after_declare_done() -> None:
    """★ THE PROMPT MOVED WITH THE BEHAVIOUR, WHICH IS THE WHOLE POINT. `declare_done` is
    terminal on a passing check now; the old wording ("...you will receive the diagnostic")
    invited the model to write its closing message in a follow-up reply this unit no longer
    buys — the good message was thrown away with the round-trip.

    TWO HALVES, DELIBERATELY. The inertness half searches for the retired phrasing and requires
    zero hits. The liveness half requires the repair arm's promise to still be there, because it
    is still TRUE — and an inertness guard alone would pass just as happily against a
    COMPLETION block someone had deleted outright.

    Asserted on the COMPOSED prompt rather than on `prompt_blocks`, so a composition site that
    stopped including the block would be caught here too. The Write mode segment composes the
    same single source (`BUILD_WORKING_RULES_TAIL`), which is what makes one assertion enough."""
    completion = _completion_block(_BUILD_PROMPT)

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

    # And what the summary must BE, since it is now the last thing the user reads.
    assert "the last thing the user reads" in completion
    assert "what they can now do" in completion
    # THE VOCABULARY CLAUSE IS GONE, and its absence is asserted rather than merely unmentioned.
    # "with no file names, commands, libraries or frameworks in it" told the agent which WORDS
    # its closing message could not contain — a restriction on what it may say rather than on
    # who it is saying it to. What replaced it is the audience: an account of what the person
    # can now do with their app, written to the person who asked for it.
    assert "no file names, commands, libraries or frameworks" not in completion
    assert "written to the person who asked for it" in completion

    # LIVENESS — the repair arm's promise is unchanged and still made.
    assert "does NOT check out you will receive the diagnostic" in completion
    assert "Do not declare done prematurely" in completion


def test_the_type_check_is_prohibited_not_merely_unnecessary() -> None:
    """★ THE INVITATION IS NOW A PROHIBITION.

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
    prompt = _BUILD_PROMPT
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
    """The other half of the same rule: the closing guidance must not hand the
    verification back to the model at the last moment.

    Sliced to the COMPLETION block on purpose (see `_completion_block`): "type-check" is
    legitimate copy elsewhere in this prompt — DATA INTEGRITY prescribes verifying by
    type-checking and rendering rather than by mutating rows, and ENVIRONMENT describes what the
    harness does — so a prompt-wide search would either be permanently red or have to be watered
    down until it proved nothing."""
    completion = _completion_block(_BUILD_PROMPT).lower()
    assert "type-check" not in completion
    assert "tsc" not in completion
    # LIVENESS beside it — the block still says what ends the turn and what the summary must be.
    assert "declare_done" in completion
    assert "ends the turn" in completion


def test_prompt_has_no_stale_app_records_demo_reference() -> None:
    """The `app/records` demo route was removed from the template, so the prompt must
    no longer tell the model to hunt for and delete it. Only the stale REMOVE SCAFFOLDING
    parenthetical ever referenced it, and it is gone."""
    assert "app/records" not in _BUILD_PROMPT


def test_prompt_names_no_demonstration_data_model_or_example_component() -> None:
    """Two inertness guards modelled on
    `test_prompt_has_no_stale_app_records_demo_reference` just above. The template's
    demonstration data model (`items`/`item_status`/`audit_events`, the tables the old baseline
    migration created) and the deleted worked-reference component must never resurface in the
    composed prompt: the first is the demonstration table the agent's first schema change is
    forbidden from dropping, the second is the 271-line file the agent no longer has any reason
    to open."""
    prompt = _BUILD_PROMPT
    lowered = prompt.lower()
    # GUARD 1 — the deleted worked-reference component, in either spelling.
    assert "example-request-board" not in lowered
    assert "examplerequestboard" not in lowered
    # GUARD 2 — the demo table/enum identifiers the deleted baseline migration created.
    assert re.search(r"\bitems\b", lowered) is None
    assert "audit_events" not in lowered
    assert "item_status" not in lowered


def test_the_golden_template_manifest_names_no_removed_path() -> None:
    """The other half of the manifest tripwire.
    `test_every_golden_template_manifest_file_exists` proves every path the manifest names still
    exists; it says nothing about a path the template used to ship staying named after it is
    deleted. Pinned separately so reverting only the manifest edit (and not the file deletions)
    still trips something."""
    manifest = _BUILD_PROMPT[_BUILD_PROMPT.index("The app starts from a minimal") :]
    for removed in ("0000_baseline.sql", "0000_snapshot.json", "example-request-board.tsx"):
        assert removed not in manifest, f"the manifest still names the removed path {removed!r}"


def test_responsive_advice_survives_the_deleted_reference_component() -> None:
    """Deleting `components/example-request-board.tsx` must not silently delete the
    three-patterns advice it introduced (the RESPONSIVE block used to point at the file's own
    docstring for this). TWO HALVES: the inertness half is the "copy from it, into your own
    route" sentence naming the now-deleted file; the liveness half beside it is the toolbar/
    table/form guidance itself, restated inline where the reference used to be — an inertness
    guard alone would pass just as happily against a RESPONSIVE block someone had gutted
    outright."""
    prompt = _BUILD_PROMPT
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
    """Records a design premise as a passing assertion rather than building it as a guard:
    `backend/src/services/deploy/assets/next.config.ts`'s `outputFileTracingIncludes` globs
    `./drizzle/**`, and the template keeps `drizzle/meta/_journal.json` (emptied, not deleted),
    so `drizzle/` is never an empty directory and that glob always matches something. No change
    to `next.config.ts` was needed — this just proves the premise still holds against the actual
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
    """★ THE ASSERTION THAT MUST NOT BE SKIPPED.

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
        "the journal read failed, and that is what prints a migration failure into "
        f"every app's dev-server stdout on every boot. Full output:\n{output}"
    )
    # LIVENESS — the run actually reached the migrate step (the env var arrived at the
    # subprocess and the connection was attempted), or the inertness assertion above would pass
    # vacuously against a run that never got that far.
    assert "migrations failed" in output.lower() or "migrations up to date" in output.lower()


def test_prompt_teaches_the_drizzle_migration_discipline() -> None:
    """The app owns its schema through Drizzle, and the migration files are the only
    thing that carries that schema across a snapshot restore. Three load-bearing claims:
    `generate` writes a versioned file, the files under `drizzle/` stay in the workspace, and the
    schema-mutating `push` shortcut is banned (it applies changes with no migration file, so a
    restore returns code that expects tables the database does not have)."""
    prompt = _BUILD_PROMPT
    lowered = prompt.lower()
    assert "db/schema.ts" in prompt
    assert "drizzle-kit" in prompt and "generate" in lowered
    # The applying half is the composite now, not `npm run db:migrate` spelled out.
    assert APPLY_SCHEMA_CHANGE_TOOL in prompt
    # The ban is doctrine — and it is stated WITHOUT the literal command, so a repo-wide
    # `grep "drizzle-kit push"` stays a clean "nothing invokes it" check.
    assert "`push` command" in prompt
    assert "drizzle-kit push" not in prompt
    # The DSN is server-only and must never be copied into a file the snapshot carries.
    assert "next_public_" in lowered
    assert "server-side from `process.env`" in prompt


def test_prompt_makes_no_claim_that_the_starter_ships_demo_routes() -> None:
    """The template ships only `app/{globals.css,layout.tsx,page.tsx}` plus lib/config,
    NO example or demo routes. REMOVE SCAFFOLDING's old premise ("the starter ships example and
    demo routes") is false and must not send the model hunting scaffolding that does not exist."""
    lowered = _BUILD_PROMPT.lower()
    assert "example and demo routes" not in lowered
    assert "ships example" not in lowered


def test_refetch_after_every_write_appears_exactly_once() -> None:
    """Exactly ONE rule owns the after-write mechanic. The clause was hoisted OUT of
    HONEST UI into the unconditional AFTER A WRITE rule; if a future edit re-adds it to HONEST UI
    (two rules prescribing the same behaviour) this count trips."""
    assert _BUILD_PROMPT.lower().count("refetch after every write") == 1


def test_after_write_requirement_is_unconditional() -> None:
    """The after-write refetch applies to EVERY app, not only ones that claim liveness.
    It lives in its own AFTER A WRITE rule, above HONEST UI, and is NOT gated behind the
    live/shared/real-time conditional that owns interval/focus refetch."""
    prompt = _BUILD_PROMPT
    after_write = prompt[prompt.index("AFTER A WRITE") : prompt.index("HONEST UI")].lower()
    honest_ui = prompt[prompt.index("HONEST UI") : prompt.index("REMOVE SCAFFOLDING")].lower()
    assert "refetch after every write" in after_write
    # The AFTER A WRITE rule carries no "if your copy claims ..." guard — it is unconditional.
    assert "if your copy" not in after_write
    # And HONEST UI no longer owns the mechanic — it moved out.
    assert "refetch after every write" not in honest_ui


def test_honest_ui_keeps_its_claim_matching_argument() -> None:
    """HONEST UI keeps ONLY the claim-matching argument: interval and window-focus refetch
    stay CONDITIONAL on actually claiming a view is live/shared/real-time (the price of the
    claim), which is distinct from the unconditional own-mutation rule."""
    prompt = _BUILD_PROMPT
    honest_ui = prompt[prompt.index("HONEST UI") : prompt.index("REMOVE SCAFFOLDING")].lower()
    assert "if your copy" in honest_ui
    assert "interval" in honest_ui
    assert "focus" in honest_ui
    assert "real-time" in honest_ui


def test_every_golden_template_manifest_file_exists() -> None:
    """Durable guard: every path the manifest advertises as an editable starting point must
    exist under `sandbox/template/`, so a template change that drops or renames a file cannot
    leave the prompt pointing at a phantom. Walks the manifest text in the rendered prompt and
    stats each path — the `components/ui/*.tsx` glob and the comma-list line included.

    `sql` is in the extension set on purpose: the generated migrations under `drizzle/` are the
    one manifest entry that is BUILT rather than hand-written, so it is the entry most likely to
    go missing (an over-eager `.gitignore` line, a fresh clone). Without `sql` here the manifest
    could advertise a migrations directory that does not exist and nothing would notice."""
    manifest = _BUILD_PROMPT[_BUILD_PROMPT.index("The app starts from a minimal") :]
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
    """Regression guard for the opaque-origin sandbox learning:
    the host owns authentication and injects identity downward. A prompt that tells generated code
    to sign users in produces an in-sandbox login form that can never reach an auth endpoint from
    `origin: null`. The prompt is part of the trust boundary — keep sign-in out of it entirely."""
    lowered = _BUILD_PROMPT.lower()
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
    # The retired 'do not run any commands' line is gone — run_command exists now.
    assert "run any commands" not in repair.lower()


def _database_block(prompt: str) -> str:
    """The DATABASE paragraph, sliced out of the composed prompt — the one place the migration
    loop is taught. Sliced for the same reason `_completion_block` is: `--name`, `generate` and
    `rename` are legitimate copy elsewhere (the sentinel's refusal quotes the whole command),
    so a prompt-wide search would prove nothing about the sentence under test."""
    return prompt[prompt.index("DATABASE \u2014") :].split("\n\n", 1)[0]


def test_both_model_facing_sources_prescribe_the_same_one_call() -> None:
    """★ The two voices that tell the model how to change a schema still agree, and what
    they now agree ON is the composite rather than the two-command sequence.

    This is the check that caught the last half-landed fix: the re-test patched the prompt and
    missed the sentinel, so the model was corrected by one voice and mis-taught by the other."""
    from src.core.prompt_blocks import APPLY_SCHEMA_CHANGE_TOOL, MIGRATION_CHANNEL
    from src.services.orchestrator.sql_guard import _refusal

    refusal = _refusal("DELETE without WHERE")
    assert MIGRATION_CHANNEL in refusal
    assert APPLY_SCHEMA_CHANGE_TOOL in MIGRATION_CHANNEL
    # The build prompt sends the model to the same one call.
    assert APPLY_SCHEMA_CHANGE_TOOL in _database_block(_BUILD_PROMPT)
    # …and NEITHER voice hands back the raw sequence it replaced. The sentinel used to spell out
    # `npx drizzle-kit generate --name <what_changed>`, then `npm run db:migrate`, which is
    # exactly the pair whose zero exit codes lie.
    assert "drizzle-kit generate" not in refusal
    assert "db:migrate" not in refusal


_A_GENERATE_SPELLING = re.compile(r"drizzle-kit[\"\',\s]+generate(.{0,24})")


def test_the_prompt_prescribes_no_generate_command_for_the_model_to_run() -> None:
    """★ ONE SPELLING, EVERYWHERE THE MODEL LOOKS, and it is now the tool rather than the
    command.

    A `run_command` example carrying the bare `["npx","drizzle-kit","generate"]` — forbidden two
    blocks earlier in the same prompt — used to ship here. It is gone, and the prescription is
    removed altogether: `apply_schema_change` runs the command, so the prompt has no reason to
    spell it. The rule survives as an INERTNESS guard — any generate spelling that comes back
    must carry the flag — plus the liveness assertion that says what replaced it, because an
    inertness assertion alone is green against a prompt that stopped teaching migrations at
    all."""
    for tail in _A_GENERATE_SPELLING.findall(_BUILD_PROMPT):
        assert "--name" in tail, f"a bare `drizzle-kit generate` survives in the prompt: {tail!r}"
    assert "run_command([" not in _database_block(_BUILD_PROMPT)
    # LIVENESS — the prompt still teaches how a schema change is made.
    assert f"{APPLY_SCHEMA_CHANGE_TOOL}(what_changed=" in _BUILD_PROMPT


def test_the_template_offers_no_second_spelling_of_the_generate_command() -> None:
    """★ The template half. `package.json` shipped `"db:generate": "drizzle-kit
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


def test_the_migration_name_claims_only_what_naming_actually_buys() -> None:
    """The `--name` bullet may claim only what naming actually buys.

    The prompt used to read "ALWAYS pass `--name`: without it the command PROMPTS when the diff
    is ambiguous ... so it hangs until it is killed." A smoke against the template's pinned
    `drizzle-kit@0.31.10` says otherwise: a bare generate over an unambiguous diff exits 0 and
    writes `drizzle/0001_special_fantastic_four.sql` — a RANDOM NAME, not a hang. The flag is the
    composite's `what_changed` argument now, and it may still only claim what it buys, or the
    model reasons from a mechanism that does not exist."""
    database = _database_block(_BUILD_PROMPT)
    name_rule = database[database.index("`what_changed` names") :].split("\n", 1)[0].lower()

    # INERTNESS — the hang, and the ambiguity mechanism, are not this bullet's business. Matched
    # as WORDS: `what_changed` itself contains the letters of "hang".
    assert re.search(r"\bhangs?\b", name_rule) is None
    assert re.search(r"\bprompts?\b", name_rule) is None

    # THE REAL COST, stated concretely enough to be checkable: a name buys a READABLE history.
    assert "read" in name_rule
    assert "what_changed" in name_rule


def test_the_prompt_teaches_the_split_that_actually_unblocked_the_wedged_build() -> None:
    """The one-change-per-call rule has to carry the rename resolver's real failure mode.

    Verified by smoke, both ways round: under a TTY the rename resolver ("is `label` created, or
    renamed from `title`?") waits forever — that is the 4m09s stall — and `--name` does not
    answer it. Under the sandbox's real `stdin=DEVNULL` it is worse: drizzle-kit prints
    "Interactive prompts require a TTY terminal" to stderr, writes no migration, and EXITS 0. A
    model taught only "it hangs" reads that zero exit as success and builds on a schema change
    that never happened, so the zero exit is the half that must be said out loud."""
    database = _database_block(_BUILD_PROMPT).lower()
    assert "one kind of change per call" in database
    assert "rename" in database
    # The mechanism: an interactive question, and no flag answers it.
    assert "asks" in database
    assert "no flag answers" in database
    # …and the failure MODE, which is the part that actually costs a build.
    assert "no migration file" in database
    assert "zero exit code" in database


def test_the_drizzle_artifacts_instruction_is_emitted_exactly_once() -> None:
    """The same rule was printed twice in one prompt: once in the golden-template manifest
    (`drizzle/*.sql … versioned artifacts that must stay in the workspace`) and once in the
    DATABASE block. Counting is the point — an `in` assertion is green at one copy and at two."""
    lowered = _BUILD_PROMPT.lower()
    assert lowered.count("versioned artifacts") == 1
    assert lowered.count("travel with the snapshot") == 1
    # LIVENESS — the surviving copy is the DATABASE one, which carries the extra rule.
    assert "never hand-edit one that has already been applied" in lowered


# --- One operation for applying a database change ---------------------------------------------


def test_the_two_step_sequence_is_no_longer_the_taught_path_but_the_tty_defences_still_are() -> (
    None
):
    """★ The prompt-trim inertness guard, WITH the liveness assertion it needs beside it.

    The DATABASE block used to dictate two `run_command([...])` invocations. It dictates one tool
    call now, and that is the inert half: neither raw command may be prescribed as the path, or
    the model is being taught the sequence whose zero exit codes lie.

    The liveness half is a different file entirely, and it is the one this unit could break by
    accident. The composite's cleanest failure — the rename resolver — is only FAST because of
    three defences in `sandbox/supervisor/app.py`: `CI=1` (well-behaved tools refuse to prompt),
    `stdin=DEVNULL` (drizzle-kit's prompt renderer probes `process.stdin.isTTY` and fails fast
    against a closed one), and `_refuse_a_manufactured_tty` (the agent's `pty.spawn` workaround,
    which bought the observed 4m09s stall). Remove any one and this tool's crispest detection
    becomes a wedged command running to its timeout — with every assertion in this file still
    green, because none of them is about that file. This one is."""
    database = _database_block(_BUILD_PROMPT)
    # INERTNESS — the sequence is not the prescribed path any more.
    assert "run_command([" not in database
    assert '"drizzle-kit"' not in database
    assert '"db:migrate"' not in database
    # …and the one call is.
    assert f"{APPLY_SCHEMA_CHANGE_TOOL}(what_changed=" in database

    # LIVENESS — the three TTY defences that make reaching the resolver fast and loud.
    supervisor = (_TEMPLATE_ROOT.parent / "supervisor" / "app.py").read_text(encoding="utf-8")
    assert 'env["CI"] = "1"' in supervisor
    assert "stdin=subprocess.DEVNULL" in supervisor
    assert "_refuse_a_manufactured_tty(body.cmd)" in supervisor


async def test_the_composite_is_offered_and_its_line_is_its_own_first_sentence() -> None:
    """★ The composite reaches the model as a REGISTERED TOOL, and the sentence the prompt
    spends on it is the same string its registration carries.

    The generic drift check covers every tool at once; this names the one this unit adds, so a
    failure reads as "the composite fell out of the prompt" rather than as a snapshot mismatch."""
    definitions = await registered_tool_definitions(ChatKind.BUILD)
    assert APPLY_SCHEMA_CHANGE_TOOL in definitions, "the composite is not registered for Write"
    described = definitions[APPLY_SCHEMA_CHANGE_TOOL].description or ""
    line = f"- `{APPLY_SCHEMA_CHANGE_TOOL}` \u2014 {first_sentence(described)}"
    assert line in _tool_surface_block(_BUILD_PROMPT)
    # The sentence has to carry the tool's REASON, not just its name — a roll-call line that only
    # says "applies a schema change" leaves the model with no cause to prefer it over the two
    # commands it already knows.
    assert "truthfully" in line and "failed" in line


# --- The TOOL SURFACE block is GENERATED, and this is the check that keeps it so --------------
#
# The goal is a check that fails when a DESCRIBED behaviour and the actual behaviour diverge.
# A name-set comparison cannot make that promise: the `declare_done` fix above is the proof — it
# changed what `declare_done` does while the sentence describing it still promised a follow-up
# round-trip, and every name-based assertion in this repo stayed green. So the block is
# rendered from the tool definitions pydantic-ai hands the model at registration, and the drift
# check is a snapshot assertion over that rendering plus a per-mode membership assertion against
# `toolsets_for_kind`.


def _tool_surface_block(prompt: str) -> str:
    """The TOOL SURFACE block, sliced out of the composed prompt."""
    return prompt[prompt.index("TOOL SURFACE:") :].split("\n\n", 1)[0]


async def _the_drift_check() -> None:
    """THE DRIFT CHECK ITSELF, factored out so the mutation tests can require it to go RED.

    An equality assertion proves the snapshot is right today; it does not prove the assertion
    would notice if it stopped being — which is exactly the property that failed. The
    two mutation tests below run THIS function against a deliberately-mutated registry."""
    generated = await render_tool_surface(ChatKind.BUILD)
    assert WRITE_TOOL_SURFACE == generated, (
        "the TOOL SURFACE block in `core/prompt_blocks.py` no longer matches the tools the Write "
        "arm registers. Regenerate it with the one-liner under `Regenerate the snapshot with:` "
        "in `services/agent/toolsets.py` and paste the result over `WRITE_TOOL_SURFACE`."
        f"\n\ngenerated:\n{generated}"
    )


async def test_the_tool_surface_is_generated_from_the_tools_the_write_arm_registers() -> None:
    """★ The snapshot half. Counted in the composed prompt as well, because a block that reached
    zero composition sites would satisfy the equality assertion perfectly well."""
    await _the_drift_check()
    assert _BUILD_PROMPT.count(WRITE_TOOL_SURFACE) == 1


async def test_the_snapshot_stays_the_platforms_surface_not_one_projects() -> None:
    """★ ADDED, NOT CHANGED — and if this gate makes you regenerate the snapshot, it is being read
    wrong.

    `WRITE_TOOL_SURFACE` is a snapshot of what EVERY project's Build prompt carries. The
    connected-data tool is registered only for a project whose connector an administrator has
    approved and whose owner has switched it on, so rendering the snapshot with a connector passed
    would bake that tool into the Build prompt of every project on the platform — including one
    whose administrator refused it. That is precisely what registration-gating exists to prevent,
    and the checked-in block is where it would leak silently: the prompt would promise a tool the
    runtime then rejects as unknown.

    So: the default render is the snapshot (the drift check above), the connected render is one
    tool larger, and the tool's own description reaches the model through the tool schema and the
    project's CONNECTED DATA stub — beside the data it reads, exactly when the tool exists."""
    default = await render_tool_surface(ChatKind.BUILD)
    connected = await render_tool_surface(
        ChatKind.BUILD, connected_systems=(a_connected_system(),)
    )
    assert default == WRITE_TOOL_SURFACE
    assert CONNECTOR_SCHEMA_TOOL not in default
    assert f"- `{CONNECTOR_SCHEMA_TOOL}` \u2014 " in connected
    assert len(connected.splitlines()) == len(default.splitlines()) + 1
    # The composed Build prompt of an ordinary project names it nowhere — not in the tool surface
    # block, not anywhere else.
    assert CONNECTOR_SCHEMA_TOOL not in _BUILD_PROMPT


async def test_the_prompts_tool_list_is_exactly_what_the_write_arm_registers() -> None:
    """★ THE MEMBERSHIP HALF, asserted against `toolsets_for_kind` rather than a hand-kept list.

    The hand-written block named six tools while the Write arm handed the model eight: the two
    structured reads it borrows off `read_only_toolset` (`_WRITE_STRUCTURED_READS`) were absent
    from the prompt for their entire life, so the model was never told it could list or search
    the tree and paid for that in `run_command` round-trips."""
    registered = set(await registered_tool_definitions(ChatKind.BUILD))
    named = set(re.findall(r"^- `(\w+)` \u2014 ", _tool_surface_block(_BUILD_PROMPT), re.M))
    assert named == registered
    # The two the old prose omitted, named explicitly so the failure reads as itself.
    assert {"list_files", "search_files"} <= named
    # …and nothing from a mode Write is not: Plan's confirmation tool is uncallable here.
    assert "present_plan_options" not in named


async def test_every_tool_line_is_its_registered_descriptions_first_sentence() -> None:
    """★ THE DESCRIPTION HALF — the one a name-set comparison cannot make.

    Each line must be the tool's OWN words, not a paraphrase of them, so the prompt and the tool
    schema cannot say different things about the same tool."""
    for name, definition in (await registered_tool_definitions(ChatKind.BUILD)).items():
        assert definition.description, f"`{name}` reaches the model with no description"
        line = f"- `{name}` \u2014 {first_sentence(definition.description)}"
        assert line in _BUILD_PROMPT, f"the prompt paraphrases `{name}`; expected {line!r}"


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
    assert "summon_a_pony" in await render_tool_surface(ChatKind.BUILD)
    with pytest.raises(AssertionError, match="no longer matches the tools"):
        await _the_drift_check()


async def test_the_drift_check_fails_when_a_tools_docstring_is_reworded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ DELIBERATE MUTATION 2 — the same tools, one of them described differently. THIS is the
    assertion the `declare_done` fix above proves is necessary, so the mutation reintroduces that
    exact regression: a `declare_done` that promises the follow-up round-trip the harness stopped
    buying. Every name-based check in this repo is green against it; this one is not."""
    registers_the_six = sandbox_toolset

    def describes_declare_done_the_old_way(sandbox_of: _SandboxOf) -> FunctionToolset[Any]:
        toolset = registers_the_six(sandbox_of)
        toolset.tools["declare_done"].description = (
            "Declare the build finished. The harness then verifies the app, and if it is not "
            "green yet you will receive the diagnostic and can carry on."
        )
        return toolset

    monkeypatch.setattr(_THE_SANDBOX_FACTORY, describes_declare_done_the_old_way)
    reworded = await render_tool_surface(ChatKind.BUILD)
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
            await render_tool_surface(ChatKind.BUILD)


async def test_run_commands_dev_server_rule_is_registered_copy_as_well_as_prompt_copy() -> None:
    """★ THE LIVENESS GUARD ON THE DOCSTRINGS THEMSELVES.

    The docstrings are prompt text now, so trimming one is a prompt edit. This sentence is what
    covers the agent starting a dev server through `/exec` without the "already running" marker
    — the supervisor's child env carries nothing that would tell a second `next dev` apart from
    the real one — so it must survive in BOTH voices: the tool's own description, and the
    ENVIRONMENT block."""
    definitions = await registered_tool_definitions(ChatKind.BUILD)
    described = (definitions["run_command"].description or "").lower()
    assert "do not start or restart the dev server" in described
    assert "already running" in described
    # And the prompt's own wording carries the same guard, because the two travel together.
    assert "do not start, restart, or kill it" in _BUILD_PROMPT.lower()


def test_the_prompt_never_grants_edit_permission_over_the_platform_config() -> None:
    """★ THE MUTANT THAT MUST FAIL, and it must fail for ALL THREE statements.

    `next.config.ts` carries the path the app is served under: lose it and the preview answers
    at `/` while the router asks for `/a/<key>/` and loads blank, while every automated check
    still reports healthy. The file stays technically writable by decision, so this prompt text
    IS the control.

    Three separate statements grant edit permission, they all ship in the SAME composed prompt,
    and correcting fewer than three leaves a contradiction the model can resolve either way:

      1. the manifest header's categorical "no file is frozen"
      2. the manifest's own line for the file
      3. the WRITE SURFACE paragraph's categorical "the WHOLE workspace is editable"

    A test that only checked one would go green against a half-fix, which is exactly how the
    original review missed the third.
    """
    prompt = _BUILD_PROMPT

    # 1 — the categorical grant in the manifest header is gone.
    assert "no file is frozen" not in prompt

    # 2 — the file is named as platform-owned rather than listed among the editable ones.
    assert "next.config.ts" in prompt, "the manifest must still NAME the file"
    assert "package.json, next.config.ts" not in prompt, (
        "the file must not sit in the editable comma-list"
    )
    assert "PLATFORM-OWNED" in prompt

    # 3 — the WRITE SURFACE paragraph excepts it alongside `.git/`.
    write_surface = prompt.split("WRITE SURFACE")[1].split("DATA & STORAGE")[0]
    assert "next.config.ts" in write_surface, (
        "the categorical write grant must except the platform config by name"
    )
    assert "the WHOLE workspace is editable" not in write_surface

    # And the SAME correction must reach the Write-turn prompt, which is a different composition
    # over the same blocks. Asserted rather than assumed: if the two ever stop sharing
    # `prompt_blocks`, a fix applied to one would silently leave the other granting permission.
    from src.services.agent.mode_prompts import _WRITE_SEGMENT

    assert "no file is frozen" not in _WRITE_SEGMENT
    assert "the WHOLE workspace is editable" not in _WRITE_SEGMENT
    assert "next.config.ts" in _WRITE_SEGMENT
