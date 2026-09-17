"""What the container still holds, asked of the container itself.

Two questions live here: the baseline-identity probe (is the app still serving the seeded starter
page?) and the workspace-integrity verdict (does this container still hold this app's work —
asked before every turn, the only answer that can authorise replacing a live workspace).

WHY THIS EXISTS. The baseline check compares blob shas against the repo's root commit rather than
a template marker or a length heuristic: a marker misses apps restored from a pre-marker bundle,
and a 255-vs-341 character gap once read as "template, not app" and fired a false top-severity
alarm. Blob-sha-against-root needs no image rebuild and nothing beyond git itself (the slim Node
base has neither `cmp` nor `diff`). Every unanswerable case — no root commit, more than one, a root
that isn't the seeded template, a failed exec — returns `UNANSWERABLE`, never "unchanged"; the
health verdict retries rather than guessing.

Neither half imports `manager` or the orchestrator: `manager` imports `reaper`, and both import
this module at module level, so the reverse would cycle. `selfheal` and `harness` defer their own
imports of this module for the same reason.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass, replace
from typing import Final

import structlog

from src.core.integrity_types import BaselineIdentity
from src.core.integrity_types import WorkspaceState as WorkspaceState
from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle
from src.services.storage import (
    StorageError,
    StorageUnconfiguredError,
    get_storage,
    head_sha_from_metadata,
    recovery_key,
    snapshot_key,
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
# `state_script` house style: the repository's root commit(s), the blob the root commit stored at
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
BASELINE_COMMIT_SUBJECT: Final = "bial: golden template baseline"
"""The subject line the sandbox client commits the seeded template under.

MATCHED, NOT ASSUMED, and this is the difference between a check and an accusation. The whole
comparison rests on "the root commit IS the golden template", and that is only true when the root
was written by `client._INIT_REPO_SCRIPT`, which is the system's only writer of a root commit — a
provision that cannot seed the repository fails outright rather than leaving one to be forged
later. A root written at the END of a turn would hold the FINISHED APP, and comparing against it
would find `app/page.tsx` identical forever: the app would be permanently and irreversibly accused
of serving the starter page, a completion claim that can never be earned again, which is worse
than the false claim this check exists to stop.

Spelled here rather than imported from `services/sandbox/client.py` for the reason `reaper.py`
documents about that direction of import: this module must stay importable by the worker. The two
literals are pinned equal by a test."""

_BASELINE_SCRIPT: Final = (
    "roots=$(git rev-list --max-parents=0 HEAD 2>/dev/null || true); "
    'printf "%s" "$roots"; echo "@@"; '
    'root=$(printf "%s" "$roots" | head -n 1); '
    'if [ -n "$root" ]; then '
    f'git rev-parse --verify --quiet "$root:{BASELINE_PATH}" 2>/dev/null || true; '
    "fi; "
    'echo "@@"; '
    f"git hash-object {BASELINE_PATH} 2>/dev/null; "
    'echo "@@"; '
    'if [ -n "$root" ]; then git log -1 --format=%s "$root" 2>/dev/null; fi; '
    "true"
)


def parse_baseline_identity(stdout: str) -> BaselineIdentity:
    """Pure parse of `_BASELINE_SCRIPT`'s three `@@`-separated fields.

    Split out for the same reason `parse_state` was: the parse is the part with the edge cases and
    it is fully testable without a container, while the probe around it is one exec. A truncated or
    otherwise malformed body reads as `UNANSWERABLE` — `partition` yields empty strings for the
    fields that were not there, and every empty field already denies."""
    roots_text, _, rest = stdout.partition("@@")
    baseline_text, _, rest = rest.partition("@@")
    working_text, _, subject_text = rest.partition("@@")
    roots = [line for line in roots_text.split() if line]
    if len(roots) != 1:
        # No root commit at all (no repository, or an unreadable one), or more than one. Both are
        # structural: there is no single birth certificate to compare against, and re-running the
        # probe will keep saying so.
        return BaselineIdentity.UNANSWERABLE
    if subject_text.strip() != BASELINE_COMMIT_SUBJECT:
        # THE ROOT IS NOT THE SEEDED TEMPLATE — the agent re-initialised the repository in its
        # own shell, or fetched an unrelated history over it. Either way there is no birth
        # certificate to compare against, and answering "still the template" on a root that IS
        # the app is how a working app gets locked out of ever completing again.
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
    """Ask one container whether its root route is still the seeded baseline.

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


# THE AGENT'S LAST CHANGE, as the container sees it.
#
# A MARKER FILE AND `find -newer`, NOT A TIMESTAMP COMPARISON. The obvious implementation reads
# the newest mtime with `stat -c %Y` and compares two numbers — and `stat -c` is GNU coreutils,
# `stat -f %m` is BSD, and neither is POSIX. The sandbox image is Debian today, but a probe whose
# failure mode is "returns nothing, so the check silently never runs" is the worst shape a guard
# can have: it would read exactly like a feature that works. `touch` and `find -newer` are both
# POSIX, so this cannot quietly stop working on a different base image.
#
# THE MARKER LIVES IN `/tmp`, NEVER IN THE WORKSPACE. A file under `/workspace/app` would show up
# in `git status --porcelain`, which means it would make the tree look dirty to `_nothing_to_lose`,
# to the save-state indicator and to the recovery-write gate — a watermark that changes the answer
# to the question it exists to help ask.
#
# The heavy trees are pruned, and not only for speed: `next dev` rewrites `.next` on every compile,
# so including it would make "the agent changed something" true forever.
_WATERMARK_PATH: Final = "/tmp/.bial-agent-watermark"  # noqa: S108 - see the note above

_STAMP_WATERMARK_SCRIPT: Final = f"touch {_WATERMARK_PATH}"

_CHANGED_SINCE_SCRIPT: Final = (
    "find . \\( -name node_modules -o -name .next -o -name .git \\) -prune -o "
    # …AND THE FILES THE TOOLCHAIN REWRITES ON ITS OWN. `next dev` regenerates `next-env.d.ts`
    # and normalises `tsconfig.json` on every boot — that is why `FRAMEWORK_CHURN` exists at all
    # — so leaving them in makes "the agent changed something" true on essentially every pass,
    # whether it did or not. A watermark that is always true is not a watermark.
    "-name next-env.d.ts -prune -o -name tsconfig.json -prune -o "
    f"-type f -newer {_WATERMARK_PATH} -print 2>/dev/null "
    "| head -n 1"
)

_CHANGED_SINCE_GUARDED: Final = (
    # THE MARKER HAS TO EXIST FOR THE QUESTION TO MEAN ANYTHING, and without this guard nothing
    # would say so: a shell pipeline reports the status of its LAST command, and `head` exits 0
    # on empty input whatever `find` did. So a missing marker — a failed stamp, or a container
    # restarted with a fresh `/tmp` — produced an empty answer at exit 0, which reads as "nothing
    # changed" rather than "we could not tell". That is the wrong direction on a guard: it turns
    # the re-check off silently, exactly when the container is misbehaving.
    f"[ -f {_WATERMARK_PATH} ] || exit 1; " + _CHANGED_SINCE_SCRIPT
)


async def stamp_the_watermark(sandbox_client: SandboxClient, handle: SandboxHandle) -> bool:
    """Mark "now" in the container, so a later question can ask what changed after it.

    Returns whether the mark was actually laid down. `False` is not an error and callers must not
    treat it as one — it means the follow-up question has no reference point, so the answer to
    "did anything change" will be "we cannot tell", which is the arm that changes nothing."""
    run_command = sandbox_client.exec  # aliased to keep the call off the JS-oriented exec guard
    try:
        result = await run_command(handle, ["sh", "-c", _STAMP_WATERMARK_SCRIPT], timeout_s=30)
    except SandboxError:
        _log.warning("watermark_stamp_failed", app=handle.app_name, exc_info=True)
        return False
    return result.exit == 0


async def anything_changed_since_the_watermark(
    sandbox_client: SandboxClient, handle: SandboxHandle
) -> bool | None:
    """Has anything in the workspace been written since `stamp_the_watermark`?

    Checks the filesystem, not tool calls: the agent edits via `run_command` as freely as via the
    file tools, so a tool-call watermark would miss every `sed`, install, or shell redirect.

    `None` (could not tell) is deliberately NOT folded into `False`: the caller treats `None` as
    "change nothing" — today's behaviour — so an unreadable container costs the improvement, not
    correctness."""
    run_command = sandbox_client.exec  # aliased to keep the call off the JS-oriented exec guard
    try:
        result = await run_command(handle, ["sh", "-c", _CHANGED_SINCE_GUARDED], timeout_s=30)
    except SandboxError:
        _log.warning("watermark_compare_failed", app=handle.app_name, exc_info=True)
        return None
    if result.exit != 0:
        return None
    return bool(result.stdout.strip())


async def has_ever_been_built(app_id: uuid.UUID) -> bool:
    """Has any turn on this app ever done real work? Meaningless pre-build — a fresh project is
    *supposed* to show the starter page.

    Answered by RECOVERY-COPY presence (no `turns` model; `finish_turn_sandbox` writes one per
    file-touching turn), not `newest_restore_source(...) is not None` (answers "newer than saved?",
    a different question, and can raise). Unconfigured storage -> `False`; unreadable -> `True`,
    fail-closed: a false "not built" beats a completion claim over an untouched template."""
    try:
        store = get_storage()
    except StorageUnconfiguredError:
        return False
    try:
        return await store.head(recovery_key(app_id)) is not None
    except StorageError:
        _log.warning("prior_building_turns_unreadable", app_id=str(app_id), exc_info=True)
        return True


# ─────────────────────────────────────────────────────────────────────────────────────────
# WHAT THE CONTAINER STILL HOLDS.
#
# `_resolve_sandbox` consults the workspace verdict before every turn, and a verdict that
# imported `state_script` back from `manager` would be a module-level cycle — and broken
# in-function it would still drag `api.v1.build_sessions.schemas` and `pydantic_ai` into
# everything that asks, including the reaper, which goes out of its way not to load them
# (`test_the_reaper_imports_without_the_fastapi_app`). `manager.py` and `reaper.py` import them
# from here.
# ─────────────────────────────────────────────────────────────────────────────────────────

# One round trip for every half of the question. `|| true` keeps a repo-less tree from
# failing the whole script.
#
# The porcelain read is capped: the caller needs to know whether the tree is empty and, when
# it is not, WHICH files changed — and a listing long enough to hit this cap has already
# answered the only question the cap could interfere with (a tree this dirty is real work, not
# two files of framework churn). Hitting it therefore sets `porcelain_truncated` and short-
# circuits the comparison rather than reasoning about a half-read list.
#
# ONE constant feeds both the shell cap and the truncation test. They were 200 and 400 in the
# first cut, which made `porcelain_truncated` unreachable and quietly deleted the backstop.
PORCELAIN_CAP_BYTES: Final = 200

# Files the FRAMEWORK rewrites on its own, with no user or agent involved. `next dev`
# regenerates `next-env.d.ts` and normalises `tsconfig.json` on every boot, so a container that
# has merely STARTED reports a dirty tree — observed live on a workspace whose only history was
# one Plan question. Treating that as "unsaved changes" is what let an empty template lock a
# user out of the project holding their real app.
#
# Scoped deliberately tight. This set is ONLY consulted when deciding whether a workspace is
# empty enough to reclaim or to declare reverted; it never suppresses anything the user is
# shown, and the Save button still offers to save these (they are legitimately part of the
# tree). Add to it only for files the toolchain writes unprompted — never for anything a person
# or the agent would edit.
FRAMEWORK_CHURN: Final = frozenset({"next-env.d.ts", "tsconfig.json"})

# THE SAME QUESTION, ASKED WHERE THE WRONG ANSWER COSTS SOMEBODY THEIR WORK.
#
# `FRAMEWORK_CHURN` above answers "may this container be reclaimed without asking", where a false
# negative merely means we ask. The SAVE INDICATOR asks the opposite-facing question — "is there
# anything here to lose" — and there a false negative is the platform reporting "Everything is
# saved" over work nobody saved.
#
# `tsconfig.json` CANNOT BE TREATED AS CHURN ON THAT SIDE, because the model is explicitly invited
# to edit it: `prompt_blocks.py` lists "package.json, tsconfig.json, postcss.config.mjs,
# components.json  -- editable". An agent adding a path alias is doing exactly what it was told it
# may do, and folding that into "the framework rewrote it" makes the citizen's own change invisible
# to the one indicator that exists to tell them about it.
#
# `next-env.d.ts` stays: Next regenerates it on boot, the file carries its own do-not-edit banner,
# and it appears nowhere in the prompt's editable list -- so nothing the agent is permitted to do
# produces a change to it.
#
# THE ASYMMETRY IS THE POINT. A wrongly-dirty tree costs a save nobody needed. A wrongly-clean one
# costs the build. Where the two cannot both be served, this side fails DIRTY.
REGENERATED_ONLY: Final = frozenset({"next-env.d.ts"})

# A commit sha, and nothing else, may be interpolated into the script below.
#
# THE REFERENCE SHA IS NOT OURS. It is read back from a stored bundle's blob metadata, which is
# a value the platform wrote but the store returned — and the composed string goes to `sh -c`.
# Seven characters is git's own minimum abbreviation, forty its full length. A sha that fails
# this is a fact about the metadata, not about the container: it will not become well-formed on
# a retry, so it is `UNVERIFIABLE` rather than `UNREADABLE`, and it never reaches the shell.
_SHA_RE: Final = re.compile(r"^[0-9a-f]{7,40}$")


def is_a_commit_sha(value: str | None) -> bool:
    """May this value be interpolated into the probe's shell string? (See `_SHA_RE`.)

    Exposed rather than kept private because the guarded recovery write asks the same question
    of the same metadata before composing the same script — and a second, subtly different
    spelling of "is this a sha" is how one of the two would eventually let something else
    through."""
    return value is not None and _SHA_RE.match(value) is not None


_STATE_FIELDS: Final = (
    'git rev-parse HEAD 2>/dev/null || true; echo "@@"; '
    f'git status --porcelain 2>/dev/null | head -c {PORCELAIN_CAP_BYTES}; echo "@@"; '
    'git rev-list --count HEAD 2>/dev/null || true; echo "@@"'
)


def state_script(reference_sha: str | None) -> str:
    """The container-state probe, optionally asking where HEAD sits relative to `reference_sha`.
    Four `@@`-fields always: HEAD, capped porcelain, commit count, ancestry.

    Ancestry empty means `NOT_ASKED`, distinct from "asked, couldn't tell" — conflating them lets
    a probe that silently stopped running read as healthy. It's two exit codes, in ORDER: `git
    cat-file -e` (is the reference even here?) then `--is-ancestor` — reversed, a missing object
    misreads as "diverged" for reasons unrelated to lineage. `reference_sha` is trusted
    `_SHA_RE`-shaped; callers validate first, or pass `None`."""
    if reference_sha is None:
        return _STATE_FIELDS
    return (
        f"{_STATE_FIELDS}; "
        f"git cat-file -e {reference_sha} 2>/dev/null; a=$?; "
        f"git merge-base --is-ancestor {reference_sha} HEAD 2>/dev/null; b=$?; "
        'printf "%s %s" "$a" "$b"'
    )


class Ancestry(enum.StrEnum):
    """Where the container's HEAD sits relative to the tree a restore would hand back."""

    #: Nobody asked — no reference sha was supplied.
    NOT_ASKED = "not_asked"
    #: HEAD *is* the reference, or was built on top of it. The normal shape of a healthy turn.
    DESCENDANT = "descendant"
    #: The reference is in this repository and HEAD is not below it. `git reset --hard`,
    #: `--amend` and `rebase` all produce this over a perfectly good tree, which is why this
    #: alone never authorises anything.
    NOT_DESCENDANT = "not_descendant"
    #: The repository does not contain the reference at all — the agent re-initialised it, or
    #: the metadata names a tree that never lived here.
    REFERENCE_ABSENT = "reference_absent"
    #: The field came back in a shape this parse does not recognise.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ContainerState:
    """What the container says about itself.

    `head is None` means NO `.git` AT ALL, not "nobody has saved yet" — a provisioned container
    is never commit-less (`client._INIT_REPO_SCRIPT` seeds a baseline commit at birth). So only a
    container running straight from its baked image produces `head is None`; reading it as an
    unsaved project's normal state is what lets a factory-reset container pass for a new one."""

    head: str | None
    uncommitted: bool
    # The paths git reports as changed, parsed out of the porcelain. `uncommitted` answers
    # "is anything different?"; this answers "different HOW", which is what tells framework
    # churn apart from the user's work. Empty when the tree is clean OR when the porcelain
    # was truncated (see `porcelain_truncated`).
    changed_paths: tuple[str, ...]
    # The porcelain is capped, so a very dirty tree comes back cut off. That is not a state
    # to reason about — it is unambiguous evidence of real work.
    porcelain_truncated: bool
    # How many commits deep HEAD is. A FRESH PROVISION IS ALWAYS 1, never 0 (the seeded
    # baseline commit above), so "no commit yet" is not a state that occurs on a provisioned
    # container, and anything asking "is there work in here?" has to compare against the
    # baseline rather than against nothing. 0 means we could not count.
    commits: int
    # Where HEAD sits relative to the reference sha the probe was given, or `NOT_ASKED`.
    ancestry: Ancestry = Ancestry.NOT_ASKED


def parse_state(stdout: str) -> ContainerState:
    """Pure parse of `state_script`'s four `@@`-separated fields, split out so it is testable
    without a container — the offset bug it now pins was invisible to every fake."""
    head_text, _, rest = stdout.partition("@@")
    porcelain, _, rest = rest.partition("@@")
    count_text, _, ancestry_text = rest.partition("@@")
    try:
        commits = int(count_text.strip() or 0)
    except ValueError:
        commits = 0
    # `XY path` per line; a rename is `XY old -> new` and the destination is the one that
    # matters. Anything unparseable is kept verbatim rather than dropped — a path we cannot
    # read must never silently shrink the change set.
    # `XY path`, two status columns then the path. Split on WHITESPACE rather than slicing a
    # fixed offset: the block gets stripped before it reaches here, so the first line has
    # already lost its leading status space and a `line[3:]` silently ate the first character
    # of its filename (`next-env.d.ts` -> `ext-env.d.ts`, which then matched nothing). A
    # one-shot split is also correct for paths containing spaces, which a fixed offset is not.
    paths: list[str] = []
    for line in porcelain.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        entry = parts[1].strip()
        if "->" in entry:  # a rename: the destination is the file that now exists
            entry = entry.split("->")[-1].strip()
        if entry:
            paths.append(entry.strip('"'))
    # BYTES, because `head -c` counts bytes and a non-ASCII filename would make the character
    # count read short. `>=` rather than `>`: output that lands exactly on the cap is
    # indistinguishable from output that was cut there, and "assume truncated" is the arm that
    # refuses a reclaim rather than the one that permits it.
    trimmed = porcelain.strip()
    return ContainerState(
        head=head_text.strip() or None,
        uncommitted=bool(trimmed),
        changed_paths=tuple(paths),
        commits=commits,
        porcelain_truncated=len(trimmed.encode("utf-8", "surrogateescape")) >= PORCELAIN_CAP_BYTES,
        ancestry=_parse_ancestry(ancestry_text),
    )


def _parse_ancestry(field: str) -> Ancestry:
    """The fourth field: `git cat-file -e`'s exit code, then `git merge-base --is-ancestor`'s.

    An EMPTY field is `NOT_ASKED` — the caller supplied no reference. Anything else that does
    not parse into two integers is `UNREADABLE`, and the difference is the whole point: the
    verdict reads `NOT_ASKED`-when-a-reference-was-given as a container that answered in a shape
    we do not understand, which is a retry, not a judgement."""
    parts = field.split()
    if not parts:
        return Ancestry.NOT_ASKED
    if len(parts) != 2:
        return Ancestry.UNREADABLE
    try:
        exists_exit, ancestor_exit = int(parts[0]), int(parts[1])
    except ValueError:
        return Ancestry.UNREADABLE
    if exists_exit != 0:
        return Ancestry.REFERENCE_ABSENT
    if ancestor_exit == 0:
        return Ancestry.DESCENDANT
    if ancestor_exit == 1:
        # git's documented "no" for `--is-ancestor`. Any OTHER non-zero exit is an error
        # (a bad revision, a broken repository), and reading those as "diverged" would let a
        # transport-level problem look like a lineage judgement.
        return Ancestry.NOT_DESCENDANT
    return Ancestry.UNREADABLE


async def container_state(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    *,
    reference_sha: str | None = None,
) -> ContainerState | None:
    """The container's commit AND whether its working tree has uncommitted changes.

    BOTH halves are read because the sha alone cannot see uncommitted work: comparing commits
    only would report "all changes saved" over a tree the agent had written and not committed,
    and the porcelain is the only thing here that can see it.

    None means we could not ask at all, which is the only honest "unknown"."""
    run_command = sandbox_client.exec  # alias keeps the call off the JS-oriented exec guard
    try:
        result = await run_command(handle, ["sh", "-c", state_script(reference_sha)], timeout_s=30)
    except SandboxError:
        return None
    if result.exit != 0:
        return None
    return parse_state(result.stdout)


# ─────────────────────────────────────────────────────────────────────────────────────────
# THE WORKSPACE INTEGRITY VERDICT.
#
# A CONTAINER THAT HAS FACTORY-RESET TO ITS BAKED IMAGE PRESENTS EXACTLY AS A PROJECT NOBODY HAS
# BUILT YET: a running dev server, a clean tree, one commit. No single one of those signals can
# tell the two apart, which is why the verdict below is built out of several that must agree.
#
# ONLY A POSITIVE CONFIRMATION OF LOSS AUTHORISES ANYTHING. `may_restore` is true for exactly
# one state, and `REVERTED` requires THREE independent facts to agree: the lineage is broken,
# the tree is empty, and this app has been built before. The two unanswerable states are
# separated on ONE axis — whether trying again could help — because they need opposite
# handling, and collapsing them is how a user gets locked out of their own project by a
# supervisor blip.
# ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntegrityVerdict:
    """The answer, plus the facts the callers and the alarm payload need.

    `content_empty` is carried rather than recomputed because the quarantine write is gated on
    it: in the factory-reset case the tree being set aside IS the baked template, and bundling
    it costs a full `git bundle` + base64 + upload on the slowest path in the system to preserve
    nothing."""

    state: WorkspaceState
    reason: str
    #: The tree holds nothing beyond the seeded baseline — OR there is no repository at all, in
    #: which case nothing can be read from it. This is the REVERTED predicate; it is NOT the
    #: question "is there anything here worth keeping", which is `provably_bare` below.
    content_empty: bool = False
    #: We can SEE the tree and it is the starter template: one commit, clean modulo framework
    #: churn. Positive knowledge, and the distinction from `content_empty` is load-bearing.
    #:
    #: A container with no `.git` at all reports an empty porcelain, so it satisfies
    #: `content_empty` whether its working directory holds the bare template or somebody's
    #: finished app with the repository deleted out from under it. Gating the quarantine write
    #: on `content_empty` would therefore do two bad things at once: skip the write on EVERY
    #: reversion (since `REVERTED` requires `content_empty` by construction, so the write would
    #: be unreachable code), and skip it precisely in the case where the working tree is the
    #: only surviving copy of the user's app. Quarantine unless we positively know there is
    #: nothing to quarantine.
    provably_bare: bool = False
    #: The container's HEAD at the moment of the verdict, for the alarm payload.
    head: str | None = None
    #: The bundle this verdict compared against — the one a restore would hand back.
    reference_key: str | None = None
    #: Whether ANY durable copy exists for this app (recovery OR saved). `False` under
    #: `REVERTED` is the no-source arm: tell the user plainly, restore nothing.
    durable_copy_exists: bool = False

    @property
    def may_restore(self) -> bool:
        """The single question every caller actually asks.

        A property rather than a comparison at each call site, for the reason
        `CopyVerdict.may_destroy` documents: `state is REVERTED` spelled out at four call sites
        is four chances to write `is not INTACT` and quietly authorise the two states that mean
        "we could not tell"."""
        return self.state is WorkspaceState.REVERTED


@dataclass(frozen=True)
class _DurableFacts:
    """What the object store says, gathered once so the judgement below stays pure."""

    recovery_present: bool
    saved_present: bool
    reference_key: str
    #: The `head_sha` stamped on the reference bundle. `None` is NO CLAIM — an object written
    #: before the stamp existed, or one carrying an empty one.
    reference_sha: str | None
    #: The stamp is present but is not a sha. Structural: metadata does not heal on a retry.
    reference_sha_malformed: bool

    @property
    def any_copy(self) -> bool:
        return self.recovery_present or self.saved_present


# THE CAP THAT KEEPS AN UNANSWERABLE CHECK FROM BECOMING A LOCKOUT.
#
# `UNREADABLE` fails the turn as retryable, which is right once and wrong forever: a container
# whose exec endpoint has genuinely stopped answering would refuse the user their project on
# every message, with a retry prompt that can never succeed. After this many consecutive
# unreadable answers for one app, the next is `UNVERIFIABLE` instead — proceed, alarm, restore
# nothing, refuse the recovery write. Degraded, not locked out.
#
# Process-local, which matches the single-replica deploy contract `reaper.py` already depends
# on, and self-pruning: any verdict that is not `UNREADABLE` drops the entry.
_UNREADABLE_STREAK_CAP: Final = 2
_unreadable_streak: dict[uuid.UUID, int] = {}


def reset_integrity_streaks_for_tests() -> None:
    """Drop the per-app unreadable counters. Process-local state, so tests that exercise the
    cap must not leak a partial streak into the next one."""
    _unreadable_streak.clear()


def judge_workspace(container: ContainerState, facts: _DurableFacts) -> IntegrityVerdict:
    """The four-state decision, as a pure function over facts already gathered.

    Split out for the reason `parse_state` was: this is the part with the edge cases, and every
    one of them is testable without a container or a store."""
    empty_tree = container.commits == 1 and clean_but_for_churn(container)
    content_empty = container.head is None or empty_tree

    def verdict(state: WorkspaceState, reason: str) -> IntegrityVerdict:
        """Every arm carries the same facts; only the state and the sentence differ."""
        return IntegrityVerdict(
            state,
            reason,
            content_empty=content_empty,
            provably_bare=empty_tree,
            head=container.head,
            reference_key=facts.reference_key,
            durable_copy_exists=facts.any_copy,
        )

    # NEVER-BUILT COMES FIRST, and it is `_nothing_to_lose`'s four conditions rather than
    # `head is None`. A brand-new project is SUPPOSED to hold nothing: calling that a reversion
    # would quarantine and "restore" every first message anyone ever sends.
    #
    # `commits == 1`, not `<= 1`: a count of 0 means the probe could not answer, and unknown is
    # not permission — exactly as `_nothing_to_lose` already refuses it.
    if not facts.any_copy and empty_tree:
        return verdict(WorkspaceState.INTACT, "this project has never been built")

    if container.head is None:
        # NO REPOSITORY AT ALL. A provisioned container is never repo-less:
        # `client._INIT_REPO_SCRIPT` seeds a baseline commit at birth. So only a container
        # running straight from its baked image answers this way — and it is the one shape where
        # the lineage question needs no ancestry answer, because there is no lineage left to be a
        # descendant of.
        #
        # DELIBERATELY AHEAD OF THE "no durable copy" ARM. A container whose turn-end autosave
        # silently failed — a live state, not a hypothetical — and which then factory-resets has
        # NO durable copy at all, so an ordering that reached the "nothing to compare against,
        # carry on" arm first would let the agent build on the wiped tree.
        return verdict(WorkspaceState.REVERTED, "the workspace has no repository at all")

    if not facts.any_copy:
        # A repository exists and holds work, and there is nothing durable to compare it
        # against. No loss to report and nothing to restore from — the turn proceeds, and the
        # turn-end recovery write makes this app's first copy.
        return verdict(WorkspaceState.INTACT, "no durable copy exists to compare against")

    if facts.reference_sha_malformed:
        return verdict(
            WorkspaceState.UNVERIFIABLE, "the durable copy's head_sha is not a commit sha"
        )

    if facts.reference_sha is None:
        # NO CLAIM, which `head_sha_from_metadata` documents as its own answer. A bundle
        # predating the stamp is a documented live state and no retry adds it — so this is
        # structural, and it must never be `REVERTED`: accusing a workspace of reversion on the
        # strength of a missing metadata key is the false positive that destroys work.
        return verdict(WorkspaceState.UNVERIFIABLE, "the durable copy carries no head_sha")

    if container.ancestry in (Ancestry.NOT_ASKED, Ancestry.UNREADABLE):
        # A reference WAS supplied, so an empty or unparseable ancestry field means the
        # container answered in a shape we do not understand. Retryable — the next probe may
        # well parse — and the streak cap above stops that becoming a lockout.
        return verdict(WorkspaceState.UNREADABLE, "the container's ancestry answer did not parse")

    if container.ancestry is Ancestry.REFERENCE_ABSENT:
        # The repository does not contain the tree the durable copy claims. Structural — the
        # agent re-initialised the repo, or the metadata names a tree that never lived here —
        # and NOT `REVERTED`, deliberately, even though this reads like strong evidence of loss.
        # `--is-ancestor` never ran, so the "is the lineage broken" question was not answered by
        # git; it was answered by the object being missing, which has innocent explanations.
        # The conservative arm still protects the user: no restore, an alarm, and the recovery
        # write is refused, so the good bundle survives for an operator to promote.
        return verdict(
            WorkspaceState.UNVERIFIABLE, "the durable copy's tree is not in this repository"
        )

    if container.ancestry is Ancestry.DESCENDANT:
        return verdict(WorkspaceState.INTACT, "the workspace still holds this app's work")

    # NOT_DESCENDANT — the lineage moved. Which of two things that is depends ENTIRELY on the
    # tree, and this is the line the unit turns on.
    if content_empty:
        return verdict(WorkspaceState.REVERTED, "the workspace was reset to an empty template")
    # LINEAGE BROKEN OVER A TREE THAT STILL HOLDS CONTENT. `git reset --hard`, `--amend` and
    # `rebase` all produce exactly this over a perfectly good workspace — and the Write prompt
    # still teaches `git checkout` / `git revert` for undo. Destroying that tree to "recover"
    # would be a NEW data-loss path, invented by the guard meant to close one.
    return verdict(
        WorkspaceState.UNVERIFIABLE, "the lineage moved but the workspace still holds content"
    )


def clean_but_for_churn(container: ContainerState) -> bool:
    """Is the tree empty of anything a person or the agent would have written?

    The tree half of `_nothing_to_lose`, reused rather than re-derived — a second, subtly
    different spelling of "is this workspace empty" is how the two would drift into disagreeing
    about whether a container may be destroyed."""
    if container.porcelain_truncated:
        return False  # too much changed to enumerate, which is itself evidence of real work
    return all(path in FRAMEWORK_CHURN for path in container.changed_paths)


def only_regenerated_files_changed(container: ContainerState) -> bool:
    """Is every change in this tree a file the FRAMEWORK rewrites and the agent may not touch?

    The save indicator's question, and deliberately stricter than `clean_but_for_churn` -- see
    `REGENERATED_ONLY` for why the two sets differ and which way each one fails.

    Fails closed on a truncated porcelain for the same reason its sibling does: output that hit the
    cap is unambiguous evidence of real work, and `changed_paths` is empty there, which a bare
    `all(...)` would otherwise read as "nothing but churn". An EMPTY change set answers False too:
    this is only ever asked of a tree git already called dirty, so "no paths" there means the
    porcelain could not be parsed, not that the tree is clean."""
    if container.porcelain_truncated:
        return False
    return bool(container.changed_paths) and all(
        path in REGENERATED_ONLY for path in container.changed_paths
    )


def holds_unsaved_work(container: ContainerState) -> bool:
    """Is there anything in this tree a durable copy would be poorer for missing?

    The save side's question in its positive sense, and the ONE spelling of it — the composition
    is what the predicate above is *for*, and two call sites writing it out by hand is how one of
    them eventually forgets the `uncommitted` half or reaches for `clean_but_for_churn` instead.
    Reaching for the reap side's twin here would forgive `tsconfig.json`, which the agent is
    invited to edit: a wrong answer on this side costs the citizen that change, where on the reap
    side it costs a confirmation prompt."""
    return container.uncommitted and not only_regenerated_files_changed(container)


async def workspace_integrity(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    restore_source_key: str | None,
) -> IntegrityVerdict:
    """Does this container still hold this app's work?

    `restore_source_key` names the bundle to compare against — the one the caller would actually
    restore, so the verdict's answer matches the tree the user would get back. `None` means the
    saved bundle (matches `newest_restore_source`'s convention, so its result passes straight
    in); Save-between-turns can make the two bundles disagree, which is why this is the caller's
    choice. Never raises: every failure is unanswerable, so a blip can't fail a turn."""
    try:
        store = get_storage()
    except StorageUnconfiguredError:
        # With no store there can be no durable copy for anyone, so there is nothing to compare
        # against and nothing to restore from — the turn proceeds, silently, which is what keeps
        # local development working.
        return IntegrityVerdict(WorkspaceState.INTACT, "the object store is not configured")

    try:
        recovery = await store.head(recovery_key(app_id))
        saved = await store.head(snapshot_key(app_id))
    except StorageError:
        _log.warning("workspace_integrity_store_unreadable", app_id=str(app_id), exc_info=True)
        return _remember_unreadable(app_id, "the object store could not be read")

    reference_key = restore_source_key if restore_source_key is not None else snapshot_key(app_id)
    reference_meta = recovery if reference_key == recovery_key(app_id) else saved
    stamped = head_sha_from_metadata(reference_meta.metadata if reference_meta else None)
    malformed = stamped is not None and _SHA_RE.match(stamped) is None
    facts = _DurableFacts(
        recovery_present=recovery is not None,
        saved_present=saved is not None,
        reference_key=reference_key,
        reference_sha=None if malformed else stamped,
        reference_sha_malformed=malformed,
    )

    # A MALFORMED SHA NEVER REACHES THE SHELL. The probe still runs — one exec, so the alarm
    # payload carries the container's real state rather than nothing — but with no reference,
    # and `judge_workspace` returns `UNVERIFIABLE` on the metadata alone.
    container = await container_state(
        sandbox_client, handle, reference_sha=None if malformed else stamped
    )
    if container is None:
        return _remember_unreadable(app_id, "the container did not answer")

    verdict = judge_workspace(container, facts)
    if verdict.state is WorkspaceState.UNREADABLE:
        return _remember_unreadable(app_id, verdict.reason, template=verdict)
    _unreadable_streak.pop(app_id, None)
    return verdict


def _remember_unreadable(
    app_id: uuid.UUID, reason: str, *, template: IntegrityVerdict | None = None
) -> IntegrityVerdict:
    """Count one unanswerable check, converting to `UNVERIFIABLE` once the streak is spent.

    The conversion DROPS the counter rather than letting it climb: the streak has been resolved
    into a state the caller acts on, and leaving it set would make every later probe for this
    app structural even after the container started answering again."""
    streak = _unreadable_streak.get(app_id, 0) + 1
    base = template or IntegrityVerdict(WorkspaceState.UNREADABLE, reason)
    if streak > _UNREADABLE_STREAK_CAP:
        _unreadable_streak.pop(app_id, None)
        _log.error(
            "workspace_integrity_unanswerable",
            app_id=str(app_id),
            reason=reason,
            consecutive=streak,
        )
        return replace(
            base,
            state=WorkspaceState.UNVERIFIABLE,
            reason=f"{reason} (after {_UNREADABLE_STREAK_CAP} consecutive retries)",
        )
    _unreadable_streak[app_id] = streak
    return replace(base, state=WorkspaceState.UNREADABLE, reason=reason)
