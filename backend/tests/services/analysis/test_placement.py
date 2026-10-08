"""A chat's session holds exactly its sent files and the reader, whatever Azure did to it."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
import structlog.testing

from src.db.models.attachment import Attachment
from src.services.analysis import AnalysisUnavailableError, placement
from src.services.analysis.placement import (
    READER,
    READER_NAME,
    AnalysisSession,
    FileGoneError,
)
from src.services.attachments.materialize import CodeLaneAttachment, _named_without_collisions
from tests.fakes import FakeAnalysisRuntime, FakeStorage

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _no_records():
    placement._records.clear()
    yield
    placement._records.clear()


@pytest.fixture
def runtime() -> FakeAnalysisRuntime:
    return FakeAnalysisRuntime()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


def _file(storage: FakeStorage, name: str, data: bytes = b"cells") -> CodeLaneAttachment:
    attachment_id = f"att_{uuid.uuid4().hex[:12]}"
    storage.objects[f"att/{attachment_id}"] = data
    return CodeLaneAttachment(
        attachment_id=attachment_id,
        display_name=name,
        file_name=name,
        media_type=_XLSX,
        size=len(data),
        storage_key=f"att/{attachment_id}",
    )


def _reply(
    runtime: FakeAnalysisRuntime,
    storage: FakeStorage,
    files: list[CodeLaneAttachment],
    conversation_id: uuid.UUID,
) -> AnalysisSession:
    return AnalysisSession(
        conversation_id=conversation_id, files=tuple(files), storage=storage, runtime=runtime
    )


def _names_uploaded(runtime: FakeAnalysisRuntime) -> list[str]:
    return [name for _, name in runtime.uploads]


def _held(runtime: FakeAnalysisRuntime, conversation_id: uuid.UUID) -> set[str]:
    return set(runtime.files.get(conversation_id.hex, {}))


async def test_a_first_reply_copies_every_sent_file_and_the_reader(runtime, storage) -> None:
    chat = uuid.uuid4()
    q3, q4 = _file(storage, "q3.xlsx", b"q3 rows"), _file(storage, "q4.xlsx", b"q4 rows")

    await _reply(runtime, storage, [q3, q4], chat).ensure_placed()

    assert _names_uploaded(runtime) == ["q3.xlsx", "q4.xlsx", READER_NAME]
    assert runtime.files[chat.hex] == {
        "q3.xlsx": b"q3 rows",
        "q4.xlsx": b"q4 rows",
        READER_NAME: READER,
    }
    assert set(placement._records[chat].files) == {"q3.xlsx", "q4.xlsx"}


async def test_a_follow_up_on_a_live_session_uploads_only_the_reader(runtime, storage) -> None:
    chat = uuid.uuid4()
    files = [_file(storage, "q3.xlsx")]
    await _reply(runtime, storage, files, chat).ensure_placed()
    runtime.uploads.clear()

    follow_up = _reply(runtime, storage, files, chat)
    await follow_up.ensure_placed()

    assert _names_uploaded(runtime) == [READER_NAME]
    assert follow_up.fresh is False


async def test_a_session_azure_removed_is_refilled_with_every_sent_file(runtime, storage) -> None:
    chat = uuid.uuid4()
    files = [_file(storage, "q3.xlsx"), _file(storage, "q4.xlsx")]
    await _reply(runtime, storage, files, chat).ensure_placed()
    runtime.remove(chat.hex)
    runtime.uploads.clear()

    after_idle = _reply(runtime, storage, files, chat)
    with structlog.testing.capture_logs() as logs:
        await after_idle.ensure_placed()

    assert _names_uploaded(runtime) == ["q3.xlsx", "q4.xlsx", READER_NAME]
    assert after_idle.fresh is True
    placed = [log for log in logs if log["event"] == "analysis_placement"]
    assert [(log["fresh"], log["files"]) for log in placed] == [(True, 2)]


async def test_a_lost_record_copies_everything_and_drops_what_was_not_sent(
    runtime, storage
) -> None:
    """A backend restart loses the record while the session lives on: a full copy is safe, and
    a file deleted from the chat meanwhile must not survive it."""
    chat = uuid.uuid4()
    q3 = _file(storage, "q3.xlsx")
    gone = _file(storage, "gone.xlsx")
    await _reply(runtime, storage, [q3, gone], chat).ensure_placed()
    placement._records.clear()
    runtime.uploads.clear()

    await _reply(runtime, storage, [q3], chat).ensure_placed()

    assert _names_uploaded(runtime) == ["q3.xlsx", READER_NAME]
    assert _held(runtime, chat) == {"q3.xlsx", READER_NAME}


async def test_a_copy_changed_in_the_session_is_replaced(runtime, storage) -> None:
    chat = uuid.uuid4()
    q3 = _file(storage, "q3.xlsx", b"the stored rows")
    await _reply(runtime, storage, [q3], chat).ensure_placed()
    await runtime.upload_file(chat.hex, "q3.xlsx", b"rows code rewrote")
    runtime.uploads.clear()

    await _reply(runtime, storage, [q3], chat).ensure_placed()

    assert runtime.files[chat.hex]["q3.xlsx"] == b"the stored rows"
    assert "q3.xlsx" in _names_uploaded(runtime)


async def test_a_copy_resized_in_the_session_is_replaced_even_with_no_stamp(
    runtime, storage
) -> None:
    chat = uuid.uuid4()
    q3 = _file(storage, "q3.xlsx", b"the stored rows")
    await _reply(runtime, storage, [q3], chat).ensure_placed()
    runtime.stamps[chat.hex] = dict.fromkeys(runtime.stamps[chat.hex], "same")
    placement._records[chat].files["q3.xlsx"] = placement._Copied(
        attachment_id=q3.attachment_id, size=q3.size, modified=None
    )
    runtime.files[chat.hex]["q3.xlsx"] = b"short"

    await _reply(runtime, storage, [q3], chat).ensure_placed()

    assert runtime.files[chat.hex]["q3.xlsx"] == b"the stored rows"


async def test_a_deleted_attachment_leaves_the_session_before_the_next_read(
    runtime, storage
) -> None:
    chat = uuid.uuid4()
    q3, q4 = _file(storage, "q3.xlsx"), _file(storage, "q4.xlsx")
    await _reply(runtime, storage, [q3, q4], chat).ensure_placed()

    await _reply(runtime, storage, [q3], chat).ensure_placed()

    assert runtime.deletions == [(chat.hex, "q4.xlsx")]
    assert _held(runtime, chat) == {"q3.xlsx", READER_NAME}


async def test_a_file_added_in_this_message_is_uploaded_alone(runtime, storage) -> None:
    chat = uuid.uuid4()
    q3 = _file(storage, "q3.xlsx")
    await _reply(runtime, storage, [q3], chat).ensure_placed()
    runtime.uploads.clear()

    await _reply(runtime, storage, [q3, _file(storage, "q4.xlsx")], chat).ensure_placed()

    assert _names_uploaded(runtime) == ["q4.xlsx", READER_NAME]


async def test_what_code_wrote_into_a_live_session_is_kept(runtime, storage) -> None:
    chat = uuid.uuid4()
    q3 = _file(storage, "q3.xlsx")
    await _reply(runtime, storage, [q3], chat).ensure_placed()
    await runtime.upload_file(chat.hex, "totals.csv", b"computed")

    await _reply(runtime, storage, [q3], chat).ensure_placed()

    assert "totals.csv" in _held(runtime, chat)


async def test_a_file_replaced_under_the_same_name_is_copied_again(runtime, storage) -> None:
    chat = uuid.uuid4()
    first = _file(storage, "q3.xlsx", b"first")
    await _reply(runtime, storage, [first], chat).ensure_placed()
    second = _file(storage, "q3.xlsx", b"second")

    await _reply(runtime, storage, [second], chat).ensure_placed()

    assert runtime.files[chat.hex]["q3.xlsx"] == b"second"


def test_two_files_with_one_name_get_distinct_names_with_no_spaces() -> None:
    user = uuid.uuid4()
    rows = [
        Attachment(
            user_id=user,
            attachment_id=f"att_{index}",
            media_type=_XLSX,
            name=name,
            size=1,
            storage_key=f"att/{index}",
        )
        for index, name in enumerate(["Q3 report.xlsx", "Q3-report.xlsx", "रिपोर्ट मार्च.xlsx"])
    ]

    names = [file.file_name for file in _named_without_collisions(rows)]

    assert names == ["Q3_report.xlsx", "Q3-report.xlsx", "attachment.xlsx"]
    assert all(" " not in name for name in names)


async def test_two_chats_never_share_a_session(runtime, storage) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()

    await _reply(runtime, storage, [_file(storage, "mine.xlsx")], first).ensure_placed()
    await _reply(runtime, storage, [_file(storage, "theirs.xlsx")], second).ensure_placed()

    assert _held(runtime, first) == {"mine.xlsx", READER_NAME}
    assert _held(runtime, second) == {"theirs.xlsx", READER_NAME}


async def test_a_missing_blob_stops_placement_before_anything_after_it(runtime, storage) -> None:
    chat = uuid.uuid4()
    gone, after = _file(storage, "gone.xlsx"), _file(storage, "after.xlsx")
    del storage.objects[gone.storage_key]

    with pytest.raises(FileGoneError):
        await _reply(runtime, storage, [gone, after], chat).ensure_placed()

    assert runtime.uploads == []
    assert chat not in placement._records


async def test_an_unreachable_session_is_unavailable(runtime, storage) -> None:
    runtime.unavailable = True

    with pytest.raises(AnalysisUnavailableError):
        await _reply(runtime, storage, [_file(storage, "q3.xlsx")], uuid.uuid4()).ensure_placed()


async def test_with_no_runtime_placement_is_unavailable_and_calls_nothing(storage) -> None:
    reply = AnalysisSession(
        conversation_id=uuid.uuid4(),
        files=(_file(storage, "q3.xlsx"),),
        storage=storage,
        runtime=None,
    )

    with pytest.raises(AnalysisUnavailableError):
        await reply.ensure_placed()


async def test_two_first_calls_together_place_once(runtime, storage) -> None:
    reply = _reply(runtime, storage, [_file(storage, "q3.xlsx")], uuid.uuid4())

    await asyncio.gather(reply.ensure_placed(), reply.ensure_placed())

    assert _names_uploaded(runtime) == ["q3.xlsx", READER_NAME]


async def test_a_reply_that_reads_nothing_calls_nothing(runtime, storage) -> None:
    _reply(runtime, storage, [_file(storage, "q3.xlsx")], uuid.uuid4())

    assert runtime.calls == []


async def test_placing_again_reconciles_once_more(runtime, storage) -> None:
    chat = uuid.uuid4()
    reply = _reply(runtime, storage, [_file(storage, "q3.xlsx")], chat)
    await reply.ensure_placed()
    del runtime.files[chat.hex]["q3.xlsx"]

    await reply.ensure_placed(again=True)

    assert "q3.xlsx" in _held(runtime, chat)


async def test_an_expired_record_is_treated_as_no_record(runtime, storage) -> None:
    chat = uuid.uuid4()
    files = [_file(storage, "q3.xlsx")]
    await _reply(runtime, storage, files, chat).ensure_placed()
    placement._records[chat].touched = time.monotonic() - placement.RECORD_TTL_S - 1
    runtime.uploads.clear()

    later = _reply(runtime, storage, files, chat)
    await later.ensure_placed()

    assert later.fresh is True
    assert _names_uploaded(runtime) == ["q3.xlsx", READER_NAME]


async def test_ending_deletes_the_session_and_the_next_access_refills_it(runtime, storage) -> None:
    chat = uuid.uuid4()
    files = [_file(storage, "q3.xlsx")]
    reply = _reply(runtime, storage, files, chat)
    await reply.ensure_placed()

    await reply.end()

    assert runtime.operations("delete_session") == [chat.hex]
    assert chat not in placement._records
    runtime.uploads.clear()
    await _reply(runtime, storage, files, chat).ensure_placed()
    assert _names_uploaded(runtime) == ["q3.xlsx", READER_NAME]


async def test_running_marks_the_reply_busy_only_while_code_runs(runtime, storage) -> None:
    reply = _reply(runtime, storage, [_file(storage, "q3.xlsx")], uuid.uuid4())
    runtime.hold_runs = asyncio.Event()

    running = asyncio.create_task(reply.run("print(1)", timeout_s=5))
    while not runtime.runs:
        await asyncio.sleep(0)
    assert reply.running is True
    runtime.hold_runs.set()
    await running

    assert reply.running is False
