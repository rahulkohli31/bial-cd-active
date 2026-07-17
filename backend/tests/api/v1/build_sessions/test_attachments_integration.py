"""R3 composition-root test — the materialization seam with NOTHING stubbed past.

The `mocks-mask-composition-seams` learning (2026-07-15) in its purest form: every other R3 test
reaches the object store through `FakeStorage`, which round-trips a dict and would happily "prove"
a materialization path that cannot actually read a real blob. This one runs the REAL wiring —
`get_storage()` → a real `AzureBlobStorage` → Azurite — against REAL `conversations` /`messages` /
`attachments` rows, with NO `dependency_overrides` on the storage seam. It is the only test that
can catch the seam itself being wrong (a key built one way and read another, a content-type that
mangles bytes, an owner-prefix mismatch).
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from pydantic_ai import BinaryContent
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.attachment import Attachment
from src.db.models.conversation import ConversationKind
from src.services.build_sessions.attachments import resolve_build_attachments
from src.services.storage import attachment_key, get_storage, reset_storage_for_tests
from src.services.storage.azure_backend import AzureBlobStorage
from src.services.storage.config import AzureStorageConfig
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory
from tests.services.storage.conftest import (
    AZURITE_ACCOUNT_URL,
    AZURITE_CONN,
    _ensure_azure_container,
    _port_open,
)

PNG_BYTES = bytes([0x89, 0x50, 0x4E, 0x47]) + b"\r\n\x1a\n pretend png payload"


@pytest.mark.integration
async def test_materialization_reads_real_blob_bytes_through_the_real_store(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not _port_open("127.0.0.1", 10000):
        pytest.skip(
            "Azurite not reachable on :10000 — `docker compose -f docker-compose.test.yml up -d`"
        )
    await reset_storage_for_tests()
    config = AzureStorageConfig(
        account_url=AZURITE_ACCOUNT_URL,
        container="platform",
        connection_string=SecretStr(AZURITE_CONN),
    )
    monkeypatch.setattr(settings, "object_store", config)
    # The platform container is the deployment's own; create it if this is a fresh Azurite.
    storage = get_storage()
    assert isinstance(storage, AzureBlobStorage)
    await _ensure_azure_container(storage, config.container)

    user = await UserFactory.create(db_session, email="r3int@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.BUILDER
    )
    # Upload the bytes exactly as `/v1/attachments` would: real store, real owner-scoped key.
    key = attachment_key(user.id, uuid.uuid7())
    await storage.put(key, PNG_BYTES, content_type="image/png")
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id="a-int",
            media_type="image/png",
            name="chart.png",
            size=len(PNG_BYTES),
            storage_key=key,
        )
    )
    await db_session.flush()
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        seq=0,
        parts=[
            {
                "type": "file",
                "attachmentId": "a-int",
                "key": key,
                "kind": "image",
                "name": "chart.png",
                "mediaType": "image/png",
                "size": len(PNG_BYTES),
            },
            {"type": "text", "text": "build a dashboard from this"},
        ],
    )

    try:
        content = await resolve_build_attachments(db_session, user.id, project.id, conv.id)

        assert len(content) == 1
        binary = content[0]
        assert isinstance(binary, BinaryContent)
        # Byte-for-byte through the real round trip — the magic-byte re-assert passed on bytes
        # that actually came back from Blob, not from a dict.
        assert binary.data == PNG_BYTES
        assert binary.media_type == "image/png"
    finally:
        await storage.delete(key)
        await reset_storage_for_tests()
