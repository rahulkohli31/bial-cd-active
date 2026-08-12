"""The #46 generation-time detector (plan U1): the pure heuristic over a fake workspace tree,
and the sandbox-walking wrapper (tar-over-exec transport, best-effort failure modes)."""

from __future__ import annotations

import base64
import io
import tarfile
import uuid

from structlog.testing import capture_logs

from src.services.build_sessions.liveness import (
    flag_liveness_overpromise,
    liveness_overpromises,
)
from src.services.sandbox import ExecResult, SandboxError
from tests.fakes import FakeSandboxClient, _fake_handle

_PAGE_WITH_CLAIM = """
export default function Board() {
  return <p>A live board shared across desks — everyone sees updates in real-time.</p>
}
"""
_PAGE_PLAIN = """
export default function Home() {
  return <p>Track visitor passes. Reload to see the latest records.</p>
}
"""
_HOOK_WITH_REFETCH = """
export function useRecords() {
  useEffect(() => {
    const timer = setInterval(() => refetch(), 15000)
    return () => clearInterval(timer)
  }, [])
}
"""


def test_claims_with_no_refetch_anywhere_are_flagged() -> None:
    flagged = liveness_overpromises(
        {"app/page.tsx": _PAGE_WITH_CLAIM, "lib/data.ts": "export const x = 1"}
    )
    assert flagged == ["app/page.tsx"]


def test_claims_backed_by_a_refetch_pattern_are_not_flagged() -> None:
    # The refetch may live anywhere in the tree (a .ts hook included) — the claim is then true.
    files = {"app/page.tsx": _PAGE_WITH_CLAIM, "lib/useRecords.ts": _HOOK_WITH_REFETCH}
    assert liveness_overpromises(files) == []


def test_no_claims_means_no_flag_even_without_refetch() -> None:
    assert liveness_overpromises({"app/page.tsx": _PAGE_PLAIN}) == []


def test_claim_wording_in_a_non_ui_file_is_not_a_claim() -> None:
    # "shared"/"live" in a .ts helper is code vocabulary, not user-facing copy — only the UI
    # surfaces (.tsx/.jsx) can promise the user anything.
    assert liveness_overpromises({"lib/shared.ts": "// the shared live cache"}) == []


_PAGE_WRITE_NO_CLAIM = """
export default function Page() {
  async function save() {
    await fetch('/api/records', { method: 'POST', body: JSON.stringify({ name }) })
  }
  return <button onClick={save}>Add visitor pass</button>
}
"""


def test_after_write_without_a_claim_is_the_documented_u11_gap() -> None:
    """U11 ACCEPTED BOUNDARY (not a bug): the hoisted AFTER A WRITE prompt rule requires EVERY app
    to refetch after a mutation so the user sees their own change. This detector cannot enforce
    that — it is claim-gated by `_CLAIM_RE` and returns early when no UI file makes a
    live/shared/real-time claim. So an app that performs a write, wires NO refetch, and makes NO
    such claim VIOLATES the prompt rule yet is deliberately silent here. Measuring "the user saw
    their own write" needs a JS-executing probe the C2 SandboxClient can't run — deferred to issue
    #49. This test pins the silence so the gap stays documented, not forgotten."""
    files = {"app/page.tsx": _PAGE_WRITE_NO_CLAIM, "lib/data.ts": "export const x = 1"}
    assert liveness_overpromises(files) == []


def _tar_b64(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _handle():
    return _fake_handle("sbx-test")


async def test_wrapper_collects_the_tree_and_warns_with_ids() -> None:
    client = FakeSandboxClient()
    tree = {"./app/page.tsx": _PAGE_WITH_CLAIM}
    client.exec_handler = lambda cmd: ExecResult(stdout=_tar_b64(tree), stderr="", exit=0)
    app_id, session_id = uuid.uuid4(), uuid.uuid4()

    with capture_logs() as logs:
        flagged = await flag_liveness_overpromise(
            client, _handle(), app_id=app_id, session_id=session_id
        )

    assert flagged == ["./app/page.tsx"]
    warning = next(log for log in logs if "wires no refetch" in str(log.get("event")))
    # The ids are the deliverable — without them the signal can't be traced to an app.
    assert warning["app_id"] == str(app_id)
    assert warning["session_id"] == str(session_id)


async def test_wrapper_is_silent_when_the_refetch_is_wired() -> None:
    client = FakeSandboxClient()
    tree = {"./app/page.tsx": _PAGE_WITH_CLAIM, "./lib/useRecords.ts": _HOOK_WITH_REFETCH}
    client.exec_handler = lambda cmd: ExecResult(stdout=_tar_b64(tree), stderr="", exit=0)

    with capture_logs() as logs:
        flagged = await flag_liveness_overpromise(
            client, _handle(), app_id=uuid.uuid4(), session_id=uuid.uuid4()
        )

    assert flagged == []
    assert not any("wires no refetch" in str(log.get("event")) for log in logs)


async def test_wrapper_swallows_a_sandbox_failure_signal_not_gate() -> None:
    # The detector rides the end sequence: a dead container (or any failure) must log and
    # return [], never raise into finalize.
    client = FakeSandboxClient()

    def _boom(cmd: list[str]) -> ExecResult:
        raise SandboxError("container is gone")

    client.exec_handler = _boom
    flagged = await flag_liveness_overpromise(
        client, _handle(), app_id=uuid.uuid4(), session_id=uuid.uuid4()
    )
    assert flagged == []


async def test_wrapper_treats_an_empty_collect_as_nothing_to_scan() -> None:
    client = FakeSandboxClient()  # default exec: exit 0, empty stdout
    flagged = await flag_liveness_overpromise(
        client, _handle(), app_id=uuid.uuid4(), session_id=uuid.uuid4()
    )
    assert flagged == []
