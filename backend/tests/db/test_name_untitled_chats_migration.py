"""Revision 0051 names every untitled chat after its earliest visible user message with words in
it, by the send route's rule, and touches nothing else.

DEFAULT LANE: the revision is data only, so its real `upgrade()` runs inside the per-test
transaction against seeded rows and is rolled back with it — no chain walk, no schema change.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.services.messages.store import dump_for_row
from tests.api.v1.conversations.test_chat_naming import NAMES
from tests.factories import ConversationFactory, MessageFactory, UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_PATH = _BACKEND_ROOT / "alembic" / "versions" / "2026_10_01_0051_name_untitled_chats.py"
_LONG_AGO = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)


def _migration() -> ModuleType:
    """Imported by path (the versions directory is not a package), so the test runs the
    revision's own code rather than a copy of it."""
    spec = importlib.util.spec_from_file_location("migration_0051", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REVISION = _migration()


def _prompt(content: Any) -> list[ModelMessage]:
    return [ModelRequest(parts=[UserPromptPart(content=content)])]


def _reply(text: str) -> list[ModelMessage]:
    return [ModelResponse(parts=[TextPart(content=text)])]


async def _chat(db_session, user, *rows: tuple[list[ModelMessage], dict[str, Any]], **fields):
    conv = await ConversationFactory.create(db_session, user.id, id=uuid.uuid7(), **fields)
    for seq, (messages, overrides) in enumerate(rows):
        await MessageFactory.create(
            db_session, user.id, conv.id, seq=seq, payload=dump_for_row(messages), **overrides
        )
    return conv


async def _upgrade(db_session) -> None:
    connection = await db_session.connection()

    def _run(sync_connection) -> None:
        with Operations.context(MigrationContext.configure(sync_connection)):
            _REVISION.upgrade()

    await connection.run_sync(_run)


# --- the rule and the payload walk -----------------------------------------------------------


def test_the_revision_id_fits_the_version_column() -> None:
    """Against a literal, never the string that produced it — that passes at any length."""
    assert _REVISION.revision == "0051_name_untitled_chats"
    assert len("0051_name_untitled_chats") <= 32
    assert _REVISION.down_revision == "0050_user_has_signed_in"


@pytest.mark.parametrize(("text", "expected"), NAMES)
def test_the_revision_names_a_chat_exactly_as_a_send_does(text: str, expected: str | None) -> None:
    assert _REVISION.derive_title(text) == expected


def test_a_text_only_prompt_reads_as_its_text() -> None:
    assert _REVISION.typed_text(dump_for_row(_prompt("just words"))) == "just words"


def test_attachment_markup_and_markers_are_not_typed_text() -> None:
    payload = dump_for_row(
        _prompt(
            [
                '<attachment name="gate.png"/>',
                BinaryContent(data=b"\x89PNG", media_type="image/png", identifier="att_gate"),
                '<attachment name="notes.txt" type="text/plain">gate 4 is closed</attachment>',
                "What does the sign say?",
            ]
        ),
        file_attachment_ids=["att_roster"],
    )

    assert _REVISION.typed_text(payload) == "What does the sign say?"


def test_a_payload_handed_back_as_json_text_is_read_the_same() -> None:
    payload = dump_for_row(_prompt("words on the wire"))

    assert _REVISION.typed_text(json.dumps(payload)) == "words on the wire"


def test_a_reply_carries_no_typed_text() -> None:
    assert _REVISION.typed_text(dump_for_row(_reply("assistant words"))) == ""


# --- the revision against real rows ---------------------------------------------------------


async def test_untitled_chats_are_named_and_nothing_else_moves(db_session, monkeypatch) -> None:
    """Two to a batch, so the walk crosses several batches and a chat it skips (no words) sits
    between two it names — a skip must not stall the walk."""
    monkeypatch.setattr(_REVISION, "_BATCH", 2)
    visible: dict[str, Any] = {}
    user = await UserFactory.create(db_session)

    plain = await _chat(db_session, user, (_prompt("Log a runway\ninspection"), visible))
    silent = await _chat(db_session, user, (_reply("nothing was asked"), visible))
    attachments_first = await _chat(
        db_session,
        user,
        (
            _prompt(
                [
                    '<attachment name="gate.png"/>',
                    BinaryContent(data=b"\x89PNG", media_type="image/png", identifier="att_g"),
                    "",
                ]
            ),
            visible,
        ),
        (_reply("a gate sign"), visible),
        (_prompt("What is on this gate sign?"), visible),
    )
    platform_first = await _chat(
        db_session,
        user,
        (
            _prompt("The build is not green yet"),
            {"entry_kind": MessageEntryKind.STEP, "visibility": MessageVisibility.HIDDEN},
        ),
        (
            _prompt("The app was changed in another chat."),
            {"entry_kind": MessageEntryKind.SYSTEM_EVENT},
        ),
        (_prompt("Add a column for the shift"), visible),
    )
    already_named = await _chat(
        db_session, user, (_prompt("something else entirely"), visible), title="Kept name"
    )
    generic = await _chat(
        db_session, user, (_prompt("Summarise the belt fault log"), visible), kind=ChatKind.GENERIC
    )
    chats = [plain, silent, attachments_first, platform_first, already_named, generic]
    ids = [chat.id for chat in chats]
    # Seeding moved every `updated_at` through the message trigger; pin it somewhere the
    # revision would visibly disturb.
    await db_session.execute(
        sa.update(Conversation).where(Conversation.id.in_(ids)).values(updated_at=_LONG_AGO)
    )

    await _upgrade(db_session)

    after = {
        row.id: (row.title, row.updated_at)
        for row in (
            await db_session.execute(
                sa.select(Conversation.id, Conversation.title, Conversation.updated_at).where(
                    Conversation.id.in_(ids)
                )
            )
        ).all()
    }
    assert {chat_id: title for chat_id, (title, _) in after.items()} == {
        plain.id: "Log a runway inspection",
        silent.id: None,
        attachments_first.id: "What is on this gate sign?",
        platform_first.id: "Add a column for the shift",
        already_named.id: "Kept name",
        generic.id: "Summarise the belt fault log",
    }
    assert {updated_at for _, updated_at in after.values()} == {_LONG_AGO}
