"""The rollback path for the code-lane attachment marker.

The claim under test is not "a key disappears" — it is that what comes out is a payload a server
which has never heard of the marker can load. That server is represented here by the type adapter
itself, which is what refuses on the real rollback.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, UserPromptPart

from scripts.strip_attachment_file_refs import count_file_refs, without_file_refs
from src.services.messages.store import ATTACHMENT_FILE_REF_KIND


def _payload_with_a_sent_spreadsheet() -> list[dict]:
    """One stored message carrying prose and a code-lane marker, as the store writes it."""
    return [
        {
            "kind": "request",
            "parts": [
                {
                    "part_kind": "user-prompt",
                    "content": [
                        "what is in this roster?",
                        {"kind": ATTACHMENT_FILE_REF_KIND, "attachment_id": "att_roster"},
                    ],
                }
            ],
        }
    ]


def test_the_marker_is_what_stops_an_older_server_reading_the_conversation() -> None:
    """★ THE FAILURE THE SCRIPT EXISTS FOR, pinned before the fix is applied to it.

    A server that does not know this kind does not ignore it: the whole payload is refused, so a
    conversation that once carried a spreadsheet stops loading rather than losing one part of one
    message. Pinned here so nobody weakens the script on the belief that the marker is harmless.
    """
    with pytest.raises(Exception) as caught:
        ModelMessagesTypeAdapter.validate_python(_payload_with_a_sent_spreadsheet())

    assert "validation error" in str(caught.value).lower()


def test_a_stripped_payload_is_one_an_older_server_can_load() -> None:
    """★ THE POINT OF THE SCRIPT. What comes back is readable, and the prose survives — the file
    is simply no longer named in that message, which is the cost stated in the script's own
    docstring.

    Mutation receipt: return the node unchanged for the marker and this raises exactly as the
    test above does.
    """
    stripped = without_file_refs(_payload_with_a_sent_spreadsheet())

    messages = ModelMessagesTypeAdapter.validate_python(stripped)
    part = messages[0].parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content == ["what is in this roster?"]


def test_everything_else_in_the_payload_is_left_exactly_as_it_was() -> None:
    """A rollback aid that edited anything else would be a second incident. Only the marker
    goes: sibling parts, nested dicts and ordinary lists come back identical."""
    payload = [
        {
            "kind": "request",
            "parts": [
                {"part_kind": "system-prompt", "content": "be helpful"},
                {
                    "part_kind": "user-prompt",
                    "content": [
                        "first",
                        {"kind": ATTACHMENT_FILE_REF_KIND, "attachment_id": "att_1"},
                        "second",
                    ],
                },
            ],
            "usage": {"requests": 1, "details": {"cached": 0}},
        }
    ]

    stripped = without_file_refs(payload)

    assert stripped[0]["parts"][0] == {"part_kind": "system-prompt", "content": "be helpful"}
    assert stripped[0]["parts"][1]["content"] == ["first", "second"]
    assert stripped[0]["usage"] == {"requests": 1, "details": {"cached": 0}}


def test_the_count_is_what_the_dry_run_reports() -> None:
    """The dry run's number has to be the number of things it would change, or an operator
    cannot tell a no-op from a sweep that missed."""
    payload = _payload_with_a_sent_spreadsheet()
    payload.append(
        {
            "kind": "request",
            "parts": [
                {
                    "part_kind": "user-prompt",
                    "content": [
                        {"kind": ATTACHMENT_FILE_REF_KIND, "attachment_id": "att_2"},
                        {"kind": ATTACHMENT_FILE_REF_KIND, "attachment_id": "att_3"},
                    ],
                }
            ],
        }
    )

    assert count_file_refs(payload) == 3
    assert count_file_refs(without_file_refs(payload)) == 0
