"""The KD-13 run-context provider's prompt assembly (`deps._live_session_spec`) — the ONE place
the multimodal build prompt's shape and ORDER are decided (R3).

Worth its own file because the ordering is a deliberate decision with no local signal: the
harness passes `BuildSpec.prompt` through to `agent.iter` verbatim, so a flipped order here
reaches the model silently — every other test still passes, and the only symptom is worse
vision grounding on a real build nobody re-runs. These tests are that signal.
"""

from __future__ import annotations

import uuid

from pydantic_ai import BinaryContent

from src.api.v1.build_sessions.deps import _live_session_spec
from src.services.build_sessions.manager import BuildSession, get_session_manager
from tests.fakes import _fake_handle

PNG = bytes([0x89, 0x50, 0x4E, 0x47]) + b"\x00 pretend png"
FENCED_XLSX = '<attachment name="sales.xlsx" type="excel">\n| Q1 | 100 |\n</attachment>'


def _register(attachments: list[str | BinaryContent]) -> uuid.UUID:
    """Put a live session in the singleton the provider reads, and hand back its id."""
    session = BuildSession(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        prompt="build me a dashboard",
        lock_token="tok",
        handle=_fake_handle("sbx-run-context-spec"),
        attachments=attachments,
    )
    get_session_manager()._sessions[session.session_id] = session
    return session.session_id


async def test_no_attachments_keeps_a_bare_string_prompt() -> None:
    """The pre-R3 path stays byte-identical — not a one-element list that merely renders the
    same. A `str` and a `[str]` are different inputs to pydantic-ai, and every build without an
    attachment (the overwhelming majority) takes this branch."""
    spec = await _live_session_spec(_register([]))
    assert spec.prompt == "build me a dashboard"
    assert isinstance(spec.prompt, str)


async def test_attachments_come_before_the_instruction() -> None:
    """R3 ordering: attachments FIRST, instruction LAST.

    This is Anthropic's documented vision ordering and matches the portal's own `buildContent`
    ("text after files"), so the build path and the chat relay ground the model identically. It
    also reads the way the user means it: the material, then the ask about that material.
    """
    session_id = _register([BinaryContent(data=PNG, media_type="image/png"), FENCED_XLSX])
    spec = await _live_session_spec(session_id)

    assert isinstance(spec.prompt, list)
    # The instruction is LAST — the assertion that fails if the order is ever flipped back.
    assert spec.prompt[-1] == "build me a dashboard"
    # ...and every attachment precedes it, in the order they were materialized.
    assert isinstance(spec.prompt[0], BinaryContent)
    assert spec.prompt[0].data == PNG
    assert spec.prompt[1] == FENCED_XLSX
    assert len(spec.prompt) == 3


async def test_every_materialized_attachment_reaches_the_spec() -> None:
    """Nothing is dropped in assembly — the silent-drop bug R3 exists to kill, re-checked at the
    last seam before the model."""
    attachments: list[str | BinaryContent] = [
        BinaryContent(data=PNG, media_type="image/png"),
        BinaryContent(data=b"%PDF-1.4 pretend", media_type="application/pdf"),
        FENCED_XLSX,
    ]
    spec = await _live_session_spec(_register(attachments))
    assert isinstance(spec.prompt, list)
    assert list(spec.prompt[:-1]) == attachments
