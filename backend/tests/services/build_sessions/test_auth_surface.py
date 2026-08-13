"""The R21 automated build check (issue #92): the pure heuristic over a fake workspace
tree, and the sandbox-walking wrapper. Mirrors `test_liveness.py`'s structure."""

from __future__ import annotations

import base64
import io
import tarfile
import uuid

from structlog.testing import capture_logs

from src.services.build_sessions.auth_surface import (
    auth_surface_detected,
    check_auth_surface,
)
from src.services.sandbox import ExecResult, SandboxError
from tests.fakes import FakeSandboxClient, _fake_handle

_CLEAN_TREE = {
    "app/page.tsx": (
        "import { getBialIdentity } from '@/lib/bial-identity';\n"
        "export default async function Home() {\n"
        "  const identity = await getBialIdentity();\n"
        "  return <p>{identity?.displayName}</p>;\n"
        "}\n"
    ),
    "db/schema.ts": (
        "export const items = pgTable('items', { id: uuid('id').primaryKey() });\n"
    ),
    "package.json": '{"dependencies": {"next": "16.0.0", "drizzle-orm": "0.45.2"}}',
}


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


# --- the pure heuristic -----------------------------------------------------------


def test_a_clean_app_using_the_platform_accessor_is_not_flagged() -> None:
    assert auth_surface_detected(_CLEAN_TREE) == []


def test_a_sign_in_route_is_flagged() -> None:
    files = {**_CLEAN_TREE, "app/sign-in/page.tsx": "export default function SignIn() {}"}
    findings = auth_surface_detected(files)
    assert any("app/sign-in/page.tsx" in f for f in findings)


def test_a_login_route_variant_is_flagged() -> None:
    files = {**_CLEAN_TREE, "app/login/page.tsx": "export default function Login() {}"}
    findings = auth_surface_detected(files)
    assert any("app/login/page.tsx" in f for f in findings)


def test_a_nextauth_catchall_route_is_flagged() -> None:
    files = {
        **_CLEAN_TREE,
        "app/api/auth/[...nextauth]/route.ts": "export const GET = handler;",
    }
    findings = auth_surface_detected(files)
    assert any("[...nextauth]" in f for f in findings)


def test_mentioning_login_in_prose_alone_is_not_flagged() -> None:
    # A path segment match only — page COPY that happens to say "log in" is not a route.
    files = {**_CLEAN_TREE, "app/help/page.tsx": "<p>Need help logging in?</p>"}
    assert auth_surface_detected(files) == []


def test_a_forbidden_auth_dependency_is_flagged() -> None:
    files = {
        **_CLEAN_TREE,
        "package.json": '{"dependencies": {"next": "16.0.0", "next-auth": "5.0.0"}}',
    }
    findings = auth_surface_detected(files)
    assert any("next-auth" in f for f in findings)


def test_a_forbidden_dev_dependency_is_also_flagged() -> None:
    files = {
        **_CLEAN_TREE,
        "package.json": '{"dependencies": {}, "devDependencies": {"@clerk/nextjs": "5.0.0"}}',
    }
    findings = auth_surface_detected(files)
    assert any("@clerk/nextjs" in f for f in findings)


def test_an_ordinary_dependency_is_not_flagged() -> None:
    files = {**_CLEAN_TREE, "package.json": '{"dependencies": {"zod": "4.0.0"}}'}
    assert auth_surface_detected(files) == []


def test_password_hashing_code_is_flagged() -> None:
    files = {
        **_CLEAN_TREE,
        "app/actions.ts": "const hashed = await bcrypt.hash(password, 10);",
    }
    findings = auth_surface_detected(files)
    assert any("password-hashing" in f for f in findings)


def test_a_credential_column_in_the_schema_is_flagged() -> None:
    files = {
        **_CLEAN_TREE,
        "db/schema.ts": (
            "export const users = pgTable('users', {"
            " id: uuid('id').primaryKey(),"
            " password: text('password').notNull() });"
        ),
    }
    findings = auth_surface_detected(files)
    assert any("credential column" in f for f in findings)


def test_the_word_password_in_a_ui_label_is_not_flagged() -> None:
    # Only schema.ts-shaped files are checked for a credential COLUMN — an ordinary
    # third-party-account form label elsewhere is not a schema.
    files = {**_CLEAN_TREE, "app/page.tsx": '<label>Password (for your bank, not us)</label>'}
    assert auth_surface_detected(files) == []


def test_multiple_findings_are_all_reported() -> None:
    files = {
        "app/sign-in/page.tsx": "export default function SignIn() {}",
        "package.json": '{"dependencies": {"next-auth": "5.0.0"}}',
    }
    findings = auth_surface_detected(files)
    assert len(findings) == 2


# --- the sandbox-walking wrapper ---------------------------------------------------


async def test_wrapper_collects_the_tree_and_warns_with_ids() -> None:
    client = FakeSandboxClient()
    tree = {"./app/sign-in/page.tsx": "export default function SignIn() {}"}
    client.exec_handler = lambda cmd: ExecResult(stdout=_tar_b64(tree), stderr="", exit=0)
    app_id, session_id = uuid.uuid4(), uuid.uuid4()

    with capture_logs() as logs:
        findings = await check_auth_surface(
            client, _handle(), app_id=app_id, session_id=session_id
        )

    assert any("sign-in" in f for f in findings)
    warning = next(log for log in logs if "authentication surface" in str(log.get("event")))
    assert warning["app_id"] == str(app_id)
    assert warning["session_id"] == str(session_id)


async def test_wrapper_is_silent_when_the_tree_is_clean() -> None:
    client = FakeSandboxClient()
    client.exec_handler = lambda cmd: ExecResult(stdout=_tar_b64(_CLEAN_TREE), stderr="", exit=0)

    with capture_logs() as logs:
        findings = await check_auth_surface(
            client, _handle(), app_id=uuid.uuid4(), session_id=uuid.uuid4()
        )

    assert findings == []
    assert not any("authentication surface" in str(log.get("event")) for log in logs)


async def test_wrapper_fails_open_on_a_sandbox_transport_failure() -> None:
    # A dead container must log and return [] — an infrastructure hiccup in THIS check
    # must not be indistinguishable from "we scanned a clean tree" at the CALLER, but it
    # also must not fail an otherwise-good build on its own transport blip (see the
    # module's own docstring on this trade-off).
    client = FakeSandboxClient()

    def _boom(cmd: list[str]) -> ExecResult:
        raise SandboxError("container is gone")

    client.exec_handler = _boom
    findings = await check_auth_surface(
        client, _handle(), app_id=uuid.uuid4(), session_id=uuid.uuid4()
    )
    assert findings == []


async def test_wrapper_treats_an_empty_collect_as_nothing_to_scan() -> None:
    client = FakeSandboxClient()  # default exec: exit 0, empty stdout
    findings = await check_auth_surface(
        client, _handle(), app_id=uuid.uuid4(), session_id=uuid.uuid4()
    )
    assert findings == []
