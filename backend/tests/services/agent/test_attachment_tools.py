"""The Plan chat's attachment capability — what it allows, and what it refuses.

`test_toolsets.py` asserts WHO is offered this tool. This file asserts what the tool does with what
it is given: the scope of the command it builds, and the three ways it can fail.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.toolsets import FunctionToolset
from structlog.testing import capture_logs

from src.services.agent.attachment_tools import (
    READER_PATH,
    AttachmentReader,
    attachment_toolset,
)
from src.services.orchestrator.deps import SandboxSession


@dataclass
class _Recorder:
    """Stands in for the reader, recording the path it was asked for.

    CARRIES A SESSION because the tool body binds `app_id` on the named-failure log. A double
    without one fails as an `AttributeError` that reads like a bug in the code under test.
    """

    result: str = '{"ok": true, "file": "roster.xlsx", "rows": 5000}'
    raises: Exception | None = None
    seen: list[str] | None = None
    session: Any = field(default_factory=lambda: SimpleNamespace(app_id=uuid.uuid4()))

    async def read(self, path: str) -> str:
        if self.seen is None:
            self.seen = []
        self.seen.append(path)
        if self.raises is not None:
            raise self.raises
        return self.result


def _tool(reader: Any) -> Any:
    toolset: FunctionToolset[Any] = attachment_toolset(lambda _ctx: reader)
    return toolset.tools["read_attachment"].function


async def test_it_reads_an_attachment_and_returns_the_manifest() -> None:
    reader = _Recorder()

    out = await _tool(reader)(None, ".attachments/roster.xlsx")

    assert reader.seen == [".attachments/roster.xlsx"]
    assert json.loads(out)["rows"] == 5000


async def test_it_refuses_a_path_that_is_not_an_attachment() -> None:
    """★ THE SCOPE. R14 requires a capability scoped to attachments — never a widening of which
    paths the agent may name. A tool that would read any path is a second, unguarded `read_file`,
    and it runs `python3`, which the shared read surface deliberately does not offer.

    Mutation receipt: drop the `is_an_attachment_path` guard and this passes an app path straight
    to the interpreter.
    """
    reader = _Recorder()

    for path in ("app/page.tsx", "/etc/passwd", "../../secrets", "attachments/x.xlsx"):
        with pytest.raises(ModelRetry):
            await _tool(reader)(None, path)

    assert reader.seen is None  # nothing reached the container


@pytest.mark.parametrize(
    "escape",
    [
        ".attachments/../../etc/roster.csv",
        ".attachments/../app/lib/secrets.csv",
        ".attachments/nested/../../../../var/data.xlsx",
    ],
)
async def test_a_traversal_out_of_the_attachments_root_is_refused(escape: str) -> None:
    """★ THE PREFIX IS NOT CONTAINMENT.

    `is_an_attachment_path` answers one question — does this name the reserved prefix — and
    every string below answers it yes. This tool then builds an argv and hands it to `exec`,
    which does NOT pass through the supervisor's `_resolve`, so an unvetted `..` reached the
    reader and it would open any `.csv`/`.xlsx`/`.docx`/`.pptx`/`.tsv` in the container.

    It is reachable rather than theoretical: the path is model-chosen, and this feature's own
    rule holds that attachment content is untrusted — a spreadsheet cell that talks an agent
    into a traversal is exactly what that rule anticipates.

    Mutation receipt: drop the `refuse_unsafe_path` call and each of these reaches `exec`.
    """
    reader = _Recorder()

    with pytest.raises(ModelRetry):
        await _tool(reader)(None, escape)

    assert reader.seen is None, f"{escape} reached the container"


async def test_the_command_is_the_shipped_reader_over_the_container_path() -> None:
    """★ THE ARGV IS THE SCOPE, AND THE PATH IN IT MUST BE THE CONTAINER'S.

    The command is a fixed interpreter, a fixed script path and one operand — no input shape turns
    this into a way to run something else. But the operand also has to be a path the container can
    resolve, and that is the half this test used to get wrong: it asserted the argv carried
    `.attachments/roster.xlsx`, the MODEL-facing token, which is exactly what the code did. Both
    were self-consistent and both were wrong.

    `exec` runs in the app root, so a relative `.attachments/…` resolves to
    `/workspace/app/.attachments/…` and the reader truthfully reports the file missing while it
    sits in `/workspace/attachments`. Driving the real UI is what found it: the agent said the
    folder was not there, and it was right.

    Mutation receipt: drop `to_container_path` from `AttachmentReader.read` and this goes red on
    the prefix — which is the assertion the old version of this test was missing."""
    calls: list[list[str]] = []

    class _Client:
        async def exec(self, _handle: Any, argv: list[str], *, timeout_s: int) -> Any:
            calls.append(argv)
            return type("R", (), {"stdout": '{"ok": true}', "stderr": "", "exit": 0})()

    # A DOUBLE RATHER THAN A `SandboxSession`, and cast because it is one deliberately: the
    # reader touches exactly two attributes, and building a whole session here would hide which
    # two by supplying twenty. The cast is the claim the double makes, written down.
    session = cast(
        SandboxSession, type("S", (), {"sandbox_client": _Client(), "handle": object()})()
    )
    await AttachmentReader(session=session).read(".attachments/roster.xlsx")

    assert calls == [["python3", READER_PATH, "/workspace/attachments/roster.xlsx"]]
    # The model-facing token must NOT survive into the command.
    assert ".attachments/" not in calls[0][2]


async def test_a_transport_failure_is_a_retry_not_a_fabricated_answer() -> None:
    """The model must not paper over a failed read by describing what the file might contain."""
    reader = _Recorder(raises=RuntimeError("sandbox gone"))

    with pytest.raises(ModelRetry) as caught:
        await _tool(reader)(None, ".attachments/roster.xlsx")

    assert "guessing" in str(caught.value)


async def test_output_that_is_not_the_readers_contract_is_a_retry() -> None:
    """The reader always prints one JSON object and exits 0, including for a corrupt file. Anything
    else means the script is missing or broken — which the model must not describe around."""
    reader = _Recorder(result="Traceback (most recent call last):\n  ...")

    with pytest.raises(ModelRetry):
        await _tool(reader)(None, ".attachments/roster.xlsx")


async def test_a_named_failure_is_returned_rather_than_retried() -> None:
    """★ A DAMAGED FILE IS AN ANSWER. Retrying it would fail identically; the citizen should be
    told the file is damaged and what to do, which is what the reader's failure shape carries."""
    reader = _Recorder(
        result=json.dumps(
            {
                "ok": False,
                "file": "roster.xlsx",
                "error": {
                    "code": "encrypted",
                    "message": "locked",
                    "next": "Remove the password.",
                },
            }
        )
    )

    out = await _tool(reader)(None, ".attachments/roster.xlsx")

    assert json.loads(out)["error"]["code"] == "encrypted"


# ── Every reader failure leaves exactly one trace naming its class ─────────────────────────────
#
# THREE FAILURE CLASSES THAT READ IDENTICALLY IN THE LOG, which is to say not at all. A damaged
# file, a container that ran out of memory reading it, and a sandbox image predating the reader
# all produced the same nothing: the model got a `ModelRetry` sentence and the operator got no
# line. The tests below pin one event per class, and pin what each event may NOT carry — the
# file's name, its path, or `handle`, which holds the live supervisor bearer.


@dataclass
class _Reply:
    """The `ExecResult` shape `AttachmentReader.read` reads — stdout, stderr and an exit code."""

    stdout: str
    stderr: str
    exit: int


def _session_double(
    reply: _Reply | None = None, raises: Exception | None = None
) -> SandboxSession:
    """A session whose container call answers with `reply`, or raises.

    CARRIES `app_id`, unlike the argv double above, because every log site binds it. A double
    without one fails as an `AttributeError` that reads like a bug in the fix rather than a gap
    in the test.
    """

    class _Client:
        async def exec(self, _handle: Any, _argv: list[str], *, timeout_s: int) -> Any:
            if raises is not None:
                raise raises
            return reply

    return cast(
        SandboxSession,
        type("S", (), {"sandbox_client": _Client(), "handle": object(), "app_id": uuid.uuid4()})(),
    )


def _events(logs: list[Any], event: str) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == event]


async def test_a_clean_read_leaves_no_failure_trace() -> None:
    """The happy path stays silent: a reader that exits 0 with a manifest is not a failure."""
    session = _session_double(_Reply(stdout='{"ok": true}', stderr="", exit=0))

    with capture_logs() as logs:
        out = await AttachmentReader(session=session).read(".attachments/roster.xlsx")

    assert out == '{"ok": true}'
    for event in (
        "attachment_read_transport_failed",
        "attachment_read_nonzero_exit",
        "attachment_read_named_failure",
    ):
        assert _events(logs, event) == []


async def test_a_nonzero_exit_is_logged_once_with_its_code_and_capped_stderr() -> None:
    """★ THE STALE-IMAGE CLASS. A container whose image predates the reader answers exit 2 with
    `python3: can't open file …` on stderr, and the tool turns that into "the reader did not
    return a result for that file" — a sentence naming neither the cause nor anything an operator
    can act on. The log is where that distinction has to survive.

    Mutation check: delete the non-zero-exit block and this goes red.
    """
    stderr = "python3: can't open file '/usr/local/lib/bial/read_attachment.py': No such file\n"
    session = _session_double(_Reply(stdout="", stderr=stderr + "x" * 2_000, exit=2))

    with capture_logs() as logs:
        out = await AttachmentReader(session=session).read(".attachments/roster.xlsx")

    # PAIRED WITH THE RETURN VALUE, because the log assertions below pass vacuously if `read`
    # ever starts raising on a non-zero exit. The pass-through is unchanged and asserted here.
    assert out == ""
    (event,) = _events(logs, "attachment_read_nonzero_exit")
    assert event["exit_code"] == 2
    assert "read_attachment.py" in event["stderr"]
    assert len(event["stderr"]) <= 500
    # The suffix, never the name and never the path: a display name is citizen-supplied text.
    assert event["suffix"] == ".xlsx"
    assert "roster" not in repr(event)
    assert "handle" not in event


async def test_a_transport_failure_binds_explicit_fields_and_never_exc_info() -> None:
    """★ `exc_info=True` WOULD EMIT NOTHING, OR TOO MUCH. `main.py`'s processor chain carries no
    `format_exc_info` and no `dict_tracebacks`, so in production it renders the literal
    `"exc_info": true`; in dev, `ConsoleRenderer` with `rich` prints frame locals — which on this
    frame include the session, and the session holds the live supervisor bearer. The absence
    assertion is what pins that.
    """
    session = _session_double(raises=RuntimeError("connection reset by peer"))

    with capture_logs() as logs, pytest.raises(RuntimeError):
        await AttachmentReader(session=session).read(".attachments/roster.xlsx")

    (event,) = _events(logs, "attachment_read_transport_failed")
    assert event["error"] == "RuntimeError"
    assert "connection reset" in event["detail"]
    assert event["suffix"] == ".xlsx"
    assert "exc_info" not in event


async def test_a_named_reader_failure_is_logged_with_the_readers_own_code() -> None:
    """★ THE COMMONEST CASE, AND THE ONE THE OTHER TWO CANNOT SEE. The reader's contract is that
    it always exits 0 and prints one JSON object — INCLUDING for a corrupt, encrypted, oversized
    or timed-out file. So the two sites above catch only the stale-image class, and without this
    one the reader's own named failures are invisible to an operator forever.

    Mutation check: delete the `ok: false` log and this goes red.
    """
    body = json.dumps(
        {
            "ok": False,
            "file": "roster.xlsx",
            "error": {"code": "too_large", "message": "needs more memory", "next": "Split it."},
        }
    )
    reader = _Recorder(result=body)

    with capture_logs() as logs:
        out = await _tool(reader)(None, ".attachments/roster.xlsx")

    # The pass-through to the model is unchanged — a named failure is still an ANSWER.
    assert json.loads(out)["error"]["code"] == "too_large"
    (event,) = _events(logs, "attachment_read_named_failure")
    assert event["code"] == "too_large"
    assert event["suffix"] == ".xlsx"
