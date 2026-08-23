"""The R21 automated build check (issue #92): a generated app must not grow an
authentication surface of its own — a sign-in/sign-up route, password/credential
handling, a session store, or any handshake with an identity provider — whether or
not the app has a sign-in at all (the platform's own accessor is the only sanctioned
path, `sandbox/template/lib/bial-identity.ts`).

Architecture mirrors `liveness.py` exactly (the #46 detector): one sandbox `exec`
ships the source tree out as a base64 tar, ALL matching runs in Python — one source
of truth for the heuristic, unit-testable on a plain dict tree, no grep-dialect
drift between the sandbox image and this code. Unlike `liveness.py` (a structlog
signal, never a gate), a finding here FAILS the build (`manager.py`'s finalize
converts a would-be success into `BuildSessionStatus.FAILED`) — R21 is explicit
that this is the one automated check of the client's rule.
"""

from __future__ import annotations

import base64
import io
import json
import re
import tarfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import structlog

from src.services.sandbox import SandboxClient, SandboxHandle

_log = structlog.get_logger()

# Same collection shape as liveness.py's _COLLECT_SCRIPT, plus package.json (dependency
# names are the strongest, lowest-false-positive signal for a bundled auth SDK) and any
# db/schema.ts-shaped file (for a hand-rolled credential column).
_COLLECT_SCRIPT: Final = (
    "find . -path ./node_modules -prune -o -path ./.git -prune -o -path ./.next -prune "
    "-o -type f \\( -name '*.tsx' -o -name '*.jsx' -o -name '*.ts' -o -name '*.js' "
    "-o -name 'package.json' \\) -print0 "
    "| tar --null -T - -cf - | base64"
)
_COLLECT_TIMEOUT_SECONDS: Final = 60
_MAX_FILE_BYTES: Final = 256 * 1024
_MAX_FILES: Final = 500

# Next.js App Router route SEGMENT names that only make sense as a self-built sign-in
# surface — matched as a path segment (`/sign-in/`), never a substring, so a page merely
# mentioning "login" in prose is not enough (the finding is a FILE PATH, not page copy).
_AUTH_ROUTE_SEGMENT_RE: Final = re.compile(
    r"(?:^|/)(sign-in|signin|sign-up|signup|login|logout|register|"
    r"\[\.\.\.nextauth\])(?:/|$)",
    re.IGNORECASE,
)

# Known identity-provider SDKs and credential-handling libraries — a dependency name is
# a near-zero-false-positive signal (nobody adds `next-auth` to package.json by accident).
_FORBIDDEN_DEPENDENCIES: Final = frozenset(
    {
        "next-auth",
        "@auth/core",
        "@auth/nextjs",
        "@clerk/nextjs",
        "@clerk/clerk-sdk-node",
        "@clerk/backend",
        "@supabase/auth-helpers-nextjs",
        "@supabase/auth-ui-react",
        "@supabase/ssr",
        "@auth0/nextjs-auth0",
        "firebase-admin",
        "lucia",
        "iron-session",
        "next-session",
        "express-session",
        "passport",
        "bcrypt",
        "bcryptjs",
        "argon2",
        "jsonwebtoken",
    }
)

# Password-hashing CALLS in application code — the strongest content-level signal of a
# hand-rolled credential store (R14: "the app stores nothing that could be checked to
# let someone in"). Deliberately NOT a bare "password" text match, which would flag the
# accessor's own reference docs and any ordinary third-party-account-linking UI copy.
_CREDENTIAL_HASHING_RE: Final = re.compile(
    r"\b(?:bcrypt|argon2|scrypt)\s*\.\s*hash(?:Sync)?\s*\(", re.IGNORECASE
)
# A Drizzle column named for a raw credential — `text("password")`, `password_hash`, etc.
# Scoped to schema-shaped files by the caller so an unrelated string literal elsewhere
# (e.g. a UI label "Password") is never enough on its own.
_CREDENTIAL_COLUMN_RE: Final = re.compile(
    r"""["'`](password|password_?hash|credential_?hash)["'`]\s*[,)]""", re.IGNORECASE
)


def _package_json_dependencies(text: str) -> set[str]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names.update(section.keys())
    return names


def auth_surface_detected(files: Mapping[str, str]) -> list[str]:
    """The human-readable findings (R21) — empty when the tree carries no self-built
    auth surface. Pure: the whole heuristic, testable on a plain dict tree, mirroring
    `liveness_overpromises`'s shape."""
    findings: list[str] = []

    for path in sorted(files):
        if _AUTH_ROUTE_SEGMENT_RE.search(path):
            findings.append(f"a self-built auth route: {path}")

    for path, text in sorted(files.items()):
        if path.endswith("package.json"):
            forbidden = sorted(_package_json_dependencies(text) & _FORBIDDEN_DEPENDENCIES)
            findings.extend(f"a forbidden auth/credential dependency: {dep}" for dep in forbidden)

    for path, text in sorted(files.items()):
        if _CREDENTIAL_HASHING_RE.search(text):
            findings.append(f"password-hashing code: {path}")
        if path.endswith("schema.ts") and _CREDENTIAL_COLUMN_RE.search(text):
            findings.append(f"a credential column in the schema: {path}")

    return findings


def _untar(b64: str) -> dict[str, str]:
    data = base64.b64decode(b64)
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar:
            if len(files) >= _MAX_FILES:
                break
            if not member.isfile() or member.size > _MAX_FILE_BYTES:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            files[member.name] = handle.read().decode("utf-8", errors="replace")
    return files


@dataclass(frozen=True, slots=True)
class AuthSurfaceScan:
    """WHETHER WE LOOKED, separately from WHAT WE FOUND (issue #92, R21).

    This used to be a bare `list[str]`, and its own docstring named the problem it then
    reintroduced: an infrastructure hiccup "must not be indistinguishable from 'we scanned a
    clean tree' at the CALLER" — but an empty list is exactly that. `manager.py` branches on
    `if findings:`, so a wedged supervisor and a genuinely clean app took the same path, and a
    build that had grown NextAuth shipped green whenever the collect exec happened to fail. The
    only trace was a log line no build reads.

    So the two facts travel separately now. `scanned=False` means the tree could not be read
    and `findings` proves NOTHING — never that the app is clean.
    """

    findings: list[str]
    scanned: bool


async def check_auth_surface(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    *,
    app_id: uuid.UUID,
    session_id: uuid.UUID,
) -> AuthSurfaceScan:
    """Run the detector against the live workspace.

    Collection stays BEST-EFFORT — a wedged supervisor or a torn tar stream does not fail an
    otherwise-good build on this check's own infrastructure hiccup, the trade-off the module
    docstring argues for. What changed is that the caller can now SEE that outcome
    (`scanned=False`) instead of receiving something shaped exactly like a clean scan. The
    gate itself is unchanged and still hard: a real finding fails the build (R21).
    """
    run_command = sandbox_client.exec
    try:
        result = await run_command(
            handle, ["sh", "-c", _COLLECT_SCRIPT], timeout_s=_COLLECT_TIMEOUT_SECONDS
        )
        if result.exit != 0 or not result.stdout.strip():
            _log.warning(
                "auth-surface check: workspace collect failed",
                app_id=str(app_id),
                session_id=str(session_id),
                exit=result.exit,
            )
            return AuthSurfaceScan(findings=[], scanned=False)
        findings = auth_surface_detected(_untar(result.stdout))
    except Exception:
        _log.exception(
            "auth-surface check failed to run; the tree was NOT scanned",
            app_id=str(app_id),
            session_id=str(session_id),
        )
        return AuthSurfaceScan(findings=[], scanned=False)
    if findings:
        _log.warning(
            "generated app grew its own authentication surface (issue #92, R21)",
            app_id=str(app_id),
            session_id=str(session_id),
            findings=findings,
        )
    return AuthSurfaceScan(findings=findings, scanned=True)
