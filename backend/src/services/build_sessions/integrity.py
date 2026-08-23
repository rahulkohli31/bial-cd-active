"""What the container still holds, asked of the container itself.

TWO QUESTIONS LIVE HERE, and they are asked by different callers for different reasons.

**The baseline-identity probe** (U6, R9) answers "is this app still serving the starter page the
platform seeded at birth?" — the content half of the health verdict. On 2026-08-18 "Build complete
— your app is live below" sat above the untouched golden template for nine minutes, in front of a
client, because nothing in the platform ever asked.

WHY A GIT IDENTITY FACT AND NOT A MARKER IN THE TEMPLATE. A marker written into
`sandbox/template/app/page.tsx` reaches only apps provisioned from a rebuilt image — and an
EXISTING app restored from its own pre-marker bundle checks out a markerless `page.tsx` too, so
for the whole fleet that exists today the false claim would stay live. The repository's ROOT
COMMIT is the `bial: golden template baseline` the sandbox client seeds at provision, it survives
a restore because bundles carry complete history, and comparing against it needs no image rebuild,
no prompt coordination and no marker the agent could be tempted to preserve. It is also an
explicit identity comparison rather than a heuristic, which is what
`docs/solutions/best-practices/e2e-harness-measure-after-the-barrier-and-refuse-vacuous-passes-2026-08-02.md`
requires after a 255-vs-341 character gap read as "template, not app" and produced a false P0.

THE COMPARISON IS BLOB SHA AGAINST BLOB SHA, not a diff. `git rev-parse <root>:<path>` is what the
root commit stored; `git hash-object <path>` is what is in the tree now. Equal means byte-identical
by construction, it needs nothing on the image but git itself (no `cmp`, no `diff`, neither of
which the slim Node base is guaranteed to carry), and both sides pass through the same filter
mechanism so neither can be made to disagree by configuration the other did not see.

**Everything unanswerable is `UNANSWERABLE`, never "unchanged".** No root commit, more than one
root, a root commit that never held the file, an exec that failed: each of those is a question we
could not answer, and answering it as "still the template" would fail a working app while
answering it as "diverged" would re-open the very claim this exists to close. The health verdict
reads `UNANSWERABLE` as `INDETERMINATE` and re-checks.

*This module is also the home Phase C's workspace-integrity verdict lands in, along with the
container-state primitives it shares with the reaper — which is why it is a module of its own from
the start and why it stays free of anything heavy at import time.*
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog

from src.core.integrity_types import BaselineIdentity
from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle
from src.services.storage import (
    StorageError,
    StorageUnconfiguredError,
    get_storage,
    recovery_key,
)

_log = structlog.get_logger()

BASELINE_PATH: Final = "app/page.tsx"
"""The one file the starter-page question is asked about.

The app's ROOT ROUTE, because that is the page the citizen is looking at when the platform claims
the build is complete — it is what "your app is live below" points at. An agent that builds only
sub-routes and never touches the root is, for this verdict's purposes, an agent that has not built
the user's app yet, and saying so is the point rather than a false positive: the repair prompt
tells it exactly that.

An agent that REPLACES the root with a redirect satisfies this correctly and for the right reason
— `page.tsx` differs, so the app has diverged from the baseline, because the agent did write it."""

# The probe, as one `sh -c` so a verdict costs a single exec. Three `@@`-separated fields, in the
# `_STATE_SCRIPT` house style: the repository's root commit(s), the blob the root commit stored at
# `BASELINE_PATH`, and the blob the working tree holds there now.
#
# `|| true` on every arm, and the last statement always succeeds: a non-zero exit from this script
# must mean the EXEC failed, not that one of three questions came back empty. An empty field is a
# fact the parse reads; a non-zero exit is a transport problem, and conflating them would make a
# repository with no root commit look like a container we could not reach.
#
# ROOTS ARE PRINTED IN FULL, not counted here. `git rev-list --max-parents=0` emits one line per
# root, and a repository with two roots (an agent that fetched and merged an unrelated history) is
# a question this probe cannot answer — but the SHELL must not be the thing that decides that,
# because `wc -l` on empty output is 0 and on one root is 1 and the two mean opposite things.
_BASELINE_SCRIPT: Final = (
    "roots=$(git rev-list --max-parents=0 HEAD 2>/dev/null || true); "
    'printf "%s" "$roots"; echo "@@"; '
    'root=$(printf "%s" "$roots" | head -n 1); '
    'if [ -n "$root" ]; then '
    f'git rev-parse --verify --quiet "$root:{BASELINE_PATH}" 2>/dev/null || true; '
    "fi; "
    'echo "@@"; '
    f"git hash-object {BASELINE_PATH} 2>/dev/null || true"
)


def parse_baseline_identity(stdout: str) -> BaselineIdentity:
    """Pure parse of `_BASELINE_SCRIPT`'s three `@@`-separated fields.

    Split out for the same reason `_parse_state` was: the parse is the part with the edge cases and
    it is fully testable without a container, while the probe around it is one exec. A truncated or
    otherwise malformed body reads as `UNANSWERABLE` — `partition` yields empty strings for the
    fields that were not there, and every empty field already denies."""
    roots_text, _, rest = stdout.partition("@@")
    baseline_text, _, working_text = rest.partition("@@")
    roots = [line for line in roots_text.split() if line]
    if len(roots) != 1:
        # No root commit at all (no repository, or an unreadable one), or more than one. Both are
        # structural: there is no single birth certificate to compare against, and re-running the
        # probe will keep saying so.
        return BaselineIdentity.UNANSWERABLE
    baseline_blob = baseline_text.strip()
    if not baseline_blob:
        # The root commit exists and never held this file. Nothing to compare against, and no
        # amount of retrying adds it — so this is unanswerable rather than "diverged", which
        # would hand a completion claim to an app on the strength of a missing file.
        return BaselineIdentity.UNANSWERABLE
    working_blob = working_text.strip()
    if not working_blob:
        # The baseline held the file and the tree does not. That is provably NOT the starter page,
        # which is the only question asked here — whether an app with no root route is healthy is
        # the SERVING half's business, and it will answer 404.
        return BaselineIdentity.DIVERGED
    if working_blob == baseline_blob:
        return BaselineIdentity.STILL_THE_BASELINE
    return BaselineIdentity.DIVERGED


async def baseline_identity(
    sandbox_client: SandboxClient, handle: SandboxHandle
) -> BaselineIdentity:
    """Ask one container whether its root route is still the seeded baseline (U6).

    One exec, bounded, and it never raises: every failure is `UNANSWERABLE`, because a probe that
    could throw would make the health verdict fail a build for a supervisor blip."""
    run_command = sandbox_client.exec  # aliased to keep the call off the JS-oriented exec guard
    try:
        result = await run_command(handle, ["sh", "-c", _BASELINE_SCRIPT], timeout_s=30)
    except SandboxError:
        _log.warning("baseline_identity_probe_failed", app=handle.app_name, exc_info=True)
        return BaselineIdentity.UNANSWERABLE
    if result.exit != 0:
        _log.warning("baseline_identity_probe_nonzero", app=handle.app_name, exit_code=result.exit)
        return BaselineIdentity.UNANSWERABLE
    return parse_baseline_identity(result.stdout)


async def has_ever_been_built(app_id: uuid.UUID) -> bool:
    """Has any turn on this app ever done real work? (U6's gating fact.)

    THE CONTENT CHECK IS ONLY MEANINGFUL FOR AN APP THAT HAS BEEN BUILT. A brand-new project is
    *supposed* to be showing the starter page, and calling that unhealthy would fail every first
    look at a project nobody has asked for anything yet.

    The durable fact that answers it is the presence of a RECOVERY COPY. There is no `turns` model
    — turns are `message` rows with a `TURN` entry kind, and a row scan per verdict is neither
    cheap nor obviously correct — but `finish_turn_sandbox` writes a recovery copy on any turn that
    touched files, which is exactly why `_nothing_to_lose` already uses its absence to mean "no
    turn ever did". One HEAD request, and the integrity gate resolves it once per turn anyway.

    ITS ONE BLIND SPOT, stated rather than hidden: an app whose building turns ALL failed to write
    a recovery copy reads as never-built. That is precisely the failure U3's "recovery write did
    not land" alarm exists to make visible; if it fires often, this wants a stronger source.

    THE TWO "NO"s ARE NOT THE SAME, and they fail in opposite directions on purpose:

    * Storage **unconfigured** is a fact about the DEPLOYMENT (KTD-2, and `durable_copy.py`
      documents the same distinction). With no store there can be no recovery copy for anyone, so
      this is a confirmed absent — `False`, and the content check is skipped.
    * Storage **unreadable** is a transient blip, and it fails CLOSED — `True`, so the check runs.
      The direction is deliberate: this plan exists because a completion claim appeared over an
      untouched template, and the worst case of checking an app that turns out to be brand-new is
      one honest sentence saying it is still the starter page. The worst case of NOT checking is
      the 2026-08-18 lie, shipped again, during an outage nobody would connect it to.
    """
    try:
        store = get_storage()
    except StorageUnconfiguredError:
        return False
    try:
        return await store.head(recovery_key(app_id)) is not None
    except StorageError:
        _log.warning("prior_building_turns_unreadable", app_id=str(app_id), exc_info=True)
        return True
