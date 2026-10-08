"""Making a chat's session hold exactly the files the chat has sent, plus the reader.

THE STORED COPY IS THE TRUTH; the session holds working copies. Azure deletes an idle session
without warning, and a call with the same identifier then quietly opens an empty one. So the
backend keeps, in process, a record of what it copied into each chat's session, and the first file
access of every reply lists the session against it:

- no record, or a recorded file missing or changed: the session is treated as new, and every
  sent file is copied in;
- otherwise only what is new is copied in, and what is no longer attached is deleted.

The record lives outside the session, where code running in it cannot alter it. Losing it, to a
restart or to its expiry, costs a full copy, never a file.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import structlog

from src.services.agent.read_tools import ATTACHMENTS_PREFIX
from src.services.analysis.runtime import READER_NAME as READER_NAME
from src.services.analysis.runtime import (
    SESSION_FILES_DIR,
    AnalysisRuntime,
    AnalysisTimedOutError,
    AnalysisUnavailableError,
    Execution,
    SessionFile,
    session_identifier,
)
from src.services.attachments.materialize import CodeLaneAttachment
from src.services.orchestrator.constants import (
    ANALYSIS_DELETE_DEADLINE_S,
    ANALYSIS_RUN_STREAM_LIMIT,
)
from src.services.storage.base import ObjectStorage
from src.services.storage.errors import StorageError, StorageNotFoundError

_log = structlog.get_logger()

READER: Final = (Path(__file__).parent / "assets" / "read_attachment.py.txt").read_bytes()
"""Byte-identical to `sandbox/scripts/read_attachment.py`; a test holds the two equal."""

RECORD_TTL_S: Final = 20 * 60.0
"""The pool's idle cool-down. A record untouched for longer describes a session Azure has
deleted."""

_LINK: Final = ATTACHMENTS_PREFIX.rstrip("/")
# Files land in the working directory, and the service refuses a folder named with a dot, so
# the one path the model is given resolves through a link to that directory.
LINK_CODE: Final = (
    "import os\n"
    f"os.chdir({SESSION_FILES_DIR!r})\n"
    f"os.path.islink({_LINK!r}) or os.symlink('.', {_LINK!r})\n"
)
_LINK_TIMEOUT_S: Final = 30.0


class FileGoneError(RuntimeError):
    """A sent file's stored copy is missing. Nothing after it was copied."""


@dataclass(frozen=True, slots=True)
class _Copied:
    attachment_id: str
    size: int
    modified: str | None


@dataclass(slots=True)
class _Record:
    files: dict[str, _Copied]
    touched: float


_records: dict[uuid.UUID, _Record] = {}


def _live_record(conversation_id: uuid.UUID) -> _Record | None:
    record = _records.get(conversation_id)
    if record is not None and time.monotonic() - record.touched > RECORD_TTL_S:
        del _records[conversation_id]
        return None
    return record


def _touch(conversation_id: uuid.UUID) -> None:
    """Age the record from the session's last call, as the pool ages the session."""
    record = _records.get(conversation_id)
    if record is not None:
        record.touched = time.monotonic()


def forget(conversation_id: uuid.UUID) -> None:
    """Drop the chat's record, after its session was deleted."""
    _records.pop(conversation_id, None)


async def end_session(runtime: AnalysisRuntime, conversation_id: uuid.UUID) -> None:
    """Forget the chat's record, then delete its session within the delete deadline."""
    forget(conversation_id)
    async with asyncio.timeout(ANALYSIS_DELETE_DEADLINE_S):
        await runtime.delete_session(session_identifier(conversation_id))


def _matches(record: _Record, listed: dict[str, SessionFile]) -> bool:
    for name, copied in record.files.items():
        found = listed.get(name)
        if found is None or found.size != copied.size:
            return False
        if copied.modified is not None and found.modified != copied.modified:
            return False
    return True


async def place(
    runtime: AnalysisRuntime,
    *,
    conversation_id: uuid.UUID,
    files: Sequence[CodeLaneAttachment],
    storage: ObjectStorage | None,
) -> bool:
    """Reconcile the chat's session with `files`, and say whether it had to be filled anew.

    Raises `AnalysisUnavailableError` when the session or the store cannot be reached, and
    `FileGoneError` when a file's stored copy is gone."""
    session_id = session_identifier(conversation_id)
    listed = {file.name: file for file in await runtime.list_files(session_id)}
    record = _live_record(conversation_id)
    fresh = record is None or not _matches(record, listed)
    wanted = {file.file_name: file for file in files}
    kept = {} if fresh or record is None else record.files

    # A session filled anew keeps nothing it was not given. A live one keeps what code wrote
    # there; only a recorded file that is no longer attached goes.
    stale = [
        name for name in (listed if fresh else kept) if name not in wanted and name != READER_NAME
    ]
    missing = [
        file
        for name, file in wanted.items()
        if name not in kept or kept[name].attachment_id != file.attachment_id
    ]
    copied_bytes = 0
    for name in stale:
        await runtime.delete_file(session_id, name)
    for file in missing:
        data = await _stored_bytes(storage, file)
        await runtime.upload_file(session_id, file.file_name, data)
        copied_bytes += len(data)
    await runtime.upload_file(session_id, READER_NAME, READER)
    linked = await runtime.run(
        session_id, LINK_CODE, timeout_s=_LINK_TIMEOUT_S, output_limit=ANALYSIS_RUN_STREAM_LIMIT
    )
    if not linked.succeeded:
        raise AnalysisUnavailableError("the attachments link could not be made")

    now_listed = (
        {file.name: file for file in await runtime.list_files(session_id)}
        if stale or missing
        else listed
    )
    _records[conversation_id] = _Record(
        files={
            name: _Copied(
                attachment_id=file.attachment_id,
                size=file.size,
                modified=now_listed[name].modified if name in now_listed else None,
            )
            for name, file in wanted.items()
        },
        touched=time.monotonic(),
    )
    _log.info(
        "analysis_placement",
        conversation_id=str(conversation_id),
        fresh=fresh,
        files=len(missing),
        removed=len(stale),
        bytes=copied_bytes,
    )
    return fresh


async def _stored_bytes(storage: ObjectStorage | None, file: CodeLaneAttachment) -> bytes:
    if storage is None:
        raise AnalysisUnavailableError("no object store")
    try:
        return await storage.get(file.storage_key)
    except StorageNotFoundError:
        raise FileGoneError(file.attachment_id) from None
    except StorageError:
        raise AnalysisUnavailableError("object store unreachable") from None


@dataclass
class AnalysisSession:
    """One reply's handle on its chat's session.

    Placement runs on the reply's first file access and not again unless asked; the lock makes
    two tool calls issued together place once. `running` is set while code executes and stays set
    if the run never came back (cancelled, timed out, unreachable) or the delete meant to end it
    did not answer: that is what tells a Stop or a timeout that the session must be deleted."""

    conversation_id: uuid.UUID
    files: tuple[CodeLaneAttachment, ...]
    storage: ObjectStorage | None
    runtime: AnalysisRuntime | None
    placed: bool = False
    fresh: bool = False
    running: bool = False
    tool_calls: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def session_id(self) -> str:
        return session_identifier(self.conversation_id)

    def _runtime(self) -> AnalysisRuntime:
        if self.runtime is None:
            raise AnalysisUnavailableError("analysis is not configured")
        return self.runtime

    async def ensure_placed(self, *, again: bool = False) -> AnalysisRuntime:
        """Place the chat's files if this reply has not, or once more when `again`."""
        runtime = self._runtime()
        async with self._lock:
            if self.placed and not again:
                return runtime
            try:
                fresh = await place(
                    runtime,
                    conversation_id=self.conversation_id,
                    files=self.files,
                    storage=self.storage,
                )
            except AnalysisTimedOutError:
                # No code of the model's is running yet, so this is not a timeout that ends the
                # session.
                raise AnalysisUnavailableError("placement outlived its deadline") from None
            self.fresh = self.fresh or fresh
            self.placed = True
        return runtime

    async def run(self, code: str, *, timeout_s: float, output_limit: int) -> Execution:
        """Run `code` in the chat's session, after placement."""
        runtime = await self.ensure_placed()
        self.running = True
        result = await runtime.run(
            self.session_id, code, timeout_s=timeout_s, output_limit=output_limit
        )
        self.running = False
        _touch(self.conversation_id)
        return result

    async def end(self) -> None:
        """Delete the chat's session and its record: the only way to stop code that is running.
        The next file access re-creates it and copies everything back in."""
        self.placed = False
        await end_session(self._runtime(), self.conversation_id)
        self.running = False
