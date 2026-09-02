"""U4 — the native message store's round-trip property and its seams.

The store's contract: dump → externalize binaries → redact → JSONB → rehydrate → validate →
repair, and the ONLY differences between what went in and what comes out are redacted values
and externalized-then-rehydrated binaries. Equality is asserted on CANONICAL DUMPS
(`ModelMessagesTypeAdapter.dump_python(..., mode="json")`), not dataclass `==`: pydantic-ai
2.5.0 validates an image `BinaryContent` back as its `BinaryImage` subclass, so dataclass
equality is class-strict while the wire shape is identical — the dump IS the contract.

REASONING BLOCKS ARE THE ONE EXEMPTION to "redact every string in the tree", and they own the
other seam here too. A thinking part's `content` and `signature` reach the row VERBATIM,
because the provider verifies the signature against the content when the block is replayed on
the next turn and the masker is tuned to over-redact; nothing is lost by exempting them,
because a reasoning block is never projected, never framed and never sent to the browser. At
the load seam the rule inverts: a thinking part that has lost its signature is DROPPED rather
than replayed, because the library sends an unsigned block's content back as visible
assistant text.

Also pinned here, against the installed pydantic-ai (upgrade tripwires):
  * the CachePoint hazard — an unknown dict inside user content validates SILENTLY as
    `CachePoint`, which is exactly why the loader's marker swap must be exhaustive;
  * the Anthropic wire mapping of a marker followed by a user prompt — consecutive user-role
    messages in order (marker first; the API folds same-role neighbours);
  * the no-prompt-run gotcha — `agent.run(None, history-ending-in-marker)` adopts the marker
    as the prompt (so the turn engine must never start a run without a real user prompt);
  * the `<thinking>`-tag fallback — an unsigned reasoning block maps to a VISIBLE assistant
    text block, which is the failure the load-seam drop exists to prevent.
"""

from __future__ import annotations

import asyncio
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
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from src.core.redaction import redact_secrets
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.agent.mode_prompts import workspace_note
from src.services.messages import store
from src.services.messages.store import (
    ATTACHMENT_REF_KIND,
    AttachmentRehydrationError,
    SeqContentionError,
    UnattributedBinaryError,
    append_batch,
    attachment_rehydrator,
    dump_for_row,
    load_history,
    load_rows,
    repair_dangling_tool_calls,
)
from tests.factories import ConversationFactory, UserFactory

_PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"fake-png-body"


async def _no_refs(attachment_ids) -> dict[str, tuple[str, str]]:
    raise AssertionError(f"unexpected rehydration of {list(attachment_ids)!r}")


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
        kind=ChatKind.PLAN,
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
        kind=ChatKind.PLAN,
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=second,
        entry_kind=MessageEntryKind.STEP,
        kind=ChatKind.BUILD,
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
        kind=ChatKind.BUILD,
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
        kind=ChatKind.BUILD,
    )
    row = await db_session.get(Message, stored.id)
    assert row is not None
    assert password not in str(row.payload)


# --- reasoning: verbatim to the row, dropped when it cannot be replayed -------


def test_a_reasoning_block_reaches_the_row_verbatim_while_the_prose_beside_it_is_redacted():
    """The masker's ONE exemption, next to the thing that proves it is scoped rather than a hole.

    An agent writing code narrates in credential-shaped strings as a matter of course: the
    environment variable it must not put in the browser bundle, the token assignment it thought
    better of, the API key it moved to the server. `redact_secrets` matches on SHAPE and is
    deliberately tuned to over-redact — which costs nothing on a string headed for a screen and
    costs the whole turn here, because the provider verifies a reasoning block's signature
    against its content when the block is replayed and one rewritten character fails that check.
    So the block goes to the row byte-for-byte, while the sentence the agent actually SAID —
    carrying the very same token — is masked exactly as it always was.

    Mutation check: drop `content` from `_THINKING_VERBATIM`, or the `part_kind` guard that
    selects it, and the reasoning arrives as the masked string this test compares against.
    """
    reasoning = (
        "The template reads BIAL_DATA_BASE_URL at boot. Setting "
        "BIAL_APP_TOKEN=tok_9f2b1c4d7e in the env file would ship it to the browser, so I will "
        'keep apiKey = "sk_live_51H8xQ2abcdefghijkl" on the server instead.'
    )
    # A signature is an opaque blob and the masker matches on shape, so a `bial_…`-shaped run
    # inside one is a collision waiting to happen rather than a contrivance. It is written to be
    # a string the masker demonstrably rewrites because a signature it happened to leave alone
    # would prove nothing at all about the exemption.
    signature = "ErUBCkYIBBgCIkAxbial_9f2b1c4d7e0a1b2c3d4e/QQ=="
    spoken = 'I moved apiKey = "sk_live_51H8xQ2abcdefghijkl" to the server, so nothing leaks.'
    # The premise both assertions below rest on: these are strings the masker MANGLES. Without
    # this, "arrived intact" could hold for text the masker never had an opinion about.
    assert redact_secrets(reasoning) != reasoning
    assert redact_secrets(signature) != signature

    [dumped] = dump_for_row(
        [
            ModelResponse(
                parts=[
                    ThinkingPart(
                        content=reasoning,
                        signature=signature,
                        provider_name="anthropic",
                        provider_details={"note": "BIAL_APP_TOKEN=tok_9f2b1c4d7e"},
                    ),
                    TextPart(content=spoken),
                ]
            )
        ]
    )
    thinking, text = dumped["parts"]
    assert thinking["content"] == reasoning
    assert thinking["signature"] == signature
    # The exemption is by FIELD, not "any string under a thinking part": a sibling field on the
    # SAME part is masked like anything else, so a future user-facing field cannot inherit the
    # exemption merely by being added there.
    assert thinking["provider_details"] == {"note": "BIAL_APP_TOKEN=***"}
    # And the prose beside it is redacted as ever — while still reading as a sentence, which is
    # what says the row holds a masked line rather than nothing at all.
    assert "sk_live_51H8xQ2abcdefghijkl" not in text["content"]
    assert text["content"] == 'I moved apiKey = "***" to the server, so nothing leaks.'


async def test_a_provider_redacted_reasoning_block_survives_the_round_trip(db_session, thread):
    """`redacted_thinking` is reasoning the PROVIDER itself withheld: pydantic-ai maps that block
    to a `ThinkingPart` carrying the id `redacted_thinking`, NO content whatsoever, and the
    opaque blob in `signature` — which is the entire block. It has to come back exactly as it
    went in, because the next turn replays it and Anthropic refuses a tool call that is not
    preceded by the reasoning that led to it; losing the block wedges the turn that follows.

    Mutation check: key the load seam's drop on the block's content instead of its signature and
    this one — empty content, signature intact — is thrown away as if it were broken.
    """
    user, conversation = thread
    signature = "ErUBCkYIBBgCIkAxRedactedBlobPayload/9f2b1c4d=="
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="add a filter to the visitors table")]),
        ModelResponse(
            parts=[
                ThinkingPart(
                    id="redacted_thinking",
                    content="",
                    signature=signature,
                    provider_name="anthropic",
                ),
                ToolCallPart(
                    tool_name="read_file", args={"path": "app/page.tsx"}, tool_call_id="r"
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="read_file", content="export default 1", tool_call_id="r")
            ]
        ),
    ]
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=history,
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    loaded = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    (block,) = [
        part
        for message in loaded
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ThinkingPart)
    ]
    assert block.id == "redacted_thinking"
    assert block.content == ""
    assert block.signature == signature  # the blob IS the block; a rewrite is a rejected turn
    # …and the block still sits in front of the tool call it belongs to, which is the shape the
    # provider actually checks.
    assert _dump(loaded) == _dump(history)


async def test_reasoning_with_no_signature_is_dropped_at_the_load_seam(db_session, thread):
    """Fail closed: a reasoning block that has lost its signature can no longer be replayed AS
    reasoning, and the library's fallback is not to skip it — it sends the content back as an
    ordinary assistant TEXT block wrapped in `<thinking>` tags (pinned below). That would turn a
    block the citizen was never meant to see into part of the model's own visible transcript for
    the rest of the conversation, silently. Losing the reasoning and keeping the transcript is
    the strictly better trade, so the load seam drops it.

    Nothing should ever reach this — the persist seam writes what the library serialized and the
    masker leaves signatures alone — but a row written before that exemption, or a payload edited
    by hand, is exactly the case it exists for.
    """
    user, conversation = thread
    reasoning = "The citizen never sees this. BIAL_APP_TOKEN=tok_9f2b1c4d7e is server-only."
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="add a filter")]),
            ModelResponse(
                parts=[
                    ThinkingPart(content=reasoning),  # no signature — unreplayable
                    TextPart(content="I added the filter."),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    loaded = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    assert not [
        part
        for message in loaded
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ThinkingPart)
    ]
    assert reasoning not in str(_dump(loaded))
    # LIVENESS — the absence above must be the drop doing its job, not the whole history failing
    # to load. The prompt and the words that rode beside the reasoning both survive.
    (text,) = [
        part
        for message in loaded
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, TextPart)
    ]
    assert text.content == "I added the filter."
    assert any(
        isinstance(message, ModelRequest)
        and any(isinstance(part, UserPromptPart) for part in message.parts)
        for message in loaded
    )


async def test_a_response_emptied_by_the_reasoning_drop_leaves_nothing_on_the_wire():
    """The hazard the drop introduces, closed by the mapping rather than by us.

    A response whose ONLY part was an unsigned reasoning block comes out of the load seam with
    zero parts — and this file's other rule is that an empty-parts message is poison, because
    Anthropic rejects a message with an empty content block. The mapping omits such a message
    entirely instead of sending an empty one, so the drop cannot wedge a turn. If an upgrade
    changes that, the load seam has to start dropping the emptied response too.
    """
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key="offline"))
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="add a filter")]),
        ModelResponse(parts=[]),  # what the drop leaves behind
        ModelRequest(parts=[UserPromptPart(content="and a date column")]),
    ]
    params = model.customize_request_parameters(ModelRequestParameters())
    _, wire = await model._map_message(messages, params, {})  # noqa: SLF001 — pinned-version seam
    # Both real prompts ride; the emptied response is simply not there (they fold as neighbours).
    assert [message["role"] for message in wire] == ["user", "user"]
    assert "add a filter" in str(wire[0]) and "and a date column" in str(wire[1])


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
        kind=ChatKind.PLAN,
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


async def test_many_attachments_rehydrate_in_one_query_and_concurrent_reads(
    db_session, thread, fake_storage
):
    """The batch seam: a history carrying several attachments (and the SAME one twice) costs
    ONE attachment query, and the object-store reads overlap instead of queueing — a per-marker
    rehydrator turned an N-attachment history into N serialized round-trips."""
    from src.db.models.attachment import Attachment
    from src.services.storage.keys import owner_prefix

    user, conversation = thread
    ids = [f"att-batch-{n}" for n in range(4)]
    for attachment_id in ids:
        key = f"{owner_prefix(user.id)}{attachment_id}"
        db_session.add(
            Attachment(
                user_id=user.id,
                attachment_id=attachment_id,
                media_type="image/png",
                name=f"{attachment_id}.png",
                size=len(_PNG),
                storage_key=key,
            )
        )
        fake_storage.objects[key] = _PNG
    await db_session.flush()

    def _binary(attachment_id: str) -> BinaryContent:
        return BinaryContent(data=_PNG, media_type="image/png", identifier=attachment_id)

    history: list[ModelMessage] = [
        ModelRequest(
            parts=[UserPromptPart(content=["first two", _binary(ids[0]), _binary(ids[1])])]
        ),
        ModelResponse(parts=[TextPart(content="noted")]),
        # ids[0] again — the same file re-referenced must not be fetched twice.
        ModelRequest(
            parts=[
                UserPromptPart(content=["more", _binary(ids[2]), _binary(ids[3]), _binary(ids[0])])
            ]
        ),
    ]
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=history,
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )

    in_flight = {"now": 0, "peak": 0}
    real_get = fake_storage.get

    async def counting_get(key: str) -> bytes:
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        try:
            await asyncio.sleep(0)  # a real await point, so overlap is observable
            return await real_get(key)
        finally:
            in_flight["now"] -= 1

    fake_storage.get = counting_get
    loaded = await load_history(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        rehydrate=attachment_rehydrator(db_session, fake_storage, user.id),
    )
    assert _dump(loaded) == _dump(history)
    # 4 distinct files across 5 markers: deduped, and read concurrently rather than one by one.
    assert in_flight["peak"] > 1


async def test_a_row_from_a_future_schema_version_is_refused_not_guessed_at(db_session, thread):
    """The `schema_version` stamp exists so a reader can REFUSE what it does not understand.
    Without the check the promise was decorative: a future writer's payload would not fail
    today's rules, it would parse into a subtly wrong history and be handed to the model as
    fact. Refusal is loud and typed instead."""
    import sqlalchemy as sa

    from src.services.messages.store import SCHEMA_VERSION, UnsupportedSchemaVersionError

    user, conversation = thread
    stored = await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelResponse(parts=[TextPart(content="written by a newer server")])],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    await db_session.execute(
        sa.update(Message).where(Message.id == stored.id).values(schema_version=SCHEMA_VERSION + 1)
    )
    await db_session.flush()

    with pytest.raises(UnsupportedSchemaVersionError):
        await load_history(
            db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
        )


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
            kind=ChatKind.PLAN,
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
        kind=ChatKind.PLAN,
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


def test_non_adjacent_answer_is_relocated_adjacent_to_its_call():
    # A plan-options call, then an intervening user message (a mode-switch marker), then the
    # card's resolution — the return is NOT immediately after its call (Anthropic rejects a
    # tool_result with no adjacent tool_use). Repair must relocate it, not leave it stranded.
    history: list[ModelMessage] = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="present_plan_options", args={}, tool_call_id="p")]
        ),
        ModelRequest(parts=[UserPromptPart(content="[mode changed: plan → write]")]),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="present_plan_options", content="build", tool_call_id="p")
            ]
        ),
    ]
    repaired = repair_dangling_tool_calls(history)
    # The return now sits immediately after its call; the marker follows.
    assert isinstance(repaired[0], ModelResponse)
    assert isinstance(repaired[1], ModelRequest)
    (answer,) = repaired[1].parts
    assert isinstance(answer, ToolReturnPart)
    assert answer.tool_call_id == "p" and answer.content == "build"
    assert isinstance(repaired[2], ModelRequest)
    assert isinstance(repaired[2].parts[0], UserPromptPart)
    # Exactly one return for "p" survives anywhere in the history.
    returns = [
        part
        for message in repaired
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_call_id == "p"
    ]
    assert len(returns) == 1


def test_two_answers_for_one_call_are_deduped_to_one():
    # The Build-it vs turn-start race: a "refine" return lands adjacent, then a "build" return
    # is appended later for the SAME card. Two tool_results for one tool_use wedges the wire —
    # repair must keep exactly one.
    history: list[ModelMessage] = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="present_plan_options", args={}, tool_call_id="p")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="present_plan_options", content="refine", tool_call_id="p"
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="present_plan_options", content="build", tool_call_id="p")
            ]
        ),
    ]
    repaired = repair_dangling_tool_calls(history)
    returns = [
        part
        for message in repaired
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_call_id == "p"
    ]
    assert len(returns) == 1
    # The surviving return is adjacent to the call (immediately after the response).
    assert isinstance(repaired[1], ModelRequest)
    assert repaired[1].parts[0] is returns[0]


def _assert_anthropic_pairing(messages: list[ModelMessage]) -> None:
    """Anthropic's rule, as the suite's oracle: every part that rides the wire as a
    `tool_result` must have a `tool_use` (a `ToolCallPart`) EARLIER in the history."""
    seen_calls: set[str] = set()
    for message in messages:
        if isinstance(message, ModelResponse):
            seen_calls |= {
                part.tool_call_id for part in message.parts if isinstance(part, ToolCallPart)
            }
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                rides_as_tool_result = isinstance(part, ToolReturnPart) or (
                    isinstance(part, RetryPromptPart) and part.tool_name is not None
                )
                if rides_as_tool_result:
                    assert part.tool_call_id in seen_calls, (  # type: ignore[union-attr]
                        f"orphan tool answer {part.tool_call_id!r} would 400 on the wire"  # type: ignore[union-attr]
                    )


def test_orphaned_tool_result_is_dropped_on_load():
    """THE round-3 P0, as a mutation check: a stored `tool_result` whose `tool_use` was never
    persisted (the write-cursor overshoot skipped the run's first ModelResponse) 400s every
    subsequent turn, forever — the conversation is bricked. Repair must drop the orphan at
    load. Revert the fourth repair case and this goes red."""
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="build it")]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="ghost")]
        ),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    repaired = repair_dangling_tool_calls(history)
    _assert_anthropic_pairing(repaired)
    answers = [
        part
        for message in repaired
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert answers == []  # the orphan is gone
    # …and the rest of the story survives intact.
    assert any(
        isinstance(m, ModelRequest)
        and any(isinstance(p, UserPromptPart) and p.content == "build it" for p in m.parts)
        for m in repaired
    )
    assert any(isinstance(m, ModelResponse) for m in repaired)


def test_well_paired_history_passes_the_repair_untouched():
    # The no-false-positive guard: a healthy call/answer/text history comes through the repair
    # byte-identical — the orphan drop must never eat a paired answer.
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="read_file", args={"path": "a"}, tool_call_id="x")]
        ),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="1", tool_call_id="x")]),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    assert repair_dangling_tool_calls(history) == history


def test_orphan_sharing_a_request_with_a_valid_part_drops_only_the_orphan():
    history: list[ModelMessage] = [
        ModelResponse(parts=[ToolCallPart(tool_name="write_file", args={}, tool_call_id="real")]),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="real"),
                ToolReturnPart(tool_name="run_command", content="?", tool_call_id="ghost"),
            ]
        ),
    ]
    repaired = repair_dangling_tool_calls(history)
    _assert_anthropic_pairing(repaired)
    (request,) = [m for m in repaired if isinstance(m, ModelRequest)]
    assert [p.tool_call_id for p in request.parts if isinstance(p, ToolReturnPart)] == ["real"]


def test_orphan_as_a_requests_only_part_drops_the_whole_request():
    # Never emit an empty-parts ModelRequest — pydantic-ai's Anthropic mapping turns it into
    # an empty content block the API rejects.
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="build it")]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="ghost")]
        ),
    ]
    repaired = repair_dangling_tool_calls(history)
    assert all(message.parts for message in repaired)
    assert len([m for m in repaired if isinstance(m, ModelRequest)]) == 1


def test_orphaned_retry_prompt_is_dropped_the_same_way():
    # RetryPromptPart serializes as a tool_result too (when tool-scoped) — same orphan, same
    # drop.
    history: list[ModelMessage] = [
        ModelRequest(
            parts=[
                RetryPromptPart(
                    content="validation failed", tool_name="write_file", tool_call_id="ghost"
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    repaired = repair_dangling_tool_calls(history)
    _assert_anthropic_pairing(repaired)
    assert all(
        not isinstance(part, RetryPromptPart)
        for message in repaired
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


def test_tool_nameless_retry_prompt_is_never_dropped():
    # A RetryPromptPart with NO tool_name rides the wire as plain user text (its auto-generated
    # tool_call_id matches nothing by construction) — dropping it would eat a real prompt.
    history: list[ModelMessage] = [
        ModelRequest(parts=[RetryPromptPart(content="please fix the output format")]),
        ModelResponse(parts=[TextPart(content="fixed")]),
    ]
    assert repair_dangling_tool_calls(history) == history


def test_answer_whose_call_exists_later_is_relocated_not_dropped():
    # The order-aware distinction: the observed return-BEFORE-call row shape is pydantic-ai's
    # healthy append timing, not an orphan. The existing machinery relocates it; the orphan
    # drop must not touch it.
    history: list[ModelMessage] = [
        ModelRequest(
            parts=[ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="x")]
        ),
        ModelResponse(parts=[ToolCallPart(tool_name="write_file", args={}, tool_call_id="x")]),
    ]
    repaired = repair_dangling_tool_calls(history)
    _assert_anthropic_pairing(repaired)
    returns = [
        part
        for message in repaired
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_call_id == "x"
    ]
    assert len(returns) == 1  # relocated, never dropped


async def test_orphaned_tool_result_in_stored_history_loads_unbricked(db_session, thread):
    # Through the DB: the real bricked-conversation shape — an orphaned tool_result row with
    # no persisted tool_use anywhere — must load wire-valid, with the orphan gone.
    user, conversation = thread
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="add a filter")]),
            ModelRequest(
                parts=[ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="ghost")]
            ),
            ModelResponse(parts=[TextPart(content="I added the filter.")]),
        ],
        entry_kind=MessageEntryKind.STEP,
        kind=ChatKind.BUILD,
    )
    loaded = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    _assert_anthropic_pairing(loaded)
    assert all(
        not (isinstance(part, ToolReturnPart) and part.tool_call_id == "ghost")
        for message in loaded
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


async def test_read_tool_return_survives_persist_and_reload(db_session, thread):
    # Regression for the responses-only persistence bug: a successful read_file result must
    # NOT be dropped and papered over with a synthesized "interrupted" on reload.
    user, conversation = thread
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="read_file", args={"path": "a.tsx"}, tool_call_id="r")
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="read_file", content="export default 1", tool_call_id="r"
                    )
                ]
            ),
            ModelResponse(parts=[TextPart(content="The file exports a default.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    loaded = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    (answer,) = [
        part
        for message in loaded
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_call_id == "r"
    ]
    assert answer.content == "export default 1"  # the real result, not "interrupted"
    assert "interrupted" not in str(answer.content).lower()


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
        kind=ChatKind.BUILD,
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
        kind=ChatKind.PLAN,
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
        kind=ChatKind.PLAN,
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
        kind=ChatKind.PLAN,
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
            kind=ChatKind.PLAN,
        )
    rows = await load_rows(db_session, user_id=user.id, conversation_id=conversation.id)
    assert [row.seq for row in rows] == [0]  # the loser wrote nothing


# --- mode-switch markers ------------------------------------------------------


async def test_a_hidden_row_is_hidden_by_the_row_predicate_not_by_its_payload(db_session, thread):
    """The property the retired mode-switch marker used to demonstrate here, kept.

    Hiddenness lives on the ROW (`visibility`), never in the payload — so the model sees a
    hidden row like any other message while the projection filters it with a WHERE clause. The
    marker is gone with the switch that wrote it (its inertness guard is
    `tests/api/v1/conversations/test_mode_switch.py`); a hidden system row makes the same point
    and is still written today."""
    user, conversation = thread
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content="hello")])],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content="a private aside")])],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
        visibility=MessageVisibility.HIDDEN,
    )
    # The MODEL sees it (load_history ignores visibility)…
    history = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    assert "a private aside" in str(_dump(history))
    # …the UI read does not, unless it asks for hidden rows explicitly.
    visible = await load_rows(db_session, user_id=user.id, conversation_id=conversation.id)
    assert all(row.visibility is MessageVisibility.VISIBLE for row in visible)
    everything = await load_rows(
        db_session, user_id=user.id, conversation_id=conversation.id, include_hidden=True
    )
    assert len(everything) == len(visible) + 1


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


async def test_an_injected_note_then_the_prompt_maps_to_ordered_user_messages_on_the_wire():
    """The wire contract every EPHEMERAL INJECTED NOTE rests on, re-pointed from the retired
    mode-switch marker to the note that actually rides today.

    The turn engine appends the workspace note to `message_history` as a `user`-role request and
    then passes this turn's prompt separately, so two consecutive user-role messages reach the
    wire. The Anthropic API accepts and folds them, IN ORDER — which is what makes the note read
    as context for the prompt that follows rather than as a message the user sent afterwards.
    The marker is gone; this contract is not, because the note uses it."""
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key="offline"))
    note = workspace_note(serving=True, still_the_template=False)
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="build me an app")]),
        ModelResponse(parts=[TextPart(content="done")]),
        ModelRequest(parts=[UserPromptPart(content=note)]),
        ModelRequest(parts=[UserPromptPart(content="add a dashboard")]),
    ]
    params = model.customize_request_parameters(ModelRequestParameters())
    _, wire = await model._map_message(messages, params, {})  # noqa: SLF001 — pinned-version seam
    roles = [message["role"] for message in wire]
    assert roles == ["user", "assistant", "user", "user"]
    assert "checked this app's workspace just now" in str(wire[2])
    assert "add a dashboard" in str(wire[3])


async def test_no_prompt_run_adopts_a_trailing_injected_note_as_the_prompt():
    """THE GOTCHA (pinned), re-pointed from the retired marker to the note that rides today.

    Running with no user prompt while an injected note is the last history message makes the
    model answer the NOTE. The turn engine must always pass a real user prompt; this is the
    tripwire that keeps that rule honest, and it matters more now than it did with the marker,
    because the workspace note is appended on EVERY turn that pinned a workspace rather than
    only at a switch."""
    seen: list[list[ModelMessage]] = []

    def capture(messages, info):
        seen.append(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    agent = Agent(FunctionModel(capture))
    note = ModelRequest(
        parts=[UserPromptPart(content=workspace_note(serving=False, still_the_template=None))]
    )
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="real question")]),
        ModelResponse(parts=[TextPart(content="answer")]),
        note,
    ]
    result = await agent.run(None, message_history=history)
    assert result is not None
    last = seen[0][-1]
    assert isinstance(last, ModelRequest)
    (part,) = last.parts
    assert isinstance(part, UserPromptPart)
    # The note IS the effective prompt — exactly what a caller must never let happen.
    assert "checked this app's workspace just now" in str(part.content)


async def test_unsigned_reasoning_maps_to_a_visible_assistant_text_block():
    """WHY the load seam drops an unsigned reasoning block rather than keeping it, pinned against
    the installed pydantic-ai.

    A `ThinkingPart` rides the wire as a `thinking` block only while it still carries the
    signature its provider issued. Without one the mapping takes the other branch and emits the
    reasoning CONTENT as an ordinary assistant TEXT block wrapped in `<thinking>` tags — private
    reasoning promoted into the model's own visible transcript, silently and permanently. If an
    upgrade changes this branch, `_without_broken_reasoning` can be revisited; until then the
    drop is the only safe reading.
    """
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    reasoning = "Private: the token lives only in the server env."
    model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key="offline"))
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="add a filter")]),
        ModelResponse(
            parts=[ThinkingPart(content=reasoning), TextPart(content="I added the filter.")]
        ),
    ]
    params = model.customize_request_parameters(ModelRequestParameters())
    _, wire = await model._map_message(messages, params, {})  # noqa: SLF001 — pinned-version seam
    assistant = wire[1]
    assert assistant["role"] == "assistant"
    # Stringified rather than indexed, because a mapped block is a typed union of two dozen
    # shapes. What matters is the pair: the reasoning is on the wire, wrapped in the tags, as a
    # TEXT block — indistinguishable from something the model said out loud — and no `thinking`
    # block was emitted for it at all.
    rendered = str(assistant["content"])
    assert reasoning in rendered and "<thinking>" in rendered
    assert "'type': 'text'" in rendered
    assert "'type': 'thinking'" not in rendered


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
