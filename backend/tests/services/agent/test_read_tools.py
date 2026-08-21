"""U8 — the jailed read-only tool surface (workspace, guest-list policy, tools in-run).

Three layers, each tested where it is enforced (testing.md: test the DECISION at the site
that makes it): the `ExtractedSnapshotWorkspace` jail (path resolution, symlink
containment, byte caps, scrubbed subprocess env), the `check_the_guest_list` argv policy
(allowlist, deny flags, sed script vetting, path-token vetting), and the tool layer driven
through a REAL pydantic-ai run (FunctionModel — refusals as ModelRetry, no-app-yet as a
truthful NORMAL result, redacted command output).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.db.models.conversation import ConversationMode
from src.services.agent import read_tools
from src.services.agent.read_tools import (
    EmptyProjectWorkspace,
    ExtractedSnapshotWorkspace,
    WorkspacePathError,
    check_the_guest_list,
)
from src.services.agent.toolsets import ReadDeps, toolsets_for_mode, workspace_from_read_deps
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
        toolsets=toolsets_for_mode(ConversationMode.ASK, workspace_from_read_deps),
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
        toolsets=toolsets_for_mode(ConversationMode.ASK, workspace_from_read_deps),
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
        toolsets=toolsets_for_mode(ConversationMode.ASK, workspace_from_read_deps),
    )
    # The refusal came back to the model as a retry, teaching the alternative.
    assert "not available in this read-only mode" in captured["incoming"][1]


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
        toolsets=toolsets_for_mode(ConversationMode.ASK, workspace_from_read_deps),
    )
    tool_feed = captured["incoming"][1]
    assert "sup3rs3cretpw" not in tool_feed
    assert "***" in tool_feed


async def test_no_app_yet_is_a_truthful_normal_result(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    captured: dict[str, Any] = {}
    model = _capturing_model([tool_turn("list_files", {}), text_turn("told the user")], captured)
    result = await _agent().run(
        "what files are there?",
        deps=_deps(EmptyProjectWorkspace(app_id=uuid.uuid4())),
        model=model,
        toolsets=toolsets_for_mode(ConversationMode.ASK, workspace_from_read_deps),
    )
    # A NORMAL tool result (the run completes without retries), truthful in content.
    assert result.output == "told the user"
    assert "No app exists yet" in captured["incoming"][1]


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
        toolsets=toolsets_for_mode(ConversationMode.ASK, workspace_from_read_deps),
    )
    assert "not a valid regular expression" in captured["incoming"][1]
