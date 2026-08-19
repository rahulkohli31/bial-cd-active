"""The build-agent system prompt + the repair-prompt template (KD-4 / KD-9 / KD-10 / C6 / R18).

`BUILD_SYSTEM_PROMPT` describes the open-sandbox reality the model works in (the vibe-coding
pivot): a real shell (`run_command` + on-demand `npm install`), a fully editable workspace (config
and `package.json` included, only `.git/` and escapes denied), the always-running dev server it
must NOT restart, the injected app ENV it writes its own data/storage code against, the
real-data-only rule (R4 — no seeded dummy records; empty/loading/error states instead), the tool
surface, and a slim golden-template manifest of editable starting points (KD-10 — instead of a
computed repo map). `build_repair_prompt` frames a redacted `BuildError` as the NEXT run's user
prompt — the concrete channel by which a harness-observed error re-enters the model's context
(KD-1 / KD-5).

The unconditional AFTER A WRITE rule (U11 — the user must see their own mutation without a manual
reload) is UNENFORCEABLE at generation time. The shipped static detector
`flag_liveness_overpromise` (`src/services/build_sessions/liveness.py`) is claim-gated: its
`_CLAIM_RE` only fires on a `.tsx`/`.jsx` file that advertises live/shared/real-time copy, so an
app that makes no such claim and wires no refetch violates this rule silently — nothing lands in
the log. Measuring the
rendered-page property "the user saw their own write" needs a JS-executing probe the frozen C2
`SandboxClient` surface cannot run. That gap is ACCEPTED here, not closed (deferred to issue #49);
relaxing `_CLAIM_RE` for the after-write case is a cheap follow-up, explicitly out of scope for
this unit.

Kept as a module constant (like `describe.py:_DESCRIBE_SYSTEM`) so the prompt evolves in code
review, never at config or runtime.
"""

from __future__ import annotations

from src.api.v1.build_sessions.schemas import BuildError
from src.core.prompt_blocks import (
    AUTH_IDENTITY_RULES as AUTH_IDENTITY_RULES,
)
from src.core.prompt_blocks import (
    BUILD_WORKING_RULES_HEAD as BUILD_WORKING_RULES_HEAD,
)
from src.core.prompt_blocks import (
    BUILD_WORKING_RULES_TAIL as BUILD_WORKING_RULES_TAIL,
)
from src.core.prompt_blocks import (
    DATA_INTEGRITY_RULES as DATA_INTEGRITY_RULES,
)
from src.core.prompt_blocks import (
    WRITE_IDENTITY as WRITE_IDENTITY,
)

BUILD_SYSTEM_PROMPT = f"""\
{WRITE_IDENTITY}

{BUILD_WORKING_RULES_HEAD}

{DATA_INTEGRITY_RULES}

{AUTH_IDENTITY_RULES}

{BUILD_WORKING_RULES_TAIL}"""
"""The standalone build prompt, now assembled from EXACTLY the pieces `_WRITE_SEGMENT` uses
(KTD-5a). It survives only for the legacy `/build-sessions` harness path; the identity paragraph
that used to be typed out here is imported, so the two cannot drift while both exist."""


def build_repair_prompt(error: BuildError) -> str:
    """Frame a redacted `BuildError` as the next run's user prompt (KD-1 / KD-5). The
    `cleaned_stack` is already de-noised + secret-redacted by `errors.declutter`."""
    return (
        f"The build is not green yet — a `{error.source.value}` check failed:\n\n"
        f"{error.title}\n\n"
        f"{error.cleaned_stack}\n\n"
        "Fix the root cause in your code, then call `declare_done` again. You may use "
        "`run_command` to investigate (re-run a check, inspect a file, reinstall a dependency)."
    )
