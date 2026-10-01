"""Name every untitled chat after its first message, the way a send names a new one

Revision ID: 0051_name_untitled_chats
Revises: 0050_user_has_signed_in
Create Date: 2026-10-01

Data only. The name comes from the earliest visible user message that carries text — attachment
markup left out, the same reading the transcript gives that bubble — and follows the send route's
rule: whitespace and control runs collapse to one space, the ends are trimmed, and anything past
40 code points is cut and marked with "…". A chat with no such message stays untitled. Generic
chats are named too. `updated_at` is not touched, so no chat changes place in a list.

The rule and the payload walk are copied here rather than imported, so this revision does what it
did when it was written whatever the application code later becomes.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0051_name_untitled_chats"
down_revision: str | None = "0050_user_has_signed_in"
branch_labels: str | None = None
depends_on: str | None = None

_BATCH = 500
_SPACE_IN_A_NAME = frozenset({"Zs", "Cc", "Zl", "Zp"})
_NAME_CODE_POINTS = 40

_UNTITLED = sa.text(
    "SELECT id FROM conversations WHERE title IS NULL AND id > :after ORDER BY id LIMIT :size"
)

# System rows are the platform's words, and hidden rows include its machine prompts, so neither
# can name a chat. The containment probe keeps rows without a user prompt off the wire.
_PROMPTS = sa.text(
    "SELECT conversation_id, payload FROM messages "
    "WHERE conversation_id IN :ids "
    "AND entry_kind IN ('turn', 'step') AND visibility = 'visible' "
    "AND payload @> CAST(:probe AS jsonb) "
    "ORDER BY conversation_id, seq"
).bindparams(sa.bindparam("ids", expanding=True))
_PROBE = json.dumps([{"kind": "request", "parts": [{"part_kind": "user-prompt"}]}])

# `IS NULL` again at write time: a send or a rename may have named the chat since it was read.
_NAME = sa.text("UPDATE conversations SET title = :title WHERE id = :id AND title IS NULL")


def derive_title(text: str) -> str | None:
    spaced = "".join(
        " " if unicodedata.category(char) in _SPACE_IN_A_NAME else char for char in text
    )
    name = " ".join(word for word in spaced.split(" ") if word)
    if not name:
        return None
    if len(name) <= _NAME_CODE_POINTS:
        return name
    return f"{name[:_NAME_CODE_POINTS]}…"


def _is_attachment_markup(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<attachment ") and stripped.endswith(("</attachment>", "/>"))


def typed_text(payload: Any) -> str:
    """What the citizen typed in one stored batch: every user prompt's prose, attachment
    markup and reference markers left out. A bare string is the text-only shape."""
    if isinstance(payload, str):  # a driver that hands JSONB back as text
        payload = json.loads(payload)
    texts: list[str] = []
    for message in payload if isinstance(payload, list) else []:
        if not isinstance(message, dict) or message.get("kind") != "request":
            continue
        for part in message.get("parts", []):
            if not isinstance(part, dict) or part.get("part_kind") != "user-prompt":
                continue
            content = part.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(
                    item
                    for item in content
                    if isinstance(item, str) and not _is_attachment_markup(item)
                )
    return "\n".join(texts)


def upgrade() -> None:
    bind = op.get_bind()
    after = uuid.UUID(int=0)
    while True:
        ids = list(bind.execute(_UNTITLED, {"after": after, "size": _BATCH}).scalars())
        if not ids:
            return
        after = ids[-1]
        names: dict[uuid.UUID, str] = {}
        rows = bind.execute(
            _PROMPTS.execution_options(yield_per=100), {"ids": ids, "probe": _PROBE}
        )
        for conversation_id, payload in rows:
            if conversation_id in names:
                continue
            title = derive_title(typed_text(payload))
            if title is not None:
                names[conversation_id] = title
        if names:
            bind.execute(_NAME, [{"id": key, "title": value} for key, value in names.items()])


def downgrade() -> None:
    # Nothing to undo: a derived name cannot be told apart from one a person typed.
    pass
