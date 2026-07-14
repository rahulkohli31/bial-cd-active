"""U5 — the C4 snapshot write (no DB / Redis; fake storage + a scripted fake client)."""

from __future__ import annotations

import base64
import uuid

import pytest

from src.services.build_sessions.snapshot import write_snapshot
from src.services.sandbox.base import ExecResult, SandboxError, SandboxHandle
from src.services.storage import snapshot_key
from tests.fakes import FakeSandboxClient

APP_ID = uuid.uuid4()


def _handle() -> SandboxHandle:
    return SandboxHandle(
        fqdn="sbx-x.example",
        token="tok",
        app_name="sbx-x",
        preview_url="https://sbx-x.example/",
        ready=False,
    )


async def test_write_snapshot_bundles_and_puts_to_blob(fake_storage: object) -> None:
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["base64"]:
            return ExecResult(
                stdout=base64.b64encode(b"BUNDLE-CONTENT").decode(), stderr="", exit=0
            )
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    await write_snapshot(client, _handle(), APP_ID)
    # The base64'd bundle round-trips to Blob at the C4 key (byte-stable).
    assert fake_storage.objects[snapshot_key(APP_ID)] == b"BUNDLE-CONTENT"  # type: ignore[attr-defined]


async def test_write_snapshot_raises_on_bundle_read_failure(fake_storage: object) -> None:
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["base64"]:
            return ExecResult(stdout="", stderr="bundle failed", exit=1)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError):
        await write_snapshot(client, _handle(), APP_ID)
    # A failed bundle read leaves no dangling blob.
    assert snapshot_key(APP_ID) not in fake_storage.objects  # type: ignore[attr-defined]
