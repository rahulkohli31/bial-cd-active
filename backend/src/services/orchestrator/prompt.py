"""The repair-prompt template.

WHY THIS EXISTS
`build_repair_prompt` frames a redacted `BuildError` as the next run's user prompt — the
concrete channel by which an observed error re-enters the model's context. Its consumer is
the live turn engine's self-heal loop (`turns/engine.py`).

NO SYSTEM PROMPT IS BUILT HERE. The Write prompt is composed by `mode_prompts`, from the
`core/prompt_blocks.py` pieces, and this module contributes nothing to it — a repair prompt
rides the USER channel, so anything stated here would be a second voice in a turn the
standing contract already speaks for.

The unconditional rule that the user must see their own write without a manual reload is
UNENFORCEABLE at generation time. The shipped static detector `flag_liveness_overpromise`
(`src/services/build_sessions/liveness.py`) is claim-gated: its `_CLAIM_RE` only fires on a
`.tsx`/`.jsx` file that advertises live/shared/real-time copy, so an app that makes no such
claim and wires no refetch violates this rule silently — nothing lands in the log. Measuring
whether the user actually saw their own write needs a JS-executing probe the frozen
`SandboxClient` surface cannot run. That gap is accepted, not closed; relaxing `_CLAIM_RE`
for the after-write case is a cheap follow-up, out of scope here.
"""

from __future__ import annotations

from src.api.v1.build_sessions.schemas import BuildError


def build_repair_prompt(error: BuildError) -> str:
    """Frame a redacted `BuildError` as the next run's user prompt. The `cleaned_stack`
    is already de-noised + secret-redacted by `errors.declutter`.
    THE ONE PLACE `agent_only_detail` IS READ. A `client`-class report is text the
    generated app wrote, so it is deliberately absent from the two fields that egress
    to the portal and carried instead on a field that never serializes — the model's
    copy of the diagnostic can only be assembled here, in-process. It arrives
    pre-wrapped in the data-only frame `errors.from_client` builds; never unwrap it or
    "simplify" this to a bare `cleaned_stack` read, which would silently send the model an
    empty diagnostic for the entire runtime-crash class."""
    detail = error.agent_only_detail or error.cleaned_stack
    return (
        f"The build is not green yet — a `{error.source.value}` check failed:\n\n"
        f"{error.title}\n\n"
        f"{detail}\n\n"
        "Fix the root cause in your code, then call `declare_done` again. You may use "
        "`run_command` to investigate (re-run a check, inspect a file, reinstall a dependency)."
    )
