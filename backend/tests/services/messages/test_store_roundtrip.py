"""U4 — the native message store's round-trip property and its seams.

The store's contract: dump → externalize binaries → redact → JSONB → rehydrate → validate →
repair, and the ONLY differences between what went in and what comes out are redacted values
and externalized-then-rehydrated binaries. Equality is asserted on CANONICAL DUMPS
(`ModelMessagesTypeAdapter.dump_python(..., mode="json")`), not dataclass `==`: pydantic-ai
2.5.0 validates an image `BinaryContent` back as its `BinaryImage` subclass, so dataclass
equality is class-strict while the wire shape is identical — the dump IS the contract.

Also pinned here, against the installed pydantic-ai (upgrade tripwires):
  * the CachePoint hazard — an unknown dict inside user content validates SILENTLY as
    `CachePoint`, which is exactly why the loader's marker swap must be exhaustive;
  * the Anthropic wire mapping of a marker followed by a user prompt — consecutive user-role
    messages in order (marker first; the API folds same-role neighbours);
  * the no-prompt-run gotcha — `agent.run(None, history-ending-in-marker)` adopts the marker
    as the prompt (so the turn engine must never start a run without a real user prompt).
"""

from __future__ import annotations

import base64

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    CachePoint,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from src.db.models.conversation import ConversationMode
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.messages import store
from src.services.messages.store import (
    ATTACHMENT_REF_KIND,
    AttachmentRehydrationError,
    SeqContentionError,
    UnattributedBinaryError,
    append_batch,
    append_mode_switch_marker,
    attachment_rehydrator,
    dump_for_row,
    load_history,
    load_rows,
    mode_switch_marker_text,
    repair_dangling_tool_calls,
)
from tests.factories import ConversationFactory, UserFactory

_PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"fake-png-body"


async def _no_refs(attachment_id: str) -> tuple[str, str]:
    raise AssertionError(f"unexpected rehydration of {attachment_id!r}")


def _dump(messages: list[ModelMessage]) -> list[object]:
    return ModelMessagesTypeAdapter.dump_python(messages, mode="json")


@pytest.fixture
async def thread(db_session):
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(db_session, user.id)
    return user, conversation


# --- the round-trip property --------------------------------------------------


async def test_multi_turn_history_round_trips_identically(db_session, thread):
    user, conversation = thread
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="add a visitors dashboard")]),
        ModelResponse(
            parts=[
                TextPart(content="Reading the app first."),
                ToolCallPart(
                    tool_name="read_file", args={"path": "app/page.tsx"}, tool_call_id="a"
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="read_file", content="export default …", tool_call_id="a")
            ]
        ),
        ModelResponse(parts=[TextPart(content="Done — the dashboard is in.")]),
    ]
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=history,
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.PLAN,
    )
    loaded = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    assert _dump(loaded) == _dump(history)


async def test_empty_conversation_loads_empty(db_session, thread):
    user, conversation = thread
    assert (
        await load_history(
            db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
        )
        == []
    )


async def test_batches_concatenate_in_seq_order(db_session, thread):
    user, conversation = thread
    first: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="one")])]
    second: list[ModelMessage] = [ModelResponse(parts=[TextPart(content="two")])]
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=first,
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.ASK,
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=second,
        entry_kind=MessageEntryKind.STEP,
        mode=ConversationMode.WRITE,
    )
    loaded = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    assert _dump(loaded) == _dump(first + second)


# --- redaction at the persist seam --------------------------------------------


async def test_dsn_in_tool_return_is_stored_redacted(db_session, thread):
    user, conversation = thread
    password = "sup3rs3cretpw"  # noqa: S105 — the value under test
    dsn = f"postgresql://appuser:{password}@dbhost:5432/bialapp"
    history: list[ModelMessage] = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="run_command", args={"argv": ["env"]}, tool_call_id="t")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="run_command", content=f"BIAL_DATABASE_URL={dsn}", tool_call_id="t"
                )
            ]
        ),
    ]
    stored = await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=history,
        entry_kind=MessageEntryKind.STEP,
        mode=ConversationMode.WRITE,
    )
    row = await db_session.get(Message, stored.id)
    assert row is not None
    flat = str(row.payload)
    # The parsed password never appears ANYWHERE in the row (security.md: whole value AND
    # password sub-token are both registered redactions).
    assert password not in flat
    assert password not in str(row.meta)


async def test_dsn_in_tool_args_is_stored_redacted(db_session, thread):
    user, conversation = thread
    password = "arg-secret-pw"  # noqa: S105
    history: list[ModelMessage] = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="run_command",
                    args={"argv": ["psql", f"postgresql://u:{password}@h/db"]},
                    tool_call_id="t",
                )
            ]
        )
    ]
    stored = await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=history,
        entry_kind=MessageEntryKind.STEP,
        mode=ConversationMode.WRITE,
    )
    row = await db_session.get(Message, stored.id)
    assert row is not None
    assert password not in str(row.payload)


# --- attachment externalization + rehydration ---------------------------------


async def test_binary_stores_reference_not_bytes_and_rehydrates(db_session, thread, fake_storage):
    from src.db.models.attachment import Attachment
    from src.services.storage.keys import owner_prefix

    user, conversation = thread
    attachment_id = "att-token-1"
    key = f"{owner_prefix(user.id)}{attachment_id}"
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id=attachment_id,
            media_type="image/png",
            name="diagram.png",
            size=len(_PNG),
            storage_key=key,
        )
    )
    await db_session.flush()
    fake_storage.objects[key] = _PNG

    history: list[ModelMessage] = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=[
                        "use this diagram",
                        BinaryContent(data=_PNG, media_type="image/png", identifier=attachment_id),
                    ]
                )
            ]
        )
    ]
    stored = await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=history,
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.PLAN,
    )
    row = await db_session.get(Message, stored.id)
    assert row is not None
    flat = str(row.payload)
    # Bytes never land in the row — neither raw nor base64 — only the reference marker.
    assert base64.b64encode(_PNG).decode("ascii") not in flat
    assert ATTACHMENT_REF_KIND in flat and attachment_id in flat

    loaded = await load_history(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        rehydrate=attachment_rehydrator(db_session, fake_storage, user.id),
    )
    assert _dump(loaded) == _dump(history)  # rehydration restores the identical wire shape


async def test_binary_without_identifier_is_a_producer_bug(db_session, thread):
    user, conversation = thread
    with pytest.raises(UnattributedBinaryError):
        await append_batch(
            db_session,
            user_id=user.id,
            conversation_id=conversation.id,
            messages=[
                ModelRequest(
                    parts=[
                        UserPromptPart(content=[BinaryContent(data=_PNG, media_type="image/png")])
                    ]
                )
            ],
            entry_kind=MessageEntryKind.TURN,
            mode=ConversationMode.PLAN,
        )


async def test_missing_attachment_row_fails_rehydration_loudly(db_session, thread, fake_storage):
    user, conversation = thread
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=[
                            BinaryContent(data=_PNG, media_type="image/png", identifier="gone")
                        ]
                    )
                ]
            )
        ],
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.PLAN,
    )
    with pytest.raises(AttachmentRehydrationError):
        await load_history(
            db_session,
            user_id=user.id,
            conversation_id=conversation.id,
            rehydrate=attachment_rehydrator(db_session, fake_storage, user.id),
        )


# --- dangling tool-call repair ------------------------------------------------


def test_dangling_tool_call_gets_synthesized_interrupted_result():
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="build it")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="write_file", args={"path": "a.tsx"}, tool_call_id="x")]
        ),
    ]
    repaired = repair_dangling_tool_calls(history)
    assert len(repaired) == 3
    stitched = repaired[-1]
    assert isinstance(stitched, ModelRequest)
    (part,) = stitched.parts
    assert isinstance(part, ToolReturnPart)
    assert part.tool_call_id == "x"
    assert "interrupted" in str(part.content).lower()


def test_answered_tool_call_is_left_alone():
    history: list[ModelMessage] = [
        ModelResponse(parts=[ToolCallPart(tool_name="write_file", args={}, tool_call_id="x")]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="x")]
        ),
    ]
    assert repair_dangling_tool_calls(history) == history


async def test_dangling_call_in_stored_history_loads_repaired(db_session, thread):
    user, conversation = thread
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[ToolCallPart(tool_name="run_command", args={}, tool_call_id="dead")]
            )
        ],
        entry_kind=MessageEntryKind.STEP,
        mode=ConversationMode.WRITE,
    )
    loaded = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    returns = [
        part
        for message in loaded
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert [ret.tool_call_id for ret in returns] == ["dead"]


# --- seq allocation: gap-free two-writer discipline ---------------------------


async def test_stale_seq_retries_without_silent_loss(db_session, thread, monkeypatch):
    user, conversation = thread
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content="first")])],
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.PLAN,
    )

    real_head = store._head_seq
    stale_served = False

    async def stale_once(db, conversation_id):
        nonlocal stale_served
        if not stale_served:
            stale_served = True
            return -1  # a stale read: claims the conversation is empty → picks seq 0 → collides
        return await real_head(db, conversation_id)

    monkeypatch.setattr(store, "_head_seq", stale_once)
    stored = await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content="second")])],
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.PLAN,
    )
    assert stored.seq == 1  # gap-free: retried onto the real head, nothing lost
    rows = await load_rows(db_session, user_id=user.id, conversation_id=conversation.id)
    assert [row.seq for row in rows] == [0, 1]


async def test_seq_contention_budget_exhausted_raises_with_nothing_written(
    db_session, thread, monkeypatch
):
    user, conversation = thread
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content="first")])],
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.PLAN,
    )

    async def always_stale(db, conversation_id):
        return -1

    monkeypatch.setattr(store, "_head_seq", always_stale)
    with pytest.raises(SeqContentionError):
        await append_batch(
            db_session,
            user_id=user.id,
            conversation_id=conversation.id,
            messages=[ModelRequest(parts=[UserPromptPart(content="doomed")])],
            entry_kind=MessageEntryKind.TURN,
            mode=ConversationMode.PLAN,
        )
    rows = await load_rows(db_session, user_id=user.id, conversation_id=conversation.id)
    assert [row.seq for row in rows] == [0]  # the loser wrote nothing


# --- mode-switch markers ------------------------------------------------------


async def test_marker_round_trips_hidden_from_projection_visible_to_model(db_session, thread):
    user, conversation = thread
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content="hello")])],
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.ASK,
    )
    await append_mode_switch_marker(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        old_mode=ConversationMode.ASK,
        new_mode=ConversationMode.WRITE,
    )
    # The MODEL sees the marker (load_history includes hidden rows)…
    history = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    assert "[mode changed: ask → write]" in str(_dump(history))
    # …the UI projection does not (hiddenness is the ROW predicate, not a payload property).
    visible = await load_rows(db_session, user_id=user.id, conversation_id=conversation.id)
    assert all(row.entry_kind is not MessageEntryKind.MODE_SWITCH for row in visible)
    everything = await load_rows(
        db_session, user_id=user.id, conversation_id=conversation.id, include_hidden=True
    )
    marker_rows = [row for row in everything if row.entry_kind is MessageEntryKind.MODE_SWITCH]
    assert len(marker_rows) == 1
    assert marker_rows[0].visibility is MessageVisibility.HIDDEN


def test_marker_text_is_direction_aware():
    up = mode_switch_marker_text(ConversationMode.PLAN, ConversationMode.WRITE)
    assert up == "[mode changed: plan → write]"  # upgrades stay minimal
    down = mode_switch_marker_text(ConversationMode.WRITE, ConversationMode.ASK)
    # The downgrade names the capability change factually (the one sanctioned exception to
    # "no absent-tool prose" — the contradiction exists exactly here).
    assert "build tools" in down and "not available" in down.lower()
    assert "switch back to Write mode" in down


# --- pinned upstream behaviors (pydantic-ai 2.5.0 tripwires) ------------------


def test_unknown_content_dict_silently_becomes_cachepoint():
    """THE HAZARD the loader's exhaustive swap defends against: a KIND-LESS unknown dict
    inside user content does NOT raise — it validates as `CachePoint` (all-defaulted union
    member; extra keys dropped) and the content silently vanishes. If an upstream upgrade
    changes this, re-audit `_swap_refs` and `_assert_no_marker_left` before relaxing
    anything."""
    raw = [
        {
            "parts": [
                {
                    "content": ["x", {"anything": "at all"}],
                    "part_kind": "user-prompt",
                    "timestamp": "2026-07-23T00:00:00Z",
                }
            ],
            "kind": "request",
        }
    ]
    validated = ModelMessagesTypeAdapter.validate_python(raw)
    request = validated[0]
    assert isinstance(request, ModelRequest)
    (prompt,) = request.parts
    assert isinstance(prompt, UserPromptPart)
    assert isinstance(prompt.content, list)
    assert isinstance(prompt.content[1], CachePoint)  # the dict VANISHED, silently


def test_unswapped_ref_marker_fails_loud_not_silent():
    """OUR marker deliberately carries a `kind` key with a non-CachePoint literal, so an
    unswapped marker RAISES at validation instead of vanishing into a cache hint — the
    fail-loud floor under `_assert_no_marker_left`'s fail-first check."""
    raw = [
        {
            "parts": [
                {
                    "content": ["x", {"kind": ATTACHMENT_REF_KIND, "attachment_id": "a1"}],
                    "part_kind": "user-prompt",
                    "timestamp": "2026-07-23T00:00:00Z",
                }
            ],
            "kind": "request",
        }
    ]
    with pytest.raises(Exception, match="cache-point|validation"):
        ModelMessagesTypeAdapter.validate_python(raw)


async def test_marker_then_prompt_maps_to_ordered_user_messages_on_the_anthropic_wire():
    """The wire contract the marker design rests on: a marker request followed by the next
    user request maps to consecutive user-role messages IN ORDER (marker first). The Anthropic
    API accepts and folds consecutive same-role messages, so the model reads the marker as
    part of the same turn, positioned exactly where the switch happened."""
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key="offline"))
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="build me an app")]),
        ModelResponse(parts=[TextPart(content="done")]),
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=mode_switch_marker_text(ConversationMode.PLAN, ConversationMode.WRITE)
                )
            ]
        ),
        ModelRequest(parts=[UserPromptPart(content="add a dashboard")]),
    ]
    params = model.customize_request_parameters(ModelRequestParameters())
    _, wire = await model._map_message(messages, params, {})  # noqa: SLF001 — pinned-version seam
    roles = [message["role"] for message in wire]
    assert roles == ["user", "assistant", "user", "user"]
    assert "mode changed" in str(wire[2])
    assert "add a dashboard" in str(wire[3])


async def test_no_prompt_run_adopts_trailing_marker_as_prompt():
    """THE GOTCHA (pinned): running with no user prompt while a marker is the last history
    message makes the model answer the MARKER. The turn engine must always pass a real user
    prompt; this test is the tripwire that keeps that rule honest."""
    seen: list[list[ModelMessage]] = []

    def capture(messages, info):
        seen.append(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    agent = Agent(FunctionModel(capture))
    marker = ModelRequest(
        parts=[
            UserPromptPart(
                content=mode_switch_marker_text(ConversationMode.ASK, ConversationMode.WRITE)
            )
        ]
    )
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="real question")]),
        ModelResponse(parts=[TextPart(content="answer")]),
        marker,
    ]
    result = await agent.run(None, message_history=history)
    assert result is not None
    last = seen[0][-1]
    assert isinstance(last, ModelRequest)
    (part,) = last.parts
    assert isinstance(part, UserPromptPart)
    # The marker IS the effective prompt — exactly what a caller must never let happen.
    assert "mode changed" in str(part.content)


def test_dump_strips_run_instructions_from_the_payload():
    """U9/D4 — pydantic-ai stamps the run's composed instructions onto every ModelRequest
    it returns; the dump seam normalizes them to None so no persisted row ever fossilizes
    a prompt (history loads from the DB, each run re-injects its own composition)."""
    request = ModelRequest(
        parts=[UserPromptPart(content="hi")], instructions="SECRET-INSTRUCTIONS-MARKER"
    )
    [dumped] = dump_for_row([request])
    assert dumped["instructions"] is None
    assert "SECRET-INSTRUCTIONS-MARKER" not in str(dumped)
