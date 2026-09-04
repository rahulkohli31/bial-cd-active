"""Every route a comment hands a reader must resolve against the mounted app.

WHY THIS IS A TEST AND NOT A REVIEW HABIT. A URL in a docstring is an instruction someone
follows — usually an operator, usually mid-incident, always by pasting it — and prose cannot go
red. Two of them had already rotted the same way: the reaper's `POST /v1/admin/reconcile-sandboxes`
(the governance router mounts at `/admin/apps`), and `POST /v1/internal/reap` in
`services/sandbox/config.py` and `workers/sandbox_reap.py` (the reap lever is on the
build-sessions router). In every case the prefix moved underneath a sentence that kept pointing
at where it used to be, and the symptom was a 404 at the moment the lever was most needed.

`tests/services/build_sessions/test_reaper.py` already does this for ONE module. This is the same
assertion widened to the whole of `src/`, which is the version that catches the next one: the
single-module guard is only ever as good as somebody remembering to add a second copy of it.

ASSERTED AGAINST THE MOUNTED APP, never against a hard-coded list — the failure mode is a prefix
moving, so pinning literals would go green on the rot and red on the fix. `openapi()` is the
resolution point: `include_router` defers to `_IncludedRouter`, so `app.routes` holds no paths
until the schema is built.

PATH PARAMETER NAMES ARE NORMALISED AWAY, deliberately, and this is the line between what this
guard is for and what it is not. Prose legitimately writes `{id}` or `{projectId}` where the route
declares `{app_id}` or `{project_id}` — that is an abbreviation a reader resolves without trouble,
and pasting it was never going to work anyway because a real id has to be substituted in. What
CANNOT be resolved by a reader is a wrong prefix or a wrong segment, because it looks exactly like
a right one. So `{...}` collapses to `{}` on both sides and everything else must match exactly.

Mutation check: drop `/apps` from the reaper docstring's reconcile URL, or write
`/v1/internal/reap` in either sweep module, and this goes red naming the file.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.main import create_app

_SRC = Path(__file__).resolve().parents[1] / "src"

_A_ROUTE_IN_PROSE = re.compile(r"`(?:GET|POST|PATCH|PUT|DELETE) (/v1/[A-Za-z0-9/_{}.-]+)`")
"""A backticked method+path — the shape a comment uses when it is handing someone a URL. Anchored
on `/v1/` so the pattern cannot drag in a portal path, a supervisor path, or an example."""

_A_PATH_PARAMETER = re.compile(r"\{[^{}]*\}")
"""One `{…}` segment. Collapsed to `{}` on both sides so `{id}` and `{app_id}` compare equal."""


def _shape(path: str) -> str:
    return _A_PATH_PARAMETER.sub("{}", path)


def test_every_route_named_in_a_backend_comment_resolves() -> None:
    mounted = {_shape(path) for path in create_app().openapi()["paths"]}
    assert mounted, "the app mounted no paths; this guard has lost its subject"

    named_somewhere = False
    unresolvable: dict[str, list[str]] = {}
    for module in sorted(_SRC.rglob("*.py")):
        named = {_shape(m) for m in _A_ROUTE_IN_PROSE.findall(module.read_text(encoding="utf-8"))}
        named_somewhere = named_somewhere or bool(named)
        missing = named - mounted
        if missing:
            unresolvable[str(module.relative_to(_SRC.parent))] = sorted(missing)

    assert named_somewhere, "no comment in `src/` names a `/v1/` route; the regex has drifted"
    assert not unresolvable, (
        "these comments hand a reader a URL no route serves — correct the prose (or the route):\n"
        + "\n".join(f"  {module}: {paths}" for module, paths in sorted(unresolvable.items()))
    )
