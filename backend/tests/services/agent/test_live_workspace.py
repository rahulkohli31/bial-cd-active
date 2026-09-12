"""`LiveSandboxWorkspace`: the two structured reads routed through the supervisor.

Driven through `FakeSandbox`, whose `exec` returns scripted stdout and RECORDS the argv — which
is what makes both halves of the ignore story assertable: the prune/`--exclude-dir` flags that
keep the cost off the sandbox, and the post-filter that keeps a listing honest when the remote
walk ignores them (the fake honors neither, exactly like a grep without `--exclude-dir`).

The guard tests are the point of the file. It is LEXICAL only, and it is a hygiene filter, not a
security control — so what is asserted is that a bad path is refused BEFORE the transport, with a
message the model can act on, not that the sandbox is contained (the supervisor's jail owns that).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.db.models.conversation import ChatKind
from src.services.agent.read_tools import (
    LIST_MAX_ENTRIES,
    SEARCH_MAX_HITS,
    LiveSandboxWorkspace,
    ReadOnlyWorkspace,
    WorkspacePathError,
)
from src.services.agent.toolsets import ReadDeps, toolsets_for_kind, workspace_from_read_deps
from src.services.orchestrator.deps import SandboxSession
from src.services.sandbox import ExecResult
from tests.services.orchestrator.fake_sandbox import FAKE_SUPERVISOR_TOKEN, FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn


def _live(fake: FakeSandbox, stdout: str = "", *, exit_code: int = 0) -> LiveSandboxWorkspace:
    fake.default_result = ExecResult(stdout=stdout, stderr="", exit=exit_code)
    return LiveSandboxWorkspace(
        session=SandboxSession(sandbox_client=fake, handle=fake.handle(), app_id=uuid.uuid4())
    )


def test_it_satisfies_the_read_only_workspace_protocol() -> None:
    # The four type gates check structural conformance AT an assignment site, and nothing else in
    # this commit has one — the turn engine that pins the workspace lands separately. Without this
    # binding, a drifted signature would only be caught over there.
    surface: ReadOnlyWorkspace = _live(FakeSandbox())
    assert surface.label == "your app's live workspace"


# --- list_files --------------------------------------------------------------


async def test_list_files_returns_sorted_workspace_relative_paths() -> None:
    fake = FakeSandbox()
    workspace = _live(fake, "./package.json\n./app/page.tsx\n./app/layout.tsx\n")
    assert await workspace.list_files() == ["app/layout.tsx", "app/page.tsx", "package.json"]


async def test_list_files_runs_find_with_the_heavy_dirs_pruned() -> None:
    # The cost half: the live tree really has node_modules on disk, so the prune must ride the
    # command rather than being cleaned up after 40k paths crossed the wire.
    fake = FakeSandbox()
    await _live(fake, "./app/page.tsx\n").list_files()
    argv = fake.command_calls[0]
    assert argv[:2] == ["find", "."]
    assert "-prune" in argv
    for heavy in (".git", ".next", ".turbo", "dist", "node_modules"):
        assert heavy in argv


async def test_list_files_drops_the_ignore_set_the_remote_walk_left_in() -> None:
    # The correctness half: the fake honors no prune at all — which is exactly the state of the
    # world where a `find` that ignored the flags would flood the listing.
    fake = FakeSandbox()
    workspace = _live(
        fake,
        "./app/page.tsx\n"
        "./node_modules/react/index.js\n"
        "./.next/static/chunk.js\n"
        "./.git/config\n"
        "./dist/bundle.js\n"
        "./.turbo/cache.log\n",
    )
    assert await workspace.list_files() == ["app/page.tsx"]


async def test_list_files_is_uncapped_so_the_tool_can_say_it_truncated() -> None:
    # The workspace stays uncapped like its snapshot twin: the entry cap belongs to the tool
    # layer, which is the only place that can also TELL the model the listing was cut.
    fake = FakeSandbox()
    listing = "".join(f"./app/f{index}.tsx\n" for index in range(LIST_MAX_ENTRIES + 40))
    assert len(await _live(fake, listing).list_files()) == LIST_MAX_ENTRIES + 40


async def test_the_entry_cap_and_its_truncation_notice_reach_the_model() -> None:
    fake = FakeSandbox()
    listing = "".join(f"./app/f{index}.tsx\n" for index in range(LIST_MAX_ENTRIES + 40))
    feed = await _tool_feed(_live(fake, listing), "list_files", {})
    assert "40 more files not shown" in feed


# --- search_files ------------------------------------------------------------


async def test_search_files_parses_grep_hits() -> None:
    fake = FakeSandbox()
    workspace = _live(
        fake,
        "./app/page.tsx:12:  const visitors = []\n./lib/db.ts:3:export const visitors = table\n",
    )
    hits = await workspace.search_files(re.compile("visitors"), None)
    assert [(hit.path, hit.line_no, hit.line) for hit in hits] == [
        ("app/page.tsx", 12, "const visitors = []"),
        ("lib/db.ts", 3, "export const visitors = table"),
    ]


async def test_search_files_greps_the_python_dialect_with_the_heavy_dirs_excluded() -> None:
    fake = FakeSandbox()
    await _live(fake, "").search_files(re.compile("visitors|guests"), None)
    argv = fake.command_calls[0]
    assert argv[0] == "grep"
    assert "-rnE" in argv  # BRE would read the model's `|` as a literal pipe
    assert "--exclude-dir=node_modules" in argv
    # `-e` and `--` are what keep a pattern or a path that starts with `-` out of the flag slot.
    assert argv[-4:] == ["-e", "visitors|guests", "--", "."]


async def test_search_files_scopes_to_a_subdir() -> None:
    fake = FakeSandbox()
    hits = await _live(fake, "./app/page.tsx:12:visitors\n").search_files(
        re.compile("visitors"), "app"
    )
    assert fake.command_calls[0][-1] == "app"
    assert [hit.path for hit in hits] == ["app/page.tsx"]


async def test_search_files_caps_the_hits() -> None:
    fake = FakeSandbox()
    stdout = "".join(f"./app/f{index}.tsx:1:visitors\n" for index in range(SEARCH_MAX_HITS + 50))
    assert len(await _live(fake, stdout).search_files(re.compile("visitors"), None)) == (
        SEARCH_MAX_HITS
    )


async def test_search_files_reads_no_match_off_stdout_not_the_exit_code() -> None:
    # grep answers "no matches" with exit 1. Treating that as a failure would turn every
    # fruitless search into an error the model has to interpret.
    fake = FakeSandbox()
    assert await _live(fake, "", exit_code=1).search_files(re.compile("nope"), None) == []


async def test_search_files_skips_lines_that_carry_no_hit() -> None:
    fake = FakeSandbox()
    workspace = _live(
        fake,
        "Binary file ./public/logo.png matches\n"
        "grep: ./app/broken: Permission denied\n"
        "./app/page.tsx:7:visitors\n",
    )
    hits = await workspace.search_files(re.compile("visitors"), None)
    assert [hit.path for hit in hits] == ["app/page.tsx"]


async def test_search_files_drops_hits_from_the_ignore_set() -> None:
    fake = FakeSandbox()
    workspace = _live(
        fake, "./node_modules/react/index.js:9:visitors\n./app/page.tsx:7:visitors\n"
    )
    hits = await workspace.search_files(re.compile("visitors"), None)
    assert [hit.path for hit in hits] == ["app/page.tsx"]


# --- the lexical guard -------------------------------------------------------


@pytest.mark.parametrize("subdir", ["/etc/passwd", "~/x", "a/../../b", "node_modules/x"])
async def test_escaping_and_ignored_subdirs_are_refused_before_the_transport(
    subdir: str,
) -> None:
    fake = FakeSandbox()
    workspace = _live(fake, "")
    with pytest.raises(WorkspacePathError):
        await workspace.search_files(re.compile("visitors"), subdir)
    # Fail-closed: the refusal happens above the seam, so the bad path never becomes an operand.
    assert fake.command_calls == []


async def test_the_refusal_teaches_instead_of_just_denying() -> None:
    fake = FakeSandbox()
    with pytest.raises(WorkspacePathError) as refusal:
        await _live(fake, "").search_files(re.compile("x"), "node_modules")
    assert "node_modules" in str(refusal.value)
    assert "app's source" in str(refusal.value)


# --- the two the Protocol demands but Write never calls ----------------------


async def test_read_file_reads_the_running_container() -> None:
    """These two used to refuse, because Write's toolset filters them out and only Write had a
    live workspace. Ask and Plan read the live container now, and they DO call both — so a
    refusal here would have made every question about a built app fail."""
    fake = FakeSandbox()
    body = "export default function Page() {}\n"
    assert await _live(fake, body).read_file("app/page.tsx") == body
    assert ["cat", "--", "app/page.tsx"] in fake.command_calls


async def test_read_file_refuses_a_path_before_it_becomes_a_command() -> None:
    fake = FakeSandbox()
    with pytest.raises(WorkspacePathError):
        await _live(fake, "").read_file("../../etc/passwd")
    assert fake.command_calls == []  # vetted ABOVE the transport, as with the other reads


async def test_read_file_reports_the_reason_a_file_could_not_be_read() -> None:
    """A non-zero `cat` is the honest 'no such file' — surfaced as a teaching error rather
    than an empty string, which would read to the model as an empty file."""
    fake = FakeSandbox()
    with pytest.raises(WorkspacePathError) as refusal:
        await _live(fake, "", exit_code=1).read_file("app/missing.tsx")
    assert "list_files" in str(refusal.value)


async def test_exec_readonly_runs_the_argv_in_the_container() -> None:
    """The POLICY did not move — the guest list, deny flags and path vetting all still run in
    the tool layer above this. Only the environment moved, from a bare server-side checkout
    into the app's real container."""
    fake = FakeSandbox()
    result = await _live(fake, "page.tsx\n").exec_readonly(["ls", "app"])
    assert (result.exit, result.stdout) == (0, "page.tsx\n")
    assert ["ls", "app"] in fake.command_calls


async def test_no_refusal_or_result_carries_the_handle_token() -> None:
    # The live workspace is the first read surface holding a live supervisor bearer.
    fake = FakeSandbox()
    workspace = _live(fake, "./app/page.tsx\n")
    with pytest.raises(WorkspacePathError) as refusal:
        await workspace.search_files(re.compile("x"), "/etc")
    assert FAKE_SUPERVISOR_TOKEN not in str(refusal.value)
    assert FAKE_SUPERVISOR_TOKEN not in "\n".join(await workspace.list_files())


# --- driving the real tool layer over the live workspace ---------------------


async def _tool_feed(workspace: LiveSandboxWorkspace, tool: str, args: dict[str, Any]) -> str:
    """Run the tool for real (Ask's toolset — the same `read_only_toolset` bodies Write borrows
    its two structured reads from) and return what the tool handed back to the model."""
    captured: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        texts: list[str] = []
        for message in messages:
            for part in getattr(message, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    texts.append(content)
        captured.append("\n".join(texts))
        return next(turns, text_turn("answered"))

    turns = iter([tool_turn(tool, args), text_turn("answered")])
    await Agent(deps_type=ReadDeps).run(
        "have a look",
        deps=ReadDeps(workspace=workspace, user_id=uuid.uuid4()),
        model=FunctionModel(respond),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    return captured[1]


async def test_the_tool_layer_names_the_live_workspace_when_there_is_nothing_to_show() -> None:
    fake = FakeSandbox()
    feed = await _tool_feed(_live(fake, ""), "search_files", {"pattern": "visitors"})
    assert "your app's live workspace" in feed


# --- the attachments prefix ---------------------------------------


async def test_the_attachments_prefix_reaches_the_second_root() -> None:
    """★ ATTACHMENTS LIVE OUTSIDE THE APP TREE, so an agent has to be able to NAME them.

    The app tree is what gets snapshotted, restored, saved and deployed; a file someone attached
    to a chat must not travel with any of that. Keeping it in a sibling directory means there is
    nothing to exclude — but a sibling is unreachable unless the read surface can address it.

    Mutation receipt: drop `to_container_path` from `read_file` and the argv carries the bare
    `.attachments/roster.xlsx`, which resolves inside the app tree and finds nothing.
    """
    fake = FakeSandbox()
    workspace = _live(fake, stdout="badge,name\n")

    await workspace.read_file(".attachments/roster.xlsx")

    argv = fake.command_calls[-1]
    assert "/workspace/attachments/roster.xlsx" in argv
    assert ".attachments/roster.xlsx" not in argv


async def test_an_ordinary_app_path_is_not_translated() -> None:
    """The translation is for exactly one prefix and a no-op for everything else — an app path
    must reach the container unchanged or every existing read breaks."""
    fake = FakeSandbox()
    workspace = _live(fake, stdout="export default function Page() {}\n")

    await workspace.read_file("app/page.tsx")

    argv = fake.command_calls[-1]
    assert "app/page.tsx" in argv
    assert not any("/workspace/attachments" in token for token in argv)


async def test_the_prefix_is_dotted_so_a_real_app_directory_cannot_be_shadowed() -> None:
    """A bare `attachments/` would shadow an app that happened to contain a directory of that
    name — silently reading somebody's chat files when they asked for their own source. The
    reserved prefix is dotted, and the undotted spelling stays an ordinary app path."""
    fake = FakeSandbox()
    workspace = _live(fake, stdout="")

    await workspace.read_file("attachments/notes.md")

    argv = fake.command_calls[-1]
    assert "attachments/notes.md" in argv
    assert not any("/workspace/attachments" in token for token in argv)


async def test_the_prefix_does_not_relax_the_shared_path_guard() -> None:
    """★ THE PART THAT MUST NOT HAVE MOVED. `_vet_path_token` is shared with the reviewer agent,
    which runs over untrusted project contents, and it deliberately cannot tell which agent is
    calling. So the prefix is an ordinary relative path that PASSES the existing checks — it is
    not an exemption from them, and every escape the guard refused before is still refused when
    it is spelled with the prefix in front."""
    workspace = _live(FakeSandbox())

    for bad in (
        "/workspace/attachments/roster.xlsx",  # absolute: still refused
        "~/.attachments/roster.xlsx",  # home-relative: still refused
        ".attachments/../../etc/passwd",  # climbing out: still refused
        ".attachments/../app/page.tsx",  # crossing into the app tree: still refused
    ):
        with pytest.raises(WorkspacePathError):
            await workspace.read_file(bad)


async def test_search_can_be_scoped_to_the_attachments_root() -> None:
    """`search_files`' subdir operand goes through the same translation, after the same vetting."""
    fake = FakeSandbox()
    workspace = _live(fake, stdout="")

    await workspace.search_files(re.compile("badge"), ".attachments")

    argv = fake.command_calls[-1]
    assert "/workspace/attachments" in argv
