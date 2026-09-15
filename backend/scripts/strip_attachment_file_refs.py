"""The way back from the code-lane attachment marker, for a server being rolled back.

WHY THIS EXISTS: a message written after code-lane attachments shipped carries
`bial-attachment-file-ref` markers in its payload, and a server that does not know that kind does
not skip them — the type adapter refuses the whole payload, so every conversation holding a sent
spreadsheet, document or deck stops loading. The payload version cannot carry the warning: the
gate is per CONVERSATION, so raising it would strand every conversation a new server touched
rather than only these.

This removes the markers, which is exactly what a reader that understands them does at load time.
The cost is the chip: a file that was sent is no longer named in that message, so it draws no chip
on reload and the delete sweep no longer finds it through history. The attachment row and its
stored object are untouched — nothing is deleted here.

  DRY RUN (default):  uv run python -m scripts.strip_attachment_file_refs
  EXECUTE:            uv run python -m scripts.strip_attachment_file_refs --execute
"""

# The module docstring above is shown verbatim as `--help` text (argparse description=__doc__).

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import sqlalchemy as sa

from src.db.base import async_session_factory
from src.db.models.message import Message
from src.services.messages.store import ATTACHMENT_FILE_REF_KIND


def without_file_refs(node: Any) -> Any:
    """The payload with every code-lane marker removed, walked like the loader walks it.

    A marker only ever appears as an item of a list — that is where a user prompt's content items
    live — so dropping it there is the whole of the change. The traversal is otherwise exhaustive
    and returns new structures, never mutating what it was given.
    """
    if isinstance(node, list):
        cleaned = [without_file_refs(item) for item in node]
        return [item for item in cleaned if item is not None]
    if isinstance(node, dict):
        if node.get("kind") == ATTACHMENT_FILE_REF_KIND:
            return None
        return {key: without_file_refs(value) for key, value in node.items()}
    return node


def count_file_refs(node: Any) -> int:
    """How many markers a payload holds, for a report that states what it would change."""
    if isinstance(node, list):
        return sum(count_file_refs(item) for item in node)
    if isinstance(node, dict):
        if node.get("kind") == ATTACHMENT_FILE_REF_KIND:
            return 1
        return sum(count_file_refs(value) for value in node.values())
    return 0


async def _run(execute: bool) -> None:
    # Narrowed in SQL so an estate with millions of rows does not load them all: the marker is a
    # literal string in the JSONB, so a text match finds every candidate and the walk below is
    # what decides.
    candidates = sa.select(Message.id, Message.conversation_id, Message.payload).where(
        sa.cast(Message.payload, sa.Text).like(f"%{ATTACHMENT_FILE_REF_KIND}%")
    )
    changed_rows = 0
    removed = 0
    conversations: set[Any] = set()
    async with async_session_factory() as db:
        for row_id, conversation_id, payload in (await db.execute(candidates)).all():
            found = count_file_refs(payload)
            if not found:
                continue
            changed_rows += 1
            removed += found
            conversations.add(conversation_id)
            if execute:
                await db.execute(
                    sa.update(Message)
                    .where(Message.id == row_id)
                    .values(payload=without_file_refs(payload))
                )
        if execute:
            await db.commit()

    verb = "removed" if execute else "would remove"
    print(f"{verb} {removed} marker(s) from {changed_rows} message(s)")
    print(f"conversations affected: {len(conversations)}")
    if not execute:
        print("dry run — pass --execute to write")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="commit (default: report and change nothing)"
    )
    asyncio.run(_run(parser.parse_args().execute))


if __name__ == "__main__":
    main()
