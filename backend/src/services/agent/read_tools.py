"""The shared read-only tool surface (U8 / R6 / plan 2026-07-22-002).

Four tools — bounded `read_file`, `list_files`, `search_files`, and an allowlisted
read-only `run_command` — built as a `FunctionToolset` factory over a `ReadOnlyWorkspace`
Protocol. The Protocol is the routing seam: a workspace resolved from a local snapshot
extraction dir (`ExtractedSnapshotWorkspace`) answers about saved code, and one resolved
from the live sandbox through the supervisor (`LiveSandboxWorkspace`) answers about the tree
in front of the model instead of a stale bundle. WHICH workspace is a fact about the run, not
about the ability: what these tools ALLOW is written down once, here, and no body below asks
which agent or which chat kind resolved it (R71).

Containment model for `run_command`, layered fail-closed:
- exec-style argv only, no shell — pipes, redirection, and chaining are structurally
  impossible, and `sh`/`bash` are not on the guest list;
- argv[0] must be on `_GUEST_LIST` (POSIX read-only classics), with per-command deny
  flags catching each binary's write-capable forms (`sed -i`, `find -delete`/`-exec`/
  `-fprintf`, `tail -f`); `sed` additionally goes through a script validator because its
  DANGER lives in the script argument (`w`/`W` write files, GNU `e` executes) — only the
  numeric range-print form (`sed -n '40,80p' file`) is admitted;
- every argv token is vetted against path escape (absolute, `~`, `..` segments);
- WHERE it runs depends on the workspace, and the two differ materially. On the LIVE
  container (the normal case) the command goes through the supervisor into the app's
  own environment — which holds `BIAL_DATABASE_URL` and a Blob SAS — with the supervisor's
  own timeout and secret redaction. On the snapshot fallback (only when no sandbox service
  is configured) it runs cwd-jailed on the control-plane server under `_minimal_env`, an
  explicit allowlist carrying no DSN and no tokens. The POLICY above is identical either
  way; the surroundings are not, and the richer one is now the normal case. Output on both
  is capped → de-escaped → secret-redacted → de-noised → cut to HEAD AND TAIL with the loss
  stated (U22/R28; mirrors `orchestrator/tools._redact_command_output`, reimplemented here
  so this module never imports the build agent's tool module, whose import registers tools
  on `build_agent` — one table-driven test runs both copies and pins them identical).

THE FOUR TOOL DOCSTRINGS IN `read_only_toolset` ARE PROMPT COPY (U20 / R26). pydantic-ai
sends each as the tool's description at registration, and `list_files`/`search_files` are
additionally rendered into the Write prompt's generated `TOOL SURFACE` block
(`agent/toolsets.render_tool_surface`), so editing one is editing a prompt and
`test_prompt.py`'s drift check says so until the snapshot is regenerated. Write the FIRST
SENTENCE as the line you want in the prompt.

Refusals are teaching `ModelRetry`s in the U1 sentinel's voice — they say WHY and what to
do instead, so the model self-corrects rather than retrying blind. There is no "no app
exists yet" answer here any more: the workspace a turn is given comes from one arm
(`turns/engine._pin_workspace`, which always resolves the project's live container), so the
emptiness signal never arrives and the workspace that used to produce it is gone — which is
why no segment promises the model one (U20; pinned by
`test_no_segment_promises_an_emptiness_signal_that_never_arrives`).

`psql` is never on this allowlist (plan, locked).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.core.redaction import (
    cut_before_an_open_credential,
    leaves_a_credential_value_open,
    scrub_untrusted,
)

if TYPE_CHECKING:
    # Annotation-only, deliberately: `LiveSandboxWorkspace` holds a `SandboxSession`, but this
    # module must add NO runtime edge into the orchestrator package — the same reason the module
    # docstring gives for mirroring the output redaction instead of importing it.
    from src.services.orchestrator.deps import SandboxSession

# ---------------------------------------------------------------------------------------
# Bounds. The plan defers exact tuning ("tuned during implementation against real traces")
# — these mirror the orchestrator's proven values where a twin exists.
# ---------------------------------------------------------------------------------------

READ_MAX_LINES = 400
"""Line window per `read_file` call (mirrors the sandbox `VIEW_MAX_LINES`)."""

READ_FILE_MAX_BYTES = 256_000
"""Hard byte cap read from any one file — a bundler artifact can't blow the context."""

LIST_MAX_ENTRIES = 500
"""Cap on `list_files` entries; deeper trees get an explicit truncation marker."""

SEARCH_MAX_HITS = 200
"""Cap on `search_files` matches."""

SEARCH_WALL_CLOCK_S = 2.0
"""Search budget: a pathological pattern gets a partial (marked) result, never a spin."""

SEARCH_LINE_SCAN_CHARS = 1_000
"""Only the first N chars of each line are pattern-scanned (backtracking containment)."""

READ_EXEC_TIMEOUT_S = 15.0
"""Read-only commands are metadata-cheap; anything longer is wedged, not working."""

_REDACT_INPUT_MAX_CHARS = 32_000
_OUTPUT_MAX_CHARS = 16_000
"""The dump budget, mirroring `orchestrator/constants.RUN_COMMAND_OUTPUT_MAX_CHARS`. Read mode
renders under it on BOTH exit paths — see the U22 block below for why a success is not summarised
on a surface that has no slice handle."""

# Heavy or history dirs the read surface refuses everywhere (list, search, read): they are
# build artifacts or plumbing, never app truth. `.git` also hides the extraction's plumbing.
# Public (with `IGNORED_FILES` below) so the pre-publish credential scan can walk the tree
# under the exact exclusions the model reads under.
IGNORED_DIRS = frozenset({".git", "node_modules", ".next", "dist", ".turbo"})

# Dependency lock files, refused at every site the directory set is applied (R22a): a lockfile
# is the single largest file in a generated app and carries no signal worth its tokens.
# Deliberately MIRRORS the sandbox toolset's `orchestrator/constants.READ_IGNORE_FILES` rather
# than importing it — the two toolsets are separate surfaces, and this module must add NO
# runtime edge into the orchestrator package (the same reason the module docstring gives for
# mirroring the output redaction). Matched on the FULL file name, never a substring.
IGNORED_FILES = frozenset({"package-lock.json", "pnpm-lock.yaml", "yarn.lock"})


def is_dependency_lockfile(path: Path) -> bool:
    """A lockfile is the name BESIDE THE MANIFEST IT RESOLVES — never the name alone.

    `npm`/`pnpm`/`yarn` write the lock file next to the `package.json` it locks, so that
    pairing is what actually identifies one. Matching the bare name at any depth was a
    hole rather than a saving: a file named `app/config/yarn.lock` is ordinary source, and
    the name hid it from BOTH the model and the model-free credential sweep while it still
    shipped — the deploy packaging step does not exclude lockfiles, so a credential parked
    there survived the whole gate.

    Anchoring to the manifest keeps every byte of the intended saving, INCLUDING the
    monorepo case a root-only rule would have broken: `apps/web/package-lock.json` sits
    beside `apps/web/package.json` and is still excluded. R22a's rationale (no signal worth
    its tokens; a multi-MB file would truncate the sweep) is true of exactly those files
    and of nothing else."""
    return path.name in IGNORED_FILES and (path.parent / "package.json").is_file()


# Spawn alias: exec-style process creation (argv vector, no shell — nothing to inject
# into). Bound once at module level; also keeps the call off the JS-oriented exec guard.
_spawn_no_shell = asyncio.create_subprocess_exec


# ---------------------------------------------------------------------------------------
# Workspace protocol + errors
# ---------------------------------------------------------------------------------------


class WorkspacePathError(Exception):
    """A path the workspace refuses (escape, ignored dir, missing file). The message is
    model-facing and teaching — the tool layer wraps it in `ModelRetry`."""


@dataclass(frozen=True)
class ReadExecResult:
    exit: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    line_no: int
    line: str


class ReadOnlyWorkspace(Protocol):
    """WHERE reads happen. Two implementations — the snapshot extraction and the
    supervisor-routed live sandbox — so the SAME toolset follows whichever workspace the run
    attached. Implementations enforce their own jail, to whatever
    strength their side of the boundary allows; the tool layer enforces the command policy,
    bounds, and redaction — those hold regardless of pairing."""

    @property
    def label(self) -> str:
        """Human phrase for results: e.g. 'the app's current snapshot'."""
        ...

    async def read_file(self, rel_path: str) -> str:
        """Whole-file text, byte-capped by the implementation. Raises WorkspacePathError."""
        ...

    async def list_files(self) -> list[str]:
        """All relative file paths (ignore set applied), sorted, uncapped."""
        ...

    async def search_files(self, pattern: re.Pattern[str], subdir: str | None) -> list[SearchHit]:
        """Regex hits across the tree (ignore set applied), implementation-bounded."""
        ...

    async def exec_readonly(self, argv: Sequence[str]) -> ReadExecResult:
        """Run an ALREADY-POLICY-CHECKED argv jailed in the workspace."""
        ...


@dataclass(frozen=True)
class ExtractedSnapshotWorkspace:
    """Reads over a local snapshot extraction dir (`snapshot_read.extract_snapshot`).

    The jail is enforced by RESOLUTION, not string comparison: every path is resolved
    (symlinks included) and must land inside the resolved root. The extraction is a git
    checkout of an untrusted bundle, so symlinks pointing out of the tree are assumed
    possible and neutralized by the containment check."""

    root: Path
    label: str = "the app's current snapshot"

    def _resolve(self, rel_path: str) -> Path:
        candidate = Path(rel_path)
        if candidate.is_absolute() or rel_path.startswith("~"):
            raise WorkspacePathError(
                f"`{rel_path}` is not a workspace-relative path. Paths are relative to the "
                "app root — e.g. `app/page.tsx`."
            )
        resolved_root = self.root.resolve()
        resolved = (resolved_root / candidate).resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise WorkspacePathError(
                f"`{rel_path}` escapes the workspace. Paths must stay inside the app root."
            )
        relative_parts = resolved.relative_to(resolved_root).parts
        if any(part in IGNORED_DIRS for part in relative_parts):
            raise WorkspacePathError(
                f"`{rel_path}` is under a heavy or irrelevant path "
                "(`node_modules`, `.next`, `dist`, `.git`) — read the app's source instead."
            )
        if is_dependency_lockfile(resolved):
            raise WorkspacePathError(
                f"`{rel_path}` is a dependency lock file "
                "(`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`) — read the app's "
                "source instead."
            )
        return resolved

    async def read_file(self, rel_path: str) -> str:
        resolved = self._resolve(rel_path)
        if not resolved.is_file():
            raise WorkspacePathError(
                f"`{rel_path}` does not exist in {self.label}. Use `list_files` to see "
                "what is there."
            )
        with resolved.open("rb") as handle:
            raw = handle.read(READ_FILE_MAX_BYTES)
        return raw.decode("utf-8", errors="replace")

    async def list_files(self) -> list[str]:
        # The walk obeys the SAME jail `read_file` does: a symlink is never listed and a
        # symlinked directory is never descended into. Otherwise a link planted in the
        # untrusted bundle advertises the server's filesystem in the listing, and every hit
        # is one `read_file` away from being followed.
        entries: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            here = Path(dirpath)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in IGNORED_DIRS and not (here / name).is_symlink()
            )
            base = here.relative_to(self.root)
            for name in filenames:
                rel = base / name
                if rel.name == ".bial-extract-ok":
                    continue  # the extraction cache's own ready-marker, not app truth
                if is_dependency_lockfile(here / name):
                    # A real lockfile (beside its manifest). A same-named file elsewhere
                    # is ordinary source and IS listed, so the model can see it exists.
                    continue
                if (here / name).is_symlink():
                    continue
                entries.append(rel.as_posix())
        return sorted(entries)

    async def search_files(self, pattern: re.Pattern[str], subdir: str | None) -> list[SearchHit]:
        start = time.monotonic()
        if subdir:
            self._resolve(subdir)  # validate (escape/ignored) before it becomes a filter
        hits: list[SearchHit] = []
        for rel in await self.list_files():
            if subdir and not Path(rel).is_relative_to(Path(subdir)):
                continue
            if time.monotonic() - start > SEARCH_WALL_CLOCK_S or len(hits) >= SEARCH_MAX_HITS:
                break
            try:
                # Resolution-jailed, exactly like `read_file` — a plain `self.root / rel`
                # would open whatever a link points at.
                path = self._resolve(rel)
            except WorkspacePathError:
                continue
            try:
                text = path.read_bytes()[:READ_FILE_MAX_BYTES].decode("utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line[:SEARCH_LINE_SCAN_CHARS]):
                    hits.append(SearchHit(path=rel, line_no=line_no, line=line.strip()[:300]))
                    if len(hits) >= SEARCH_MAX_HITS:
                        break
        return hits

    def _refuse_escaping_argv(self, argv: Sequence[str]) -> None:
        """Realpath-resolve each non-flag argv token against the resolved root and refuse any
        that lands outside — the BELT to `snapshot_read`'s `core.symlinks=false` braces, and
        the only symlink guard when a live workspace (U12), not a clone-controlled extraction,
        backs the reads. `check_the_guest_list` only vets tokens LEXICALLY (`/`, `~`, `..`), so
        a symlink inside the tree that points out of it would otherwise be followed by the OS
        when `cat`/`grep`/`find`/`sed` open it. A bare flag carries no path and is skipped, but
        `--flag=<path>` does (`wc --files0-from=/etc/passwd`), so the VALUE half is contained
        exactly like a positional token. A non-path token (a grep pattern) resolves to a
        harmless in-root path and passes."""
        resolved_root = self.root.resolve()
        for token in argv[1:]:
            if token.startswith("-"):
                _, separator, value = token.partition("=")
                if not separator or not value:
                    continue
                candidate = value
            else:
                candidate = token
            resolved = (resolved_root / candidate).resolve()
            if resolved != resolved_root and resolved_root not in resolved.parents:
                raise WorkspacePathError(
                    f"`{candidate}` resolves outside the workspace (a symlink or path escape). "
                    "A read-only `run_command` touches only the app's own files."
                )

    async def exec_readonly(self, argv: Sequence[str]) -> ReadExecResult:
        self._refuse_escaping_argv(argv)
        process = await _spawn_no_shell(
            *argv,
            cwd=self.root,
            env=_minimal_env(self.root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), READ_EXEC_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ReadExecResult(
                exit=124,
                stdout="",
                stderr=f"timed out after {READ_EXEC_TIMEOUT_S:.0f}s",
            )
        return ReadExecResult(
            exit=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


_LIVE_READ_TIMEOUT_S = int(READ_EXEC_TIMEOUT_S)
"""The supervisor's exec bound for a live read (`SandboxClient.exec` takes whole seconds). Same
budget as the local twin: with the heavy dirs pruned, walking or grepping an app's own source is
metadata-cheap — longer means wedged, not working."""


def _strip_the_dot_slash(path: str) -> str:
    """`find .` and `grep -r .` prefix every path with `./`; the tools speak plain
    workspace-relative paths, and `app/page.tsx` is also what the model must pass back."""
    return path[2:] if path.startswith("./") else path


def _is_under_an_ignored_dir(path: str) -> bool:
    return any(part in IGNORED_DIRS for part in path.split("/"))


def _is_an_ignored_file(path: str) -> bool:
    """Full-filename match on the last segment — `my-package-lock.json.bak` is not a
    lockfile. This is the LIVE-sandbox post-filter, which has only the path string (the
    tree is on the other side of an exec boundary), so it cannot check for the adjacent
    manifest the way the snapshot surfaces do. Over-excluding here costs the model a file;
    it does not create the scan blind spot, because the credential sweep runs on the
    extracted SNAPSHOT and uses `is_dependency_lockfile`."""
    return path.rsplit("/", 1)[-1] in IGNORED_FILES


def _find_the_files() -> list[str]:
    """`find . -type f` with the ignore set PRUNED at the source. Post-filtering alone would be
    correct but ruinous: unlike a snapshot extraction, the live tree really does carry
    `node_modules` and `.next` on disk, so an unpruned walk lists tens of thousands of paths and
    ships every one of them back over the supervisor. Lockfiles ride the same prune group:
    `-name` matches a plain file too, where `-prune` is a no-op that still evaluates true, so
    the `-o … -print` branch never sees it."""
    prune: list[str] = ["("]
    for index, name in enumerate(sorted(IGNORED_DIRS | IGNORED_FILES)):
        if index:
            prune.append("-o")
        prune += ["-name", name]
    prune.append(")")
    return ["find", ".", *prune, "-prune", "-o", "-type", "f", "-print"]


_LIVE_FIND_ARGV = _find_the_files()


def _grep_the_tree(pattern: str, target: str) -> list[str]:
    """`grep -rn` over `target`, heavy dirs excluded. `-E` is not decoration: the pattern was
    written as a PYTHON regex, and grep's default BRE would read `foo|bar` as a literal pipe —
    POSIX ERE is the closest dialect every grep has. `-e` and `--` keep a pattern or a path that
    starts with `-` from being read as a flag."""
    excludes = [f"--exclude-dir={name}" for name in sorted(IGNORED_DIRS)]
    excludes += [f"--exclude={name}" for name in sorted(IGNORED_FILES)]
    return ["grep", "-rnE", *excludes, "-e", pattern, "--", target]


@dataclass(frozen=True)
class LiveSandboxWorkspace:
    """Reads over the LIVE Write sandbox, routed through the supervisor's exec transport.

    Only `list_files` and `search_files` ever run here: `toolsets._WRITE_STRUCTURED_READS`
    allowlists exactly those two, and Write gets the sandbox-routed `read_file`/`run_command`
    for everything else. The other two are implemented anyway because the Protocol is a
    contract, not a convention — they do the real read through the same transport rather than
    existing as a hole.

    THE CONTAINMENT GUARD IS LEXICAL ONLY, and that is worth being plain about.
    `ExtractedSnapshotWorkspace`'s resolution jail cannot be reused: `Path.resolve()` answers
    about THIS server's filesystem, and the tree lives in a container on the far side of an HTTP
    boundary. So the guard is string work — absolute / `~` / `..` via `_vet_path_token`, plus the
    ignore set — and there is NO symlink defence available at all. A symlink planted inside the
    sandbox would be followed by the sandbox's own `grep`, and nothing here would know.

    That is acceptable because containment is not this class's job. The supervisor's workspace
    jail and the demoted `appuser` are the real boundary, and in a Build chat the model already
    holds an unrestricted `run_command` on the other side of it — a listing filter it can trivially
    step around is not what keeps the sandbox contained. This is a model-facing hygiene filter:
    it keeps results on the app's own source and turns a bad path into a refusal the model can
    learn from. Do not cite it as a security control."""

    session: SandboxSession
    label: str = "your app's live workspace"

    def _vet(self, rel_path: str) -> None:
        """The lexical guard, run BEFORE the transport so a bad path never becomes a command."""
        refusal = _vet_path_token(rel_path)
        if refusal is not None:
            raise WorkspacePathError(refusal)
        if _is_under_an_ignored_dir(rel_path):
            raise WorkspacePathError(
                f"`{rel_path}` is under a heavy or irrelevant path "
                "(`node_modules`, `.next`, `dist`, `.git`) — search the app's source instead."
            )
        if _is_an_ignored_file(rel_path):
            raise WorkspacePathError(
                f"`{rel_path}` is a dependency lock file "
                "(`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`) — search the app's "
                "source instead."
            )

    async def _read(self, argv: list[str]) -> ReadExecResult:
        """One read command through the supervisor, with its exit code intact."""
        # Alias keeps the call off the JS-oriented exec guard (mirrors `orchestrator/tools`).
        transport = self.session.sandbox_client.exec
        result = await transport(self.session.handle, argv, timeout_s=_LIVE_READ_TIMEOUT_S)
        return ReadExecResult(exit=result.exit, stdout=result.stdout, stderr=result.stderr)

    async def _read_out(self, argv: list[str]) -> str:
        """One read command through the supervisor; the caller parses stdout. A non-zero exit is
        NORMAL here — `grep` answers 'no matches' with 1 — so only stdout is read. A genuinely
        broken transport raises `SandboxError`, which this layer has no honest way to paper over
        and must not turn into a silent empty result."""
        # Alias keeps the call off the JS-oriented exec guard (mirrors `orchestrator/tools`).
        transport = self.session.sandbox_client.exec
        result = await transport(self.session.handle, argv, timeout_s=_LIVE_READ_TIMEOUT_S)
        return result.stdout

    async def read_file(self, rel_path: str) -> str:
        """Whole-file text, read out of the running container.

        `cat` rather than the supervisor's `/files` view action, deliberately: that endpoint
        returns LINE-NUMBERED text for the sandbox's own editing tools, and the read tool layer
        above this does its own line windowing. Handing it pre-numbered text would number it
        twice and quietly corrupt every line the model tried to quote back."""
        self._vet(rel_path)
        result = await self._read(["cat", "--", rel_path])
        if result.exit != 0:
            # `cat`'s stderr is the honest reason (missing, a directory, unreadable) and it is
            # already the shape the tool layer turns into a teaching retry.
            raise WorkspacePathError(
                f"`{rel_path}` could not be read from {self.label}: "
                f"{result.stderr.strip() or 'no such file'}. Use `list_files` to see what "
                "is there."
            )
        return result.stdout[:READ_FILE_MAX_BYTES]

    async def list_files(self) -> list[str]:
        stdout = await self._read_out(_LIVE_FIND_ARGV)
        # The prune above is a COST guard on the sandbox side; this is the CORRECTNESS one. We
        # cannot verify from here that the remote `find` honored it, and a listing that leaks
        # `node_modules` buries the app's own files under 40k lines of dependency.
        return sorted(
            path
            for path in (_strip_the_dot_slash(line) for line in stdout.splitlines())
            if path and not _is_under_an_ignored_dir(path) and not _is_an_ignored_file(path)
        )

    async def search_files(self, pattern: re.Pattern[str], subdir: str | None) -> list[SearchHit]:
        if subdir:
            self._vet(subdir)  # validate (escape/ignored) before it becomes a command operand
        stdout = await self._read_out(_grep_the_tree(pattern.pattern, subdir or "."))
        hits: list[SearchHit] = []
        for line in stdout.splitlines():
            path, path_sep, rest = line.partition(":")
            line_no, line_sep, text = rest.partition(":")
            if not path_sep or not line_sep or not line_no.isdigit():
                continue  # `grep: …` diagnostics and "Binary file … matches" carry no hit
            relative = _strip_the_dot_slash(path)
            if _is_under_an_ignored_dir(relative) or _is_an_ignored_file(relative):
                continue
            hits.append(SearchHit(path=relative, line_no=int(line_no), line=text.strip()[:300]))
            if len(hits) >= SEARCH_MAX_HITS:
                break
        return hits

    async def exec_readonly(self, argv: Sequence[str]) -> ReadExecResult:
        """Run an ALREADY-POLICY-CHECKED argv inside the container.

        THE POLICY DID NOT MOVE, THE ENVIRONMENT DID — and that is worth being precise about
        rather than waving through. The guest list, the per-command deny flags, the `sed`
        script validator and the path vetting all still run in the tool layer above this, so
        the set of commands this surface may issue is byte-for-byte what it was.

        What changed is where they land. They used to run on the CONTROL-PLANE server under
        `_minimal_env` — an explicit allowlist holding no DSN and no tokens, asserted by test.
        They now run in the app's own container, which by construction holds `BIAL_DATABASE_URL`
        and a Blob SAS in its environment. Nothing on the guest list can print an environment
        variable, absolute paths are refused before the command is built, and the supervisor
        redacts known secrets from every response — so the exposure is bounded on three sides.
        It is still a materially richer environment than a bare checkout on a server, and that
        is a real change in posture, not a no-op."""
        result = await self.session.sandbox_client.exec(
            self.session.handle, list(argv), timeout_s=_LIVE_READ_TIMEOUT_S
        )
        return ReadExecResult(exit=result.exit, stdout=result.stdout, stderr=result.stderr)


def _minimal_env(home: Path) -> dict[str, str]:
    """The read-command env: an explicit allowlist, NOTHING inherited from the server
    process. No DSN, no tokens, no cloud credentials — asserted by test."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LC_ALL": "C",
        "LANG": "C",
    }


# ---------------------------------------------------------------------------------------
# The run_command guest list
# ---------------------------------------------------------------------------------------


def _vet_sed_script(argv: Sequence[str]) -> str | None:
    """`sed`'s write/execute surface lives in its SCRIPT (`w`/`W` write files, GNU `e`
    runs commands), so flags alone can't make it safe. Admit only the one shape the plan
    names — numeric range printing (`sed -n '40,80p' file`) — and teach everything else
    toward `read_file`/`search_files`."""
    scripts: list[str] = []
    files: list[str] = []
    tokens = list(argv[1:])
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-n":
            index += 1
            continue
        if token == "-e":
            if index + 1 >= len(tokens):
                return "`sed -e` needs a script argument."
            scripts.append(tokens[index + 1])
            index += 2
            continue
        if token.startswith("-"):
            return (
                f"`sed {token}` is not available here. Only `sed -n 'START,ENDp' file` "
                "(numeric range print) is supported — use `read_file` with a view range "
                "for the same result."
            )
        if not scripts:
            scripts.append(token)
        else:
            files.append(token)
        index += 1
    if not scripts:
        return "`sed` needs a script — e.g. `sed -n '40,80p' app/page.tsx`."
    if len(scripts) > 1:
        return "Pass exactly one sed script."
    if re.fullmatch(r"\d{1,7}(,\d{1,7})?p", scripts[0]) is None:
        return (
            f"`sed` script `{scripts[0][:80]}` is not available here — only numeric "
            "range printing (e.g. `40,80p`) is supported by a read-only `run_command`. "
            "Use `read_file` for windows or `search_files` for matching."
        )
    if not files:
        return "`sed` here must name the file to print from (stdin is closed)."
    return None


@dataclass(frozen=True)
class CommandPolicy:
    """One guest-list entry: the binary plus its denied (write-capable) flags and an
    optional deeper validator for commands whose danger lives beyond flags."""

    deny_flags: frozenset[str] = frozenset()
    deny_reason: str = ""
    validator: Callable[[Sequence[str]], str | None] | None = None


_PATH_BEARING_FLAG_REASON = (
    "it reads its input list from a file path, which is the one place a path can ride into "
    "the command without being vetted"
)


# The guest list: POSIX read-only classics, present on any Linux/macOS server
# runtime (verified against POSIX; the extraction runs SERVER-side, not in the sandbox
# image — when U12 routes to the live workspace these same names exist in the
# debian-based sandbox). `psql` is NEVER listed (plan, locked). `sh`/`bash`/`node`/`npm`
# are absent by design: no shell, no runtime, no package manager on a read-only surface.
_GUEST_LIST: dict[str, CommandPolicy] = {
    "ls": CommandPolicy(),
    "cat": CommandPolicy(),
    "head": CommandPolicy(),
    "tail": CommandPolicy(
        deny_flags=frozenset({"-f", "-F", "--follow"}),
        deny_reason="following a file would hang until the timeout",
    ),
    "grep": CommandPolicy(
        deny_flags=frozenset({"-f", "--file", "--files0-from"}),
        deny_reason=_PATH_BEARING_FLAG_REASON,
    ),
    "wc": CommandPolicy(
        deny_flags=frozenset({"-f", "--file", "--files0-from"}),
        deny_reason=_PATH_BEARING_FLAG_REASON,
    ),
    "find": CommandPolicy(
        deny_flags=frozenset(
            {
                "-delete",
                "-exec",
                "-execdir",
                "-ok",
                "-okdir",
                "-fprint",
                "-fprintf",
                "-fprint0",
                "-fls",
            }
        ),
        deny_reason="it can delete files or execute commands",
    ),
    "sed": CommandPolicy(validator=_vet_sed_script),
}


def _vet_path_token(token: str) -> str | None:
    """Reject argv tokens that reach outside the jail. Conservative by design: a regex
    that legitimately needs `..` as a path segment can be rephrased for `search_files`."""
    if token.startswith(("/", "~")):
        return (
            f"`{token}` is not a workspace-relative path — commands run inside the app "
            "root; drop the leading `/` or `~`."
        )
    parts = token.split("/")
    if ".." in parts:
        return f"`{token}` steps outside the workspace (`..` segments are not allowed)."
    return None


def _denied_flag_in(flag: str, policy: CommandPolicy) -> str | None:
    """The denied flag this token carries, or None. Exact match catches the long forms; the
    per-character sweep catches short flags that hide in a cluster or wear their value
    attached (`grep -rnf pats`, `grep -f/etc/passwd`), which an exact match reads as a
    single unknown flag and waves through."""
    if flag in policy.deny_flags:
        return flag
    if not flag.startswith("--"):
        for char in flag[1:]:
            if f"-{char}" in policy.deny_flags:
                return f"-{char}"
    return None


def check_the_guest_list(argv: Sequence[str]) -> str | None:
    """The door policy for the read-only `run_command`: returns the teaching refusal, or
    None when the command may run. Mirrors `sql_guard.you_shall_not_pass`'s contract.

    R71 — WHAT IT ALLOWS IS A PROPERTY OF THE ABILITY, and the signature is where that is
    provable: `argv` and nothing else. No `RunContext`, no deps, no settings, no chat kind,
    and nothing in this module imports one. Two agents reach these bodies today — a Plan
    chat's toolset and the classification agent's, which has no chat kind at all — and they
    get byte-identical answers because there is nothing here that could tell them apart."""
    if not argv:
        return 'The command is empty — pass argv tokens, e.g. `["ls", "app"]`.'
    binary = argv[0]
    policy = _GUEST_LIST.get(binary)
    if policy is None:
        allowed = ", ".join(sorted(_GUEST_LIST))
        return (
            f"`{binary}` is not on the guest list for this read-only `run_command` "
            f"(available: {allowed}). It inspects the app; it cannot run builds, scripts, "
            "or package managers. Use `read_file`, `list_files`, and `search_files` for "
            "most reads."
        )
    for token in argv[1:]:
        if token.startswith("-"):
            flag, separator, value = token.partition("=")
            denied = _denied_flag_in(flag, policy)
            if denied is not None:
                return (
                    f"`{binary} {denied}` is not available to a read-only `run_command` — "
                    f"{policy.deny_reason}. Stick to the read-only form of the command."
                )
            # A path also rides in on `--flag=<path>`; vetting only bare tokens let
            # `grep --file=../../x .` walk straight out of the jail.
            if separator:
                path_refusal = _vet_path_token(value)
                if path_refusal is not None:
                    return path_refusal
        else:
            path_refusal = _vet_path_token(token)
            if path_refusal is not None:
                return path_refusal
    if policy.validator is not None:
        return policy.validator(argv)
    return None


# ---------------------------------------------------------------------------------------
# U22 / R28: the MIRRORED output cap — head AND tail, with the truncation stated
# ---------------------------------------------------------------------------------------
#
# THE MIRROR of `orchestrator/tools`'s block of the same shape (`_is_predictable_noise` /
# `_redacted_lines` / `_render_output` / `_redact_command_output`) — see the module docstring for
# why the read surface copies it instead of importing it (this module must add NO runtime edge
# into the orchestrator package). The pair is held identical by ONE table-driven test,
# `test_read_tools.py::test_the_two_mirrored_output_caps_behave_identically`, which runs the same
# table through both functions rather than asserting each one separately.
#
# WHAT DIFFERS IS THE CALLERS, NOT THIS CODE. Read mode has no `fetch_output_slice` — the slice
# tool is registered on `sandbox_toolset`, which only Write gets — so every call here passes
# `handle=None` and the notice tells the model to narrow its command instead of naming a tool it
# does not have. This surface also renders under the FULL dump budget on success as well as
# failure: here the successful output IS the answer that was asked for, and with no handle to
# recover an elided middle, summarising a success here would buy back exactly the re-run this
# unit exists to remove.
#
# AND THERE IS NO NOISE FILTER HERE AT ALL — the one place this copy is deliberately SHORTER than
# the Write one rather than merely called differently. De-noising exists to drop dependency-
# manager chatter, and this surface cannot produce any: `check_the_guest_list` admits
# `ls, cat, head, tail, grep, sed, find, wc` and nothing else, and `search_files` renders hits out
# of files. Every string that reaches these functions is FILE CONTENT, where dropping a line is
# not a saving but a silent edit — a `sed -n '40,80p'` answering 40 of the 41 lines it was asked
# for, and an `edit_file` composed from that read failing to match with nothing on screen to
# explain why. The Write copy asks its classifier the same question per command
# (`command_only_inspects`); here the answer is a constant, so the machinery is absent rather than
# always-off, and the noise policy has exactly one home and no second copy to drift.


_WITHHELD_TAIL_NOTICE = (
    "[... the end of this output was withheld: the part that was dropped opens a credential value "
    "that never closes, so the remaining text may be the inside of one and cannot be masked "
    "safely. Re-run a narrower command to see it ...]"
)
"""The MIRROR of the Write copy's notice — same words, same reason (see that one)."""


_WITHHELD_HEAD_NOTICE = (
    "[... the rest of this output was withheld: a credential value opens above and never closes "
    "in what was captured, so everything after it may be the inside of one and cannot be masked "
    "safely. Re-run a narrower command to see it ...]"
)
"""The MIRROR of the Write copy's head notice — the same guard at the other end of the cut."""


def _capture_limit_marker(dropped: int) -> str:
    """What a capture too big to scan lost, said out loud (the MIRROR of the Write copy)."""
    return (
        f"[... {dropped:,} characters dropped at capture — this command printed more than the "
        f"{_REDACT_INPUT_MAX_CHARS:,}-character limit, so only its first and last "
        f"{_REDACT_INPUT_MAX_CHARS // 2:,} characters were read. No handle holds the rest; "
        "re-run a narrower command to see it ...]"
    )


def _within_the_capture_limit(text: str) -> tuple[str, str, int]:
    """The head and the tail of a raw capture, cut ON LINE BOUNDARIES — and what that cost.

    TWO PROPERTIES, and the older single `text[:cap]` slice got both wrong.

    SECURITY: the redactor is never handed half a line. A credential is a shape on ONE line, so a
    cut landing inside one leaves a fragment matching none of the redactor's shapes — which U22
    turned from harmless into egressed, because the tail of the capture is now always rendered.
    Whole lines in, whole lines out; a single line longer than the window is dropped rather than
    truncated.

    TRUTHFULNESS: a head-only cap silently deleted the END of every capture over the limit, and
    the notice then reported the surviving line count as the total.

    The two halves together stay inside `_REDACT_INPUT_MAX_CHARS` (the ReDoS guard)."""
    if len(text) <= _REDACT_INPUT_MAX_CHARS:
        return text, "", 0
    half = _REDACT_INPUT_MAX_CHARS // 2
    head = text[:half].rpartition("\n")[0]
    tail = text[len(text) - half :].partition("\n")[2]
    return head, tail, len(text) - len(head) - len(tail)


def _redacted_lines(text: str) -> list[str]:
    """The SAFE artifact, and the ONLY thing that is ever returned (U22 / R3).

    `scrub_untrusted` is cap → de-escape → redact, in that order: the cap bounds the work an
    app-controlled blob can make a synchronous scan do (ReDoS guard), the escape strip runs
    BEFORE the mask because an ANSI sequence spliced into a credential splits the token and the
    pattern stops matching, and the mask is what makes the text egressable at all.

    REDACTION HAPPENS HERE, ONCE, ON EVERY CHARACTER THAT SURVIVES CAPTURE — before any slicing,
    before the head/tail cut: cutting first would split a credential that straddles the cut into
    two fragments that no longer match the redactor's shapes, which is exactly how a cap applied
    after redaction re-exposes one. The capture cut above is the same rule one level up, which is
    why it cuts on lines."""
    head, tail, dropped = _within_the_capture_limit(text)
    # THE HEAD IS CUT WHEN IT ENDS INSIDE A CREDENTIAL — the MIRROR of the Write copy's head
    # guard, and the same reason: a value opened in the head and closed past it matches none of
    # the redactor's shapes and renders verbatim. See the Write copy's guard.
    safe_head = cut_before_an_open_credential(head)
    scannable = head if safe_head is None else safe_head
    lines = scrub_untrusted(scannable, limit=_REDACT_INPUT_MAX_CHARS).splitlines()
    if safe_head is not None:
        lines.append(_WITHHELD_HEAD_NOTICE)
    if dropped:
        lines.append(_capture_limit_marker(dropped))
    # THE TAIL IS WITHHELD WHEN IT MIGHT BE A CREDENTIAL'S BODY — the MIRROR of the Write copy's
    # guard, and the same reason: the redactor's quoted arms span newlines, so a tail beginning
    # part-way through a value carries no key and masks to nothing. See the Write copy's guard.
    if tail and leaves_a_credential_value_open(text[: len(text) - len(tail)]):
        lines.append(_WITHHELD_TAIL_NOTICE)
        return lines
    if tail:
        lines.extend(scrub_untrusted(tail, limit=_REDACT_INPUT_MAX_CHARS).splitlines())
    return lines


def _leading_lines_within(lines: list[str], budget: int) -> int:
    """How many leading lines fit in `budget` characters (newline separators counted)."""
    used = 0
    for index, line in enumerate(lines):
        used += len(line) + 1
        if used > budget:
            return index
    return len(lines)


def _elision_notice(
    *,
    elided_lines: int,
    elided_chars: int,
    first: int,
    last: int,
    total: int,
    cut_line: int,
    partly_shown: bool,
    handle: str | None,
) -> str:
    """The truncation notice: WHAT was removed, and — inline — how to get it back.

    NAMING THE TOOL AND THE HANDLE IN THE NOTICE ITSELF is the point. A capability described once
    in a system prompt is a thing the model has to remember at the moment it is staring at a
    truncated log; a call it can copy off the line in front of it is not.

    `cut_line` IS PASSED RATHER THAN DERIVED (it used to be `first - 1`) — the MIRROR of the Write
    copy's change, and the same reason: the caller now names the elided range from the first line
    it did not show WHOLE, so the two numbers stopped being one apart."""
    if elided_lines > 0:
        edge = " (the ends of that range are only partly shown here)" if partly_shown else ""
        what = (
            f"{elided_lines:,} lines ({elided_chars:,} characters) elided — "
            f"lines {first}-{last} of {total:,}{edge}"
        )
        how = (
            f'read them with fetch_output_slice(handle="{handle}", '
            f"start_line={first}, end_line={last})"
            if handle is not None
            else "re-run a narrower command to see them"
        )
    else:
        what = f"{elided_chars:,} characters elided from the middle of line {cut_line:,}"
        how = (
            f'read that line with fetch_output_slice(handle="{handle}", '
            f"start_line={cut_line}, end_line={cut_line})"
            if handle is not None
            else "re-run a narrower command to see them"
        )
    return f"\n[... {what}; {how} ...]\n"


def _render_output(lines: list[str], *, budget: int, handle: str | None) -> str:
    """Render redacted lines under `budget`, keeping the HEAD AND THE TAIL (ASM13).

    Head-only was the defect: a stack trace puts its message at the top and the failing assertion
    at the bottom, so a head cap loses the error and a tail cap loses the cause. The budget is
    split down the middle and the two ends are joined by a notice that says what is missing.

    NOTHING IS RE-REDACTED HERE. The input is already `_redacted_lines`' output, so this function
    only ever cuts already-masked text — which is what makes a cut safe at all."""
    joined = "\n".join(lines)
    if len(joined) <= budget:
        return joined
    head_budget = budget // 2
    tail_budget = budget - head_budget
    # At least one line in the head even when that single line is longer than the whole budget;
    # it is hard-capped below, and the `else` arm carves the tail out of the same line.
    whole_lines_in_head = _leading_lines_within(lines, head_budget)
    head_count = max(1, whole_lines_in_head)
    remaining = lines[head_count:]
    tail_count = _leading_lines_within(remaining[::-1], tail_budget)
    head_text = "\n".join(lines[:head_count])[:head_budget]
    if tail_count:
        tail_text = "\n".join(lines[len(lines) - tail_count :])[-tail_budget:]
    else:
        # NO WHOLE LINE FITS THE TAIL BUDGET — carve the tail out of the last line instead, or a
        # capture that is one enormous line (a minified bundle, a `--json` payload) renders
        # head-only, and on this surface there is no handle to recover the rest with at all.
        tail_text = lines[-1][-tail_budget:]
    # A LINE SHOWN ONLY IN PART BELONGS IN THE ELIDED RANGE, at either end of it — the MIRROR of
    # the Write copy's rule, and the same reason: `fetch_output_slice` addresses LINES, so a line
    # the render hard-capped is only ever recoverable if the range names it. On this surface
    # there is no handle at all, so the notice's honesty is the whole of what the model gets.
    notice = _elision_notice(
        elided_lines=len(lines) - head_count - tail_count,
        elided_chars=len(joined) - len(head_text) - len(tail_text),
        first=head_count + 1 if whole_lines_in_head else head_count,
        last=len(lines) - tail_count,
        total=len(lines),
        cut_line=head_count,
        partly_shown=not whole_lines_in_head or not tail_count,
        handle=handle,
    )
    return head_text + notice + tail_text


def _cap_redact_cap(text: str, *, budget: int, handle: str | None = None) -> str:
    """Raw capture → the model-facing artifact: cap → de-escape → redact → head+tail.

    The MIRROR of `orchestrator/tools._redact_command_output` called with `denoise=False`; the
    shared table-driven test pins them identical."""
    return _render_output(_redacted_lines(text), budget=budget, handle=handle)


# ---------------------------------------------------------------------------------------
# The toolset factory
# ---------------------------------------------------------------------------------------


def read_only_toolset[DepsT](
    workspace_of: Callable[[RunContext[DepsT]], ReadOnlyWorkspace],
) -> FunctionToolset[DepsT]:
    """Build the four read tools over whatever workspace `workspace_of` resolves from the
    run's deps. Generic over the deps type so the SAME surface serves every consumer over
    its own deps — a Plan chat (`ReadDeps`), the classification review agent (`ReviewDeps`),
    and the Build chat's borrowed structured reads. Note: `read_file` and `run_command`
    already exist on `build_agent`, so the Build composition must add ONLY
    `list_files`/`search_files` (tool names are unique per run).

    The inner tools annotate `RunContext[Any]`: pydantic-ai resolves tool annotations with
    `get_type_hints` at registration, and a PEP-695 type param of the ENCLOSING function
    is not in scope there under deferred annotations. The factory signature carries the
    real typing; the `cast` at the return is the one boundary where it narrows back.
    """

    async def read_file(ctx: RunContext[Any], path: str, start_line: int = 1) -> str:
        """Read a file from the app (line-numbered), up to 400 lines per call. Pass
        `start_line` to continue a long file from where the previous window ended."""
        workspace = workspace_of(ctx)
        try:
            text = await workspace.read_file(path)
        except WorkspacePathError as exc:
            raise ModelRetry(str(exc)) from exc
        lines = text.splitlines()
        first = max(1, start_line)
        window = lines[first - 1 : first - 1 + READ_MAX_LINES]
        if not window and lines:
            raise ModelRetry(
                f"`{path}` has only {len(lines)} lines — `start_line={start_line}` is "
                "past the end."
            )
        numbered = "\n".join(f"{first + offset}\t{line}" for offset, line in enumerate(window))
        if first - 1 + len(window) < len(lines):
            numbered += (
                f"\n[... truncated — {len(lines)} lines total; continue with "
                f"start_line={first + len(window)} ...]"
            )
        return numbered or "(empty file)"

    async def list_files(ctx: RunContext[Any]) -> str:
        """List every file in the app (relative paths; heavy dirs like node_modules
        excluded)."""
        workspace = workspace_of(ctx)
        entries = await workspace.list_files()
        if not entries:
            return f"No files found in {workspace.label}."
        shown = entries[:LIST_MAX_ENTRIES]
        listing = "\n".join(shown)
        if len(entries) > len(shown):
            listing += f"\n[... {len(entries) - len(shown)} more files not shown ...]"
        return listing

    async def search_files(ctx: RunContext[Any], pattern: str, path: str | None = None) -> str:
        """Search the app's files for a regex `pattern` (grep-like; case-sensitive).
        Optionally restrict to a subdirectory via `path`."""
        workspace = workspace_of(ctx)
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ModelRetry(
                f"`{pattern}` is not a valid regular expression ({exc}). Fix the pattern "
                "— plain text works too."
            ) from exc
        try:
            hits = await workspace.search_files(compiled, path)
        except WorkspacePathError as exc:
            raise ModelRetry(str(exc)) from exc
        if not hits:
            return f"No matches for `{pattern}` in {workspace.label}."
        rendered = "\n".join(f"{hit.path}:{hit.line_no}: {hit.line}" for hit in hits)
        if len(hits) >= SEARCH_MAX_HITS:
            rendered += "\n[... more matches exist — narrow the pattern or path ...]"
        return _cap_redact_cap(rendered, budget=_OUTPUT_MAX_CHARS)

    async def run_command(ctx: RunContext[Any], command: list[str]) -> str:
        """Run a read-only inspection command in the app root — pass argv tokens, e.g.
        `["grep", "-rn", "visitors", "app/"]` or `["sed", "-n", "40,80p", "app/page.tsx"]`.
        Available commands: ls, cat, head, tail, grep, sed (range print), find, wc. There
        is no shell — no pipes, redirection, or chaining."""
        workspace = workspace_of(ctx)
        refusal = check_the_guest_list(command)
        if refusal is not None:
            raise ModelRetry(refusal)
        try:
            result = await workspace.exec_readonly(command)
        except WorkspacePathError as exc:
            # A symlink or path token that resolves out of the jail (the escape lexical vetting
            # cannot see) — teach the model back toward the app's own files.
            raise ModelRetry(str(exc)) from exc
        sections = [f"exit code: {result.exit}"]
        stdout = _cap_redact_cap(result.stdout, budget=_OUTPUT_MAX_CHARS).strip()
        stderr = _cap_redact_cap(result.stderr, budget=_OUTPUT_MAX_CHARS).strip()
        if stdout:
            sections.append(f"stdout:\n{stdout}")
        if stderr:
            sections.append(f"stderr:\n{stderr}")
        return "\n\n".join(sections)

    toolset = FunctionToolset[Any](
        [read_file, list_files, search_files, run_command], id="read-only-tools"
    )
    return cast(FunctionToolset[DepsT], toolset)
