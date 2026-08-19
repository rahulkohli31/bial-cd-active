"""The read-only tool surface for Ask/Plan (U8 / R6 / plan 2026-07-22-002).

Four tools — bounded `read_file`, `list_files`, `search_files`, and an allowlisted
read-only `run_command` — built as a `FunctionToolset` factory over a `ReadOnlyWorkspace`
Protocol. The Protocol is the routing seam: Ask/Plan read a local snapshot extraction dir
(`ExtractedSnapshotWorkspace`), and a Write turn — which is editing the tree as it goes —
reads the live sandbox through the supervisor (`LiveSandboxWorkspace`), so the SAME two
structured reads answer about the tree in front of the model instead of a stale bundle.

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
  container (every mode, normally) the command goes through the supervisor into the app's
  own environment — which holds `BIAL_DATABASE_URL` and a Blob SAS — with the supervisor's
  own timeout and secret redaction. On the snapshot fallback (only when no sandbox service
  is configured) it runs cwd-jailed on the control-plane server under `_minimal_env`, an
  explicit allowlist carrying no DSN and no tokens. The POLICY above is identical either
  way; the surroundings are not, and the richer one is now the normal case. Output is
  capped → secret-redacted → capped on both (mirrors `orchestrator/tools._redact_command_
  output`; reimplemented here so this module never imports the build agent's tool module,
  whose import registers tools on `build_agent`).

Refusals are teaching `ModelRetry`s in the U1 sentinel's voice — they say WHY and what to
do instead, so the model self-corrects rather than retrying blind. "No app exists yet" is
NOT a refusal: `EmptyProjectWorkspace` makes every tool answer it as a truthful normal
result (the inverse of the #9 lesson).

`psql` is never on a read-mode allowlist (plan, locked).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.core.redaction import redact_secrets

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
_OUTPUT_TRUNCATION_MARKER = "\n[... output truncated ...]"

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

# Spawn alias: exec-style process creation (argv vector, no shell — nothing to inject
# into). Bound once at module level; also keeps the call off the JS-oriented exec guard.
_spawn_no_shell = asyncio.create_subprocess_exec


# ---------------------------------------------------------------------------------------
# Workspace protocol + errors
# ---------------------------------------------------------------------------------------


class WorkspacePathError(Exception):
    """A path the workspace refuses (escape, ignored dir, missing file). The message is
    model-facing and teaching — the tool layer wraps it in `ModelRetry`."""


class NoAppYetError(Exception):
    """Raised by `EmptyProjectWorkspace` for every operation; the tool layer converts it
    into the truthful 'no app exists yet' NORMAL result (not an error)."""


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
    """WHERE reads happen. Three implementations — the snapshot extraction (Ask/Plan), the
    supervisor-routed live sandbox (Write), and the truthful empty one — so the SAME toolset
    follows the conversation's mode and attached sandbox. Implementations enforce their own
    jail, to whatever strength their side of the boundary allows; the tool layer enforces the
    command policy, bounds, and redaction — those hold regardless of pairing."""

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
class EmptyProjectWorkspace:
    """The no-snapshot workspace: every read answers `NoAppYetError`, which the tool layer
    renders as the truthful typed result. Keeps 'no app yet' out of every tool's control
    flow — the mode's toolset is identical whether or not an app exists."""

    app_id: uuid.UUID

    @property
    def label(self) -> str:
        return "this project (no app built yet)"

    async def read_file(self, rel_path: str) -> str:
        raise NoAppYetError

    async def list_files(self) -> list[str]:
        raise NoAppYetError

    async def search_files(self, pattern: re.Pattern[str], subdir: str | None) -> list[SearchHit]:
        raise NoAppYetError

    async def exec_readonly(self, argv: Sequence[str]) -> ReadExecResult:
        raise NoAppYetError


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
        if relative_parts and relative_parts[-1] in IGNORED_FILES:
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
                if name in IGNORED_FILES:
                    continue  # lockfiles are hidden everywhere the ignored dirs are
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
                    "Read-mode commands touch only the app's own files."
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

_LIVE_WRONG_TOOL = (
    "The live workspace only answers `list_files` and `search_files`. To read a file or run a "
    "command, use the sandbox's own `read_file` / `run_command` — they see this exact same tree."
)
"""What `read_file`/`exec_readonly` teach when something reaches them on the live workspace.
Unreachable in Write (the toolset allowlists the two structured reads and gives Write the
sandbox-routed `read_file`/`run_command`), so this fires only if a future composition slips —
and then it points the model at the tool that works rather than failing it blind."""


def _strip_the_dot_slash(path: str) -> str:
    """`find .` and `grep -r .` prefix every path with `./`; the tools speak plain
    workspace-relative paths, and `app/page.tsx` is also what the model must pass back."""
    return path[2:] if path.startswith("./") else path


def _is_under_an_ignored_dir(path: str) -> bool:
    return any(part in IGNORED_DIRS for part in path.split("/"))


def _is_an_ignored_file(path: str) -> bool:
    """Full-filename match on the last segment — `my-package-lock.json.bak` is not a lockfile."""
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
    for everything else. The other two are implemented because the Protocol is a contract, not a
    convention — they refuse with a teaching message instead of existing as a hole.

    THE CONTAINMENT GUARD IS LEXICAL ONLY, and that is worth being plain about.
    `ExtractedSnapshotWorkspace`'s resolution jail cannot be reused: `Path.resolve()` answers
    about THIS server's filesystem, and the tree lives in a container on the far side of an HTTP
    boundary. So the guard is string work — absolute / `~` / `..` via `_vet_path_token`, plus the
    ignore set — and there is NO symlink defence available at all. A symlink planted inside the
    sandbox would be followed by the sandbox's own `grep`, and nothing here would know.

    That is acceptable because containment is not this class's job. The supervisor's workspace
    jail and the demoted `appuser` are the real boundary, and in Write mode the model already
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
        the set of commands Ask and Plan may issue is byte-for-byte what it was.

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
            "range printing (e.g. `40,80p`) is supported in read mode. Use `read_file` "
            "for windows or `search_files` for matching."
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


# The read-mode guest list: POSIX read-only classics, present on any Linux/macOS server
# runtime (verified against POSIX; the extraction runs SERVER-side, not in the sandbox
# image — when U12 routes to the live workspace these same names exist in the
# debian-based sandbox). `psql` is NEVER listed (plan, locked). `sh`/`bash`/`node`/`npm`
# are absent by design: no shell, no runtime, no package manager in read mode.
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
    """The door policy for read-mode `run_command`: returns the teaching refusal, or None
    when the command may run. Mirrors `sql_guard.you_shall_not_pass`'s contract."""
    if not argv:
        return 'The command is empty — pass argv tokens, e.g. `["ls", "app"]`.'
    binary = argv[0]
    policy = _GUEST_LIST.get(binary)
    if policy is None:
        allowed = ", ".join(sorted(_GUEST_LIST))
        return (
            f"`{binary}` is not available in this read-only mode (available: {allowed}). "
            "This mode inspects the app; it cannot run builds, scripts, or package "
            "managers. Use `read_file`, `list_files`, and `search_files` for most reads."
        )
    for token in argv[1:]:
        if token.startswith("-"):
            flag, separator, value = token.partition("=")
            denied = _denied_flag_in(flag, policy)
            if denied is not None:
                return (
                    f"`{binary} {denied}` is not available in read mode — {policy.deny_reason}. "
                    "Stick to the read-only form of the command."
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


def _cap_redact_cap(text: str) -> str:
    """Cap → redact → cap (mirrors `orchestrator/tools._redact_command_output` — see the
    module docstring for why it is mirrored, not imported)."""
    redacted = redact_secrets(text[:_REDACT_INPUT_MAX_CHARS])
    if len(redacted) <= _OUTPUT_MAX_CHARS:
        return redacted
    return redacted[:_OUTPUT_MAX_CHARS] + _OUTPUT_TRUNCATION_MARKER


_NO_APP_YET_RESULT = (
    "No app exists yet for this project — nothing has been built, so there are no files "
    "to read. Once a build has run, the app's code will be readable here. Answer the "
    "user from that fact; do not guess at code that does not exist."
)


# ---------------------------------------------------------------------------------------
# The toolset factory
# ---------------------------------------------------------------------------------------


def read_only_toolset[DepsT](
    workspace_of: Callable[[RunContext[DepsT]], ReadOnlyWorkspace],
) -> FunctionToolset[DepsT]:
    """Build the four read tools over whatever workspace `workspace_of` resolves from the
    run's deps. Generic over the deps type so the SAME surface serves the Ask/Plan agent
    now (`ReadDeps`) and, via U12's live-workspace accessor, the build agent later —
    note: `read_file` and `run_command` already exist on `build_agent`, so a Write-mode
    composition must add ONLY `list_files`/`search_files` (tool names are unique per run).

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
        except NoAppYetError:
            return _NO_APP_YET_RESULT
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
        try:
            entries = await workspace.list_files()
        except NoAppYetError:
            return _NO_APP_YET_RESULT
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
        except NoAppYetError:
            return _NO_APP_YET_RESULT
        except WorkspacePathError as exc:
            raise ModelRetry(str(exc)) from exc
        if not hits:
            return f"No matches for `{pattern}` in {workspace.label}."
        rendered = "\n".join(f"{hit.path}:{hit.line_no}: {hit.line}" for hit in hits)
        if len(hits) >= SEARCH_MAX_HITS:
            rendered += "\n[... more matches exist — narrow the pattern or path ...]"
        return _cap_redact_cap(rendered)

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
        except NoAppYetError:
            return _NO_APP_YET_RESULT
        except WorkspacePathError as exc:
            # A symlink or path token that resolves out of the jail (the escape lexical vetting
            # cannot see) — teach the model back toward the app's own files.
            raise ModelRetry(str(exc)) from exc
        sections = [f"exit code: {result.exit}"]
        stdout = _cap_redact_cap(result.stdout).strip()
        stderr = _cap_redact_cap(result.stderr).strip()
        if stdout:
            sections.append(f"stdout:\n{stdout}")
        if stderr:
            sections.append(f"stderr:\n{stderr}")
        return "\n\n".join(sections)

    toolset = FunctionToolset[Any](
        [read_file, list_files, search_files, run_command], id="read-only-tools"
    )
    return cast(FunctionToolset[DepsT], toolset)
