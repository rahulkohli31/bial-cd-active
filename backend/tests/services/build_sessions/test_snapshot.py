"""U5 — the C4 snapshot write (no DB / Redis; fake storage + a scripted fake client)."""

from __future__ import annotations

import base64
import uuid

import pytest

from src.services.build_sessions.snapshot import write_snapshot
from src.services.sandbox.base import ExecResult, SandboxError, SandboxHandle
from src.services.storage import snapshot_key
from tests.fakes import FakeSandboxClient, FakeStorage

APP_ID = uuid.uuid4()


def _handle() -> SandboxHandle:
    return SandboxHandle(
        fqdn="sbx-x.example",
        token="tok",
        app_name="sbx-x",
        preview_url="https://sbx-x.example/",
        ready=False,
    )


async def test_write_snapshot_bundles_and_puts_to_blob(fake_storage: FakeStorage) -> None:
    client = FakeSandboxClient()
    scripts: list[str] = []

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["sh", "-c"]:
            scripts.append(cmd[2])
        if cmd[:1] == ["base64"]:
            return ExecResult(
                stdout=base64.b64encode(b"BUNDLE-CONTENT").decode(), stderr="", exit=0
            )
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    await write_snapshot(client, _handle(), APP_ID)
    # The base64'd bundle round-trips to Blob at the C4 key (byte-stable).
    assert fake_storage.objects[snapshot_key(APP_ID)] == b"BUNDLE-CONTENT"
    # The commit script survives a GIT-LESS workspace (the baked image has no .git): it inits
    # idempotently and guards the nothing-to-commit case (mirrors sandbox/scripts/snapshot.sh).
    # Asserted on the script text — the dict-backed fake cannot run real git.
    assert scripts[0].startswith("git init -q")
    assert "git diff --cached --quiet || git commit" in scripts[0]


async def test_write_snapshot_raises_on_commit_failure(fake_storage: FakeStorage) -> None:
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["sh", "-c"] and "git init" in cmd[2]:
            return ExecResult(stdout="", stderr="fatal: not a work tree", exit=128)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError, match="commit failed"):
        await write_snapshot(client, _handle(), APP_ID)
    assert snapshot_key(APP_ID) not in fake_storage.objects


async def test_write_snapshot_bundle_failure_never_uploads_a_stale_bundle(
    fake_storage: FakeStorage,
) -> None:
    # The bundle step fails but a STALE app.bundle from an earlier snapshot is still on disk:
    # the exit-code check must abort before the base64 read ever ships it as "latest".
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["sh", "-c"] and "git bundle" in cmd[2]:
            return ExecResult(stdout="", stderr="fatal: refusing to create empty bundle", exit=128)
        if cmd[:1] == ["base64"]:  # the stale on-disk bundle would read back fine
            return ExecResult(stdout=base64.b64encode(b"STALE").decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError, match="bundle failed"):
        await write_snapshot(client, _handle(), APP_ID)
    assert snapshot_key(APP_ID) not in fake_storage.objects


async def test_write_snapshot_raises_on_bundle_read_failure(fake_storage: FakeStorage) -> None:
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["base64"]:
            return ExecResult(stdout="", stderr="bundle failed", exit=1)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError):
        await write_snapshot(client, _handle(), APP_ID)
    # A failed bundle read leaves no dangling blob.
    assert snapshot_key(APP_ID) not in fake_storage.objects
