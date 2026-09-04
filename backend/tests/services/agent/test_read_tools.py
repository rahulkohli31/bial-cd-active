"""U8 — the jailed read-only tool surface (workspace, guest-list policy, tools in-run).

Three layers, each tested where it is enforced (testing.md: test the DECISION at the site
that makes it): the `ExtractedSnapshotWorkspace` jail (path resolution, symlink
containment, byte caps, scrubbed subprocess env), the `check_the_guest_list` argv policy
(allowlist, deny flags, sed script vetting, path-token vetting), and the tool layer driven
through a REAL pydantic-ai run (FunctionModel — refusals as ModelRetry, no-app-yet as a
truthful NORMAL result, redacted command output).
"""

from __future__ import annotations

import inspect
import re
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.core.redaction import (
    CREDENTIAL_OPEN_SCAN_MAX_CHARS,
    leaves_a_credential_value_open,
)
from src.db.models.conversation import ChatKind
from src.services.agent import read_tools
from src.services.agent.read_tools import (
    ExtractedSnapshotWorkspace,
    WorkspacePathError,
    check_the_guest_list,
    read_only_toolset,
)
from src.services.agent.toolsets import ReadDeps, toolsets_for_kind, workspace_from_read_deps
from src.services.classification.agent import ReviewDeps
from src.services.orchestrator.constants import (
    REDACT_INPUT_MAX_CHARS,
    RUN_COMMAND_OUTPUT_MAX_CHARS,
)
from src.services.orchestrator.tools import _redact_command_output
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_SECRET_DSN = "postgresql://appuser:sup3rs3cretpw@db.example/appdb"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "app").mkdir(parents=True)
    (root / "app" / "page.tsx").write_text(
        "export default function VisitorLog() {\n  return <main>visitors</main>\n}\n"
    )
    (root / "package.json").write_text('{"name": "visitor-log"}\n')
    (root / "node_modules" / "react").mkdir(parents=True)
    (root / "node_modules" / "react" / "index.js").write_text("module.exports = {}\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "secrets.env").write_text(f"DATABASE_URL={_SECRET_DSN}\n")
    return root


@pytest.fixture
def workspace(tree: Path) -> ExtractedSnapshotWorkspace:
    return ExtractedSnapshotWorkspace(root=tree)


# --- workspace jail ----------------------------------------------------------


async def test_read_file_returns_content(workspace: ExtractedSnapshotWorkspace) -> None:
    text = await workspace.read_file("app/page.tsx")
    assert "VisitorLog" in text


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "~/x", "app/../../etc/passwd"])
async def test_escaping_paths_are_refused(
    workspace: ExtractedSnapshotWorkspace, path: str
) -> None:
    with pytest.raises(WorkspacePathError):
        await workspace.read_file(path)


async def test_symlink_out_of_the_tree_is_refused(
    tree: Path, tmp_path: Path, workspace: ExtractedSnapshotWorkspace
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("loot")
    (tree / "app" / "sneaky.txt").symlink_to(outside)
    with pytest.raises(WorkspacePathError):
        await workspace.read_file("app/sneaky.txt")


async def test_list_and_search_do_not_follow_symlinks_out_of_the_tree(
    tree: Path, tmp_path: Path, workspace: ExtractedSnapshotWorkspace
) -> None:
    # `read_file` was jailed by resolution but the WALK was not: a link planted in the
    # untrusted bundle showed up in `list_files` and had its target's contents grepped.
    outside = tmp_path / "outside.txt"
    outside.write_text("loot visitors\n")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "loot.txt").write_text("more loot visitors\n")
    (tree / "app" / "sneaky.txt").symlink_to(outside)
    (tree / "linked").symlink_to(outside_dir, target_is_directory=True)

    listing = await workspace.list_files()
    assert "app/sneaky.txt" not in listing
    assert not any(entry.startswith("linked/") for entry in listing)
    assert "app/page.tsx" in listing  # ordinary files are untouched

    hits = await workspace.search_files(re.compile("loot"), None)
    assert hits == []


async def test_exec_readonly_refuses_a_symlink_escape(
    tree: Path, tmp_path: Path, workspace: ExtractedSnapshotWorkspace
) -> None:
    # Layer 2 of the P0 jail-escape fix: even a live-workspace source (no `core.symlinks=false`
    # clone) must not let `cat`/`grep`/`find`/`sed` follow a symlink out of the tree — argv path
    # tokens are realpath-contained, unlike the LEXICAL-only guest-list vetting.
    outside = tmp_path / "outside.txt"
    outside.write_text("loot")
    (tree / "app" / "sneaky.txt").symlink_to(outside)
    with pytest.raises(WorkspacePathError):
        await workspace.exec_readonly(["cat", "app/sneaky.txt"])
    # A plain in-root read is untouched by the containment check.
    ok = await workspace.exec_readonly(["cat", "package.json"])
    assert ok.exit == 0 and "visitor-log" in ok.stdout


async def test_ignored_dirs_are_refused_and_hidden(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    with pytest.raises(WorkspacePathError):
        await workspace.read_file("node_modules/react/index.js")
    listing = await workspace.list_files()
    assert "app/page.tsx" in listing
    assert not any(entry.startswith(("node_modules/", ".git/")) for entry in listing)


async def test_read_file_is_byte_capped(
    tree: Path, workspace: ExtractedSnapshotWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_tools, "READ_FILE_MAX_BYTES", 64)
    (tree / "big.txt").write_text("x" * 10_000)
    assert len(await workspace.read_file("big.txt")) == 64


async def test_search_finds_lines_and_respects_subdir(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    hits = await workspace.search_files(re.compile("visitors"), None)
    assert [(hit.path, hit.line_no) for hit in hits] == [("app/page.tsx", 2)]
    assert await workspace.search_files(re.compile("visitors"), "app") != []
    with pytest.raises(WorkspacePathError):
        await workspace.search_files(re.compile("visitors"), "../elsewhere")


async def test_exec_runs_jailed_with_a_scrubbed_env(
    workspace: ExtractedSnapshotWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plant secrets in the SERVER env; the child env is an explicit allowlist, so neither
    # may cross. Capture the actual env passed to the spawn (patch-and-delegate — the
    # subprocess still really runs).
    monkeypatch.setenv("DATABASE_URL", _SECRET_DSN)
    monkeypatch.setenv("AZURE_STORAGE_KEY", "topsecret")
    captured: dict[str, Any] = {}
    real_spawn = read_tools._spawn_no_shell

    async def capturing_spawn(*args: Any, **kwargs: Any) -> Any:
        captured["env"] = kwargs["env"]
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(read_tools, "_spawn_no_shell", capturing_spawn)
    result = await workspace.exec_readonly(["ls", "app"])

    assert result.exit == 0
    assert "page.tsx" in result.stdout
    child_env = captured["env"]
    assert "DATABASE_URL" not in child_env
    assert "AZURE_STORAGE_KEY" not in child_env
    assert set(child_env) == {"PATH", "HOME", "LC_ALL", "LANG"}


async def test_exec_timeout_returns_a_timeout_result(
    workspace: ExtractedSnapshotWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_tools, "READ_EXEC_TIMEOUT_S", 0.2)
    result = await workspace.exec_readonly(["sleep", "5"])
    assert result.exit == 124
    assert "timed out" in result.stderr


# --- dependency lock files (U1 / R22a) ----------------------------------------


@pytest.fixture
def locked_tree(tree: Path) -> Path:
    """The base tree plus REAL lockfiles (beside the manifest each one locks), a monorepo
    lockfile one level down, a near-miss name, and a file merely NAMED like a lockfile in a
    directory with no manifest. Every one carries the `visitors` needle so a search hit
    from inside any of them is detectable."""
    (tree / "package-lock.json").write_text('{"lockfileVersion": 3, "note": "visitors"}\n')
    (tree / "yarn.lock").write_text('# yarn lockfile v1\n"visitors": {}\n')
    # A monorepo package: a genuine lockfile that is NOT at the root. Excluded because it
    # sits beside its own manifest — the case a root-only rule would have wrongly included.
    (tree / "packages").mkdir(exist_ok=True)
    (tree / "packages" / "ui").mkdir(exist_ok=True)
    (tree / "packages" / "ui" / "package.json").write_text('{"name": "ui"}\n')
    (tree / "packages" / "ui" / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n# visitors\n"
    )
    # NOT a lockfile: the name, with no manifest beside it. Ordinary source, and the file
    # a credential would otherwise have been parked in to skip the whole gate.
    (tree / "app" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n# visitors\n")
    (tree / "my-package-lock.json.bak").write_text("visitors backup, not a lockfile\n")
    return tree


@pytest.fixture
def locked_workspace(locked_tree: Path) -> ExtractedSnapshotWorkspace:
    return ExtractedSnapshotWorkspace(root=locked_tree)


async def test_listing_omits_lockfiles_and_keeps_the_apps_own_files(
    locked_workspace: ExtractedSnapshotWorkspace,
) -> None:
    listing = await locked_workspace.list_files()
    assert "app/page.tsx" in listing
    assert "package.json" in listing
    assert "package-lock.json" not in listing
    assert "yarn.lock" not in listing
    # A monorepo package's own lockfile is still excluded — depth was never the rule.
    assert "packages/ui/pnpm-lock.yaml" not in listing
    # ...but a file merely NAMED like one, with no manifest beside it, is ordinary source
    # and stays visible. Hiding it bought no tokens and cost the credential sweep its
    # only deterministic look at that file.
    assert "app/pnpm-lock.yaml" in listing


@pytest.mark.parametrize("path", ["package-lock.json", "yarn.lock", "packages/ui/pnpm-lock.yaml"])
async def test_reading_a_lockfile_is_refused_in_the_ignored_dir_shape(
    locked_workspace: ExtractedSnapshotWorkspace, path: str
) -> None:
    # Same refusal shape as the directory case: a WorkspacePathError naming the excluded set
    # and steering back to the app's source — so the model gets the same teaching either way.
    with pytest.raises(WorkspacePathError) as dir_refusal:
        await locked_workspace.read_file("node_modules/react/index.js")
    with pytest.raises(WorkspacePathError) as lock_refusal:
        await locked_workspace.read_file(path)
    steer = "read the app's source instead."
    assert steer in str(dir_refusal.value)
    assert steer in str(lock_refusal.value)


async def test_a_name_that_merely_contains_a_lockfile_name_stays_readable(
    locked_workspace: ExtractedSnapshotWorkspace,
) -> None:
    # Full-filename match, not substring: the backup is not a lockfile.
    assert "my-package-lock.json.bak" in await locked_workspace.list_files()
    assert "backup" in await locked_workspace.read_file("my-package-lock.json.bak")


async def test_search_returns_no_hits_from_inside_a_lockfile(
    locked_workspace: ExtractedSnapshotWorkspace,
) -> None:
    # `visitors` lives in every lockfile, the near-miss backup, the manifest-less
    # lookalike, and app/page.tsx — only the genuinely readable files may answer. The
    # lookalike answers precisely BECAUSE it is not a lockfile: a credential parked there
    # is now reachable by search, by the model, and by the credential sweep.
    hits = await locked_workspace.search_files(re.compile("visitors"), None)
    assert sorted((hit.path, hit.line_no) for hit in hits) == [
        ("app/page.tsx", 2),
        ("app/pnpm-lock.yaml", 2),
        ("my-package-lock.json.bak", 1),
    ]


def test_live_find_and_grep_exclude_lockfiles_at_the_source() -> None:
    # The other two application sites: the find prune and the grep exclusion the live
    # workspace ships to the sandbox.
    for name in sorted(read_tools.IGNORED_FILES):
        assert name in read_tools._LIVE_FIND_ARGV
        assert f"--exclude={name}" in read_tools._grep_the_tree("visitors", ".")


# --- the guest list ----------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["ls", "app"],
        ["cat", "package.json"],
        ["grep", "-rn", "visitors", "app/"],
        ["sed", "-n", "40,80p", "app/page.tsx"],
        ["sed", "-n", "-e", "1,5p", "app/page.tsx"],
        ["find", "app", "-name", "*.tsx"],
        ["wc", "-l", "app/page.tsx"],
        ["head", "-20", "package.json"],
    ],
)
def test_read_only_classics_are_admitted(argv: list[str]) -> None:
    assert check_the_guest_list(argv) is None


def test_the_guest_list_has_no_way_to_learn_which_chat_it_is_in() -> None:
    """★ R71, asserted from the signature — the cheapest proof there is.

    A policy that takes only `argv` cannot vary by chat kind, by deps, by settings or by
    which agent resolved the toolset, because none of those are reachable from inside it.
    That is stronger than any number of per-kind cases, which could only ever sample the
    behaviour; this closes the question.

    The import half matters too: `ChatKind` is not in this module's namespace at all, so no
    body here can name one even by accident."""
    parameters = list(inspect.signature(check_the_guest_list).parameters)
    assert parameters == ["argv"]
    assert check_the_guest_list.__closure__ is None
    assert not hasattr(read_tools, "ChatKind")


async def test_the_same_refusal_reaches_two_different_agents_byte_for_byte(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    """★ THE ONE THAT CARRIES R71 END TO END, through two genuinely different consumers.

    `read_only_toolset` is shared: a Plan chat resolves it through `toolsets_for_kind` over
    `ReadDeps`, and the classification review agent resolves it through its own accessor over
    `ReviewDeps` — a deps type with no chat kind on it at all, in a run that has no
    conversation behind it. If what the ability allows were a question about the surrounding
    run, these two are where the answers would differ.

    ZERO TRANSPORT CALLS IN BOTH is the other half of the claim, and it is the half that says
    WHERE the policy runs. The guest list is the first statement of the tool body, before any
    workspace call — so a refusal is not a command that ran and was judged afterwards, and a
    future approval layer could not be positioned to skip it."""
    reached: list[Sequence[str]] = []

    class SpyWorkspace:
        """Delegates everything, and records any argv that got as far as the transport."""

        label = workspace.label

        async def read_file(self, rel_path: str) -> str:
            return await workspace.read_file(rel_path)

        async def list_files(self) -> list[str]:
            return await workspace.list_files()

        async def search_files(
            self, pattern: re.Pattern[str], subdir: str | None
        ) -> list[read_tools.SearchHit]:
            return await workspace.search_files(pattern, subdir)

        async def exec_readonly(self, argv: Sequence[str]) -> read_tools.ReadExecResult:
            reached.append(argv)
            return await workspace.exec_readonly(argv)

    argv = ["npm", "install", "zod"]

    plan_capture: dict[str, Any] = {}
    plan_spy = SpyWorkspace()
    await _agent().run(
        "install zod",
        deps=ReadDeps(workspace=plan_spy, user_id=uuid.uuid4()),
        model=_capturing_model(
            [tool_turn("run_command", {"command": argv}), text_turn("ok")], plan_capture
        ),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )

    review_capture: dict[str, Any] = {}
    review_spy = SpyWorkspace()
    review_agent: Agent[ReviewDeps, str] = Agent(deps_type=ReviewDeps)
    await review_agent.run(
        "install zod",
        deps=ReviewDeps(user_id=uuid.uuid4(), workspace=review_spy),
        model=_capturing_model(
            [tool_turn("run_command", {"command": argv}), text_turn("ok")], review_capture
        ),
        toolsets=[read_only_toolset(lambda ctx: ctx.deps.workspace)],
    )

    # Byte-identical, not merely both-refused: a policy written down once produces one string.
    assert plan_capture["incoming"][1] == review_capture["incoming"][1]
    assert "not on the guest list" in plan_capture["incoming"][1]
    assert reached == []


@pytest.mark.parametrize("binary", ["npm", "node", "rm", "sh", "bash", "psql", "curl", "python3"])
def test_non_guest_binaries_are_bounced_with_teaching(binary: str) -> None:
    refusal = check_the_guest_list([binary, "anything"])
    assert refusal is not None
    assert "read-only" in refusal
    assert "read_file" in refusal  # points at the structured alternative


@pytest.mark.parametrize(
    "argv",
    [
        ["sed", "-i", "s/a/b/", "app/page.tsx"],
        ["sed", "-n", "w out.txt", "app/page.tsx"],
        ["sed", "-n", "1e", "app/page.tsx"],
        ["find", "app", "-delete"],
        ["find", "app", "-exec", "rm", "{}", ";"],
        ["find", "app", "-fprintf", "out", "%p"],
        ["tail", "-f", "app/page.tsx"],
        ["tail", "--follow=name", "app/page.tsx"],
    ],
)
def test_write_capable_forms_are_bounced(argv: list[str]) -> None:
    assert check_the_guest_list(argv) is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["cat", "/etc/passwd"],
        ["cat", "../secrets"],
        ["grep", "-r", "pw", "~/"],
        ["ls", "app/../.."],
    ],
)
def test_path_tokens_reaching_outside_are_bounced(argv: list[str]) -> None:
    assert check_the_guest_list(argv) is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["wc", "--files0-from=/etc/passwd"],
        ["grep", "-f/etc/passwd", "."],
        ["grep", "--file=../../x", "."],
        ["grep", "-rnf", "/etc/passwd", "."],
        ["wc", "--files0-from=../../outside"],
    ],
)
def test_paths_riding_on_a_flag_are_bounced(argv: list[str]) -> None:
    # The same escape class as the symlink instance, through a different door: a path attached
    # to a flag (`--flag=<path>`, `-f<path>`, or a short flag buried in a cluster) was never
    # vetted, because every `-` token was skipped wholesale.
    assert check_the_guest_list(argv) is not None


def test_flag_shaped_tokens_that_carry_no_path_still_pass() -> None:
    # The widened check must not start bouncing ordinary read flags: `-F` is not `-f`, and an
    # `=` value half that stays in-root is legitimate.
    assert check_the_guest_list(["grep", "-rnF", "visitors", "app/"]) is None
    assert check_the_guest_list(["grep", "--include=*.tsx", "visitors", "app/"]) is None
    assert check_the_guest_list(["grep", "--exclude-dir=dist", "visitors", "app/"]) is None


async def test_exec_readonly_contains_a_path_riding_on_a_flag(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # Layer 2 must contain the flag-attached form too — realpath, not just lexical vetting.
    with pytest.raises(WorkspacePathError):
        await workspace.exec_readonly(["wc", "--files0-from=/etc/passwd"])


def test_empty_argv_is_bounced() -> None:
    assert check_the_guest_list([]) is not None


# --- the tools, driven through a real run ------------------------------------


def _capturing_model(turns: list[ModelResponse], captured: dict[str, Any]) -> FunctionModel:
    iterator = iter(turns)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        texts: list[str] = []
        for message in messages:
            for part in getattr(message, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    texts.append(content)
        captured.setdefault("incoming", []).append("\n".join(texts))
        return next(iterator, text_turn("(exhausted)"))

    return FunctionModel(respond)


def _agent() -> Agent[ReadDeps, str]:
    return Agent(deps_type=ReadDeps)


def _deps(workspace: Any) -> ReadDeps:
    return ReadDeps(workspace=workspace, user_id=uuid.uuid4())


async def test_read_file_tool_returns_numbered_windowed_content(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("read_file", {"path": "app/page.tsx"}), text_turn("answered")], captured
    )
    result = await _agent().run(
        "what does the app show?",
        deps=_deps(workspace),
        model=model,
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    assert result.output == "answered"
    tool_feed = captured["incoming"][1]
    assert "1\texport default function VisitorLog() {" in tool_feed


async def test_read_file_tool_truncates_with_a_continue_hint(
    tree: Path, workspace: ExtractedSnapshotWorkspace
) -> None:
    (tree / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 1001)))
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("read_file", {"path": "long.txt"}), text_turn("ok")], captured
    )
    await _agent().run(
        "read it",
        deps=_deps(workspace),
        model=model,
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    tool_feed = captured["incoming"][1]
    assert "1000 lines total" in tool_feed
    assert "start_line=401" in tool_feed


async def test_run_command_tool_bounces_npm_in_run(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("run_command", {"command": ["npm", "install", "zod"]}), text_turn("ok")],
        captured,
    )
    await _agent().run(
        "install zod",
        deps=_deps(workspace),
        model=model,
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    # The refusal came back to the model as a retry, teaching the alternative.
    assert "not on the guest list for this read-only `run_command`" in captured["incoming"][1]


async def test_run_command_tool_output_is_redacted(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("run_command", {"command": ["cat", "secrets.env"]}), text_turn("ok")], captured
    )
    await _agent().run(
        "show the env file",
        deps=_deps(workspace),
        model=model,
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    tool_feed = captured["incoming"][1]
    assert "sup3rs3cretpw" not in tool_feed
    assert "***" in tool_feed


async def test_search_files_tool_rejects_a_bad_regex_with_teaching(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("search_files", {"pattern": "([unclosed"}), text_turn("ok")], captured
    )
    await _agent().run(
        "find it",
        deps=_deps(workspace),
        model=model,
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    assert "not a valid regular expression" in captured["incoming"][1]


# ═══════════════════════════════════════════════════════════════════════════════════════
# U22 / R28 — the two mirrored output caps, proved identical from ONE table
# ═══════════════════════════════════════════════════════════════════════════════════════
#
# `orchestrator/tools._redact_command_output` and `read_tools._cap_redact_cap` are the same
# algorithm written twice, on purpose: the read surface adds NO runtime edge into the orchestrator
# package (both module docstrings say why), so the copy cannot be replaced by an import. What a
# copy CAN be replaced by is a test — and it has to be ONE test over BOTH, not two tests that
# happen to agree today. Every case below runs through both functions and asserts, first, that
# they returned the same string; the behavioural assertions come after and are therefore
# assertions about both.

_MIDDLE_SECRET = "DATABASE_PASSWORD=hunter2-super-secret"
_TOP = "FATAL: the migration runner refused to start"
_BOTTOM = "error TS2322: Type 'string' is not assignable to type 'number'."


def _long_capture(middle: str | None = None) -> str:
    body = [f"  at frame {n:03d} of a long and tedious stack ({'x' * 30})" for n in range(140)]
    if middle is not None:
        body[len(body) // 2] = middle
    return "\n".join([_TOP, *body, _BOTTOM])


_CAP_CASES: list[tuple[str, str, int, str | None, list[str], list[str]]] = [
    (
        "short output is returned whole and unmarked",
        "exit fine\nnothing to see",
        4_000,
        "out_abc12345",
        ["exit fine", "nothing to see"],
        ["elided", "fetch_output_slice"],
    ),
    (
        "a long capture keeps head AND tail and states the loss",
        _long_capture(),
        4_000,
        "out_abc12345",
        [_TOP, _BOTTOM, "elided", 'fetch_output_slice(handle="out_abc12345"'],
        ["  at frame 070 "],
    ),
    (
        "with no handle the notice names no tool the mode does not have",
        _long_capture(),
        4_000,
        None,
        [_TOP, _BOTTOM, "elided", "re-run a narrower command"],
        ["fetch_output_slice"],
    ),
    (
        "a secret in the elided middle is masked before anything is cut",
        _long_capture(middle=_MIDDLE_SECRET),
        4_000,
        "out_abc12345",
        [_TOP, _BOTTOM],
        ["hunter2"],
    ),
    (
        # A FIRST LINE BIGGER THAN THE HEAD BUDGET is hard-capped mid-line, so it is only PARTLY
        # shown — and a slice addresses whole lines, so the range has to name it or the rest of
        # that line is unreachable in any number of calls. It used to start the range at line 2.
        "a head line cut mid-way is named in the range that recovers it",
        "X" * 20_000 + "\n" + "\n".join(f"ordinary line {i}" for i in range(200)),
        4_000,
        "out_abc12345",
        ["lines 1-", "start_line=1,", "only partly shown"],
        ["lines 2-"],
    ),
    (
        "a secret straddling the cut is not re-exposed as a fragment",
        ("A" * 1_969) + f" {_MIDDLE_SECRET} " + ("B" * 5_000),
        4_000,
        None,
        ["DATABASE_PASSWORD=***"],
        ["hunter2", "hunter2-sup"],
    ),
]


#: A multi-line quoted credential whose opening `KEY="` lands in the DROPPED MIDDLE of a capture
#: and whose body runs into the retained TAIL. `_SECRET_ASSIGN_RE`'s quoted arms span newlines on
#: purpose (a PEM, a passphrase), so the tail carries the VALUE with no key to identify it — which
#: is why cutting on line boundaries does not save it.
_PEM_BODY_SECRET = "hunter2NeverMaskedWithoutItsKey"


def _credential_straddling_the_dropped_middle(*, separator: str = '="') -> str:
    """A capture over the redactor's input cap, shaped so the credential's key is dropped and its
    body survives into the tail. The old head-only cap never rendered this text at all, which is
    what made retaining the tail a REGRESSION rather than an improvement.

    `separator` is what sits between the key and the opening quote, and it is a parameter because
    a COLOUR BYTE is a legitimate thing to find there — see the escape test below."""
    lead = "".join(f"ordinary build line {i}\n" for i in range(4_000))
    pem = "".join(
        f"MIIEowIBAAKCAQEA{i:04d}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n" for i in range(1_200)
    )
    return (
        lead
        + f"PRIVATE_KEY{separator}-----BEGIN RSA PRIVATE KEY-----\n"
        + pem
        + f'{_PEM_BODY_SECRET}\n-----END RSA PRIVATE KEY-----"\n'
        + "".join(f"trailing line {i}\n" for i in range(50))
    )


#: The marker sitting in the FIRST body line of a credential that opens inside the HEAD of an
#: over-cap capture — the half of the cut the tail guard never looked at.
_HEAD_BODY_SECRET = "hunter2InTheHeadWithNoClosingQuote"


def _credential_opened_inside_the_head() -> str:
    """A capture over the input cap whose credential opens EARLY — inside the head — and whose
    closing quote falls far past the head's cut point.

    The masker cannot touch this: `_SECRET_ASSIGN_RE`'s quoted arms need the closing delimiter and
    its bare arm excludes quote characters, so the assignment matches nothing at all and the
    body renders verbatim. The head is not "masked with its key present"; it is unmatched."""
    lead = "".join(f"ordinary build line {i}\n" for i in range(100))
    pem = "".join(
        f"MIIEowIBAAKCAQEA{i:04d}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n" for i in range(1_200)
    )
    return (
        lead
        + 'PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n'
        + f"{_HEAD_BODY_SECRET}\n"
        + pem
        + '-----END RSA PRIVATE KEY-----"\n'
        + "".join(f"trailing line {i}\n" for i in range(50))
    )


# The noise case is deliberately NOT in the table above: it is the ONE dimension on which the two
# copies are designed to differ, so asserting byte-equality on it would pin a bug as correct.
# It is pinned per-copy instead, immediately below.
_NOISY_CAPTURE = (
    "npm notice New major version of npm available!\n"
    "12 packages are looking for funding\n"
    "  run `npm fund` for details\n"
    "npm warn deprecated request@2.88.2: request has been deprecated\n"
    "3 vulnerabilities (1 moderate, 2 high)"
)


@pytest.mark.parametrize(
    ("text", "budget", "handle", "present", "absent"),
    [case[1:] for case in _CAP_CASES],
    ids=[case[0] for case in _CAP_CASES],
)
def test_the_two_mirrored_output_caps_behave_identically(
    text: str, budget: int, handle: str | None, present: list[str], absent: list[str]
) -> None:
    """★ THE ANTI-DRIFT TEST. Mutation check: change either copy's head/tail split, budget
    handling or redaction ordering and the equality assertion goes red before any of the
    behavioural ones do — which is the point, because a drift that keeps both copies
    self-consistent is exactly the one no per-module test would catch.

    Compared against `denoise=False` because that is the ACTUAL parity claim: the two pipelines are
    identical *modulo the noise arm*, which the read copy does not have and is not supposed to have
    (see the block comment in `read_tools`). Comparing against the default `denoise=True` would
    assert a design difference away, and the only way to make it pass would be to teach the read
    surface to drop lines out of file content — the exact silent edit that comment forbids."""
    read_mode = read_tools._cap_redact_cap(text, budget=budget, handle=handle)
    write_mode = _redact_command_output(text, budget=budget, handle=handle, denoise=False)
    assert read_mode == write_mode, "the mirrored output caps have drifted apart"
    for expected in present:
        assert expected in read_mode
    for unwanted in absent:
        assert unwanted not in read_mode


def test_a_credential_body_in_the_tail_is_withheld_not_egressed() -> None:
    """★ THE U22 REGRESSION, and the one case where this branch was WORSE than what it replaced.

    `_SECRET_ASSIGN_RE`'s quoted arms deliberately span newlines, so "a credential is a shape on
    ONE line" — the assumption the line-boundary capture cut rests on — is false for those arms.
    When the opening `KEY="` falls in the dropped middle, the retained tail is the inside of a
    private key with nothing left to identify it: it matches none of the redactor's shapes and
    egressed in the clear, into the model's context AND into the slice buffer behind
    `fetch_output_slice`.

    The old head-only cap discarded the tail entirely, so this text never left the container. That
    is what makes it a regression and not a pre-existing gap.

    Both copies are asserted, because both cut captures the same way."""
    raw = _credential_straddling_the_dropped_middle()

    write_mode = _redact_command_output(raw, budget=RUN_COMMAND_OUTPUT_MAX_CHARS)
    read_mode = read_tools._cap_redact_cap(raw, budget=RUN_COMMAND_OUTPUT_MAX_CHARS)

    assert _PEM_BODY_SECRET not in write_mode
    assert _PEM_BODY_SECRET not in read_mode
    # Withheld, and SAID so — a silently missing tail is the failure mode the capture marker
    # exists to prevent, and the model needs a next action rather than a gap.
    assert "withheld" in write_mode
    assert "withheld" in read_mode
    assert "re-run a narrower command" in write_mode


def test_a_truncated_capture_with_no_credential_still_keeps_its_tail() -> None:
    """THE OTHER HALF. A guard that withholds every truncated tail would undo the whole reason the
    tail is rendered — `Failed to compile.` and the failing assertion live at the END, and getting
    them there is what stopped the agent re-running the command. So the withholding must be rare
    and conditional, not the default."""
    lead = "".join(f"ordinary build line {i}\n" for i in range(4_000))
    raw = lead + "Failed to compile.\nerror TS2304: Cannot find name 'foo'.\n"
    # A budget wider than the capture window so this isolates the CAPTURE layer (where the guard
    # lives) rather than the render layer, which does its own head/tail cut and can legitimately
    # drop the capture marker to fit.
    budget = REDACT_INPUT_MAX_CHARS * 2

    write_mode = _redact_command_output(raw, budget=budget)
    read_mode = read_tools._cap_redact_cap(raw, budget=budget)

    for rendered in (write_mode, read_mode):
        assert "dropped at capture" in rendered  # it really was truncated
        assert "Failed to compile." in rendered  # ...and the tail survived anyway
        assert "withheld" not in rendered


def test_a_credential_body_in_the_head_is_withheld_not_egressed() -> None:
    """★ THE OTHER HALF OF THE SAME CUT, and the one the tail guard did not cover.

    A value that opens INSIDE the head and closes past it is unmaskable for exactly the reason a
    tail beginning inside one is: the redactor's quoted arms need their closing delimiter, and its
    bare arm excludes quote characters, so the assignment matches NOTHING. The head was rendered
    unconditionally — so the visible prefix of a real bearer credential went into the model's
    context and into the persisted step row, in the clear.

    Mutation check: drop the `cut_before_an_open_credential` call from either copy's
    `_redacted_lines` and the secret assertion goes red while everything else stays green.

    Both copies are asserted, because both cut captures the same way."""
    raw = _credential_opened_inside_the_head()

    write_mode = _redact_command_output(raw, budget=RUN_COMMAND_OUTPUT_MAX_CHARS)
    read_mode = read_tools._cap_redact_cap(raw, budget=RUN_COMMAND_OUTPUT_MAX_CHARS)

    for rendered in (write_mode, read_mode):
        assert _HEAD_BODY_SECRET not in rendered
        assert "MIIEowIBAAKCAQEA0000" not in rendered  # nor the body lines after it
        # Withheld, and SAID so — the same courtesy the tail gets.
        assert "withheld" in rendered
        # LIVENESS: the cut is to the credential's own line, not to the whole capture. Everything
        # that came before it is still there, or a guard that answered "yes" to everything would
        # pass this test while deleting every build log the tool ever returned.
        assert "ordinary build line 3" in rendered


@pytest.mark.parametrize(
    "separator",
    ['\x1b[39m="', '\x1b[0m="', '\u200b="', '\ufeff="'],
    ids=["sgr-reset", "sgr-plain", "zero-width-space", "byte-order-mark"],
)
def test_an_escape_between_the_key_and_its_quote_does_not_unlock_the_tail(separator: str) -> None:
    """★ THE GUARD AND THE MASKER MUST READ THE SAME BYTES.

    `scrub_untrusted` masks DE-ESCAPED text, so a guard that scanned the RAW text disagreed with
    it about the same input: neither ESC nor U+200B is `\\s`, so one colour byte between the key
    and its opening quote answered "nothing is open" and the tail — the inside of the private key
    — shipped in the clear. Colourised CLI output puts an SGR reset in exactly that position as a
    matter of routine, so this is not only an adversarial shape.

    Mutation check: drop `strip_control_sequences` from `leaves_a_credential_value_open` and every
    row here goes red while the plain-separator test above stays green."""
    raw = _credential_straddling_the_dropped_middle(separator=separator)

    write_mode = _redact_command_output(raw, budget=RUN_COMMAND_OUTPUT_MAX_CHARS)
    read_mode = read_tools._cap_redact_cap(raw, budget=RUN_COMMAND_OUTPUT_MAX_CHARS)

    for rendered in (write_mode, read_mode):
        assert _PEM_BODY_SECRET not in rendered
        assert "-----END RSA PRIVATE KEY-----" not in rendered
        assert "withheld" in rendered
        # LIVENESS: the head is still rendered in full — this withholds a tail, not a capture.
        assert "ordinary build line 3" in rendered


def test_the_capture_guard_stays_inside_its_wall_clock_budget() -> None:
    """★ THE MUTATION TARGET for the input bound: raise `CREDENTIAL_OPEN_SCAN_MAX_CHARS` back
    toward its old 8,000,000 and this goes red, because the scan below stops being bounded by the
    ceiling and starts being bounded by whatever the app printed.

    WHY WALL-CLOCK AND NOT A CONCURRENCY PROBE. The cost is paid ON the event loop and cannot be
    moved off it: `re` does not release the GIL, so a non-matching scan — one C call — blocks
    every other request in the process for its whole duration no matter which thread it runs in
    (measured: a 1 ms ticker gets 0 ticks across a 304 ms `to_thread` scan of this pattern). The
    only lever is how much text the scan may ever see, which is what this pins. 1s never flakes on
    a bounded linear scan — the ceiling measures ~80 ms, and the capture below is 25x past it, so
    the margin is the bound rather than the machine — and it fails loudly on either a raised bound
    or a superlinear regression, the pair the ReDoS learning says to guard together."""
    # Well past the scan ceiling: what is asserted is that the BOUND decides the cost, not the
    # input. Identifier-shaped bytes, because that is what makes this pattern work hardest. Built
    # OUTSIDE the stopwatch — 6 MB of string concatenation is not what is being measured.
    raw = ("A" * 79 + "\n") * 80_000
    started = time.perf_counter()
    rendered = _redact_command_output(raw, budget=4_000)
    elapsed = time.perf_counter() - started

    assert "elided" in rendered  # LIVENESS: it really rendered the capture, it did not bail
    assert elapsed < 1.0, f"the open-credential scan took {elapsed:.2f}s"


def test_the_open_credential_detector_answers_both_ways() -> None:
    """The detector itself, at the unit level — the two answers the guard above turns on, plus the
    fail-toward-withholding case. An earlier opener that CLOSED must not poison the answer, or
    every build log containing one quoted credential would lose its tail forever."""
    assert leaves_a_credential_value_open('PRIVATE_KEY="-----BEGIN\nstill going') is True
    assert leaves_a_credential_value_open('PRIVATE_KEY="closed"\nmore output') is False
    assert leaves_a_credential_value_open("no credential here at all\n") is False
    # Unscannably large input is not a licence: it answers "assume open".
    assert leaves_a_credential_value_open("x" * (CREDENTIAL_OPEN_SCAN_MAX_CHARS + 1)) is True


def test_the_write_copy_drops_predictable_noise_but_keeps_the_signal() -> None:
    """The noise arm, pinned on the copy that HAS one. A dependency-manager solicitation and a
    vulnerability advisory arrive on the same stream, so the filter earns its keep only if it can
    tell them apart — dropping an advisory to save four lines of chatter is a bad trade."""
    kept = _redact_command_output(_NOISY_CAPTURE, budget=4_000, denoise=True)

    assert "deprecated" in kept
    assert "vulnerabilities" in kept
    assert "npm notice" not in kept
    assert "looking for funding" not in kept
    assert "npm fund" not in kept


def test_the_read_copy_never_drops_a_line_of_what_it_was_asked_to_read() -> None:
    """The other half, and the reason the copies differ. Everything reaching the read surface is
    FILE CONTENT — `check_the_guest_list` admits only `ls, cat, head, tail, grep, sed, find, wc`,
    and `search_files` renders hits out of files. Dropping a line there is not a saving, it is a
    silent edit: a `sed -n '40,80p'` that answers 40 of the 41 lines it was asked for, and an
    `edit_file` composed from that read failing to match with nothing on screen to explain why.

    Written over the same capture the Write copy de-noises, so the two tests read as the pair they
    are. These are PRESENCE assertions throughout: an absence assertion here would pass by vacuity
    against a function that returned "", which is exactly the false green to avoid."""
    read = read_tools._cap_redact_cap(_NOISY_CAPTURE, budget=4_000)

    for line in _NOISY_CAPTURE.splitlines():
        assert line.strip() in read, f"the read surface silently dropped: {line!r}"
