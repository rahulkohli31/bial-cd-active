"""Bundle-header validation tests (APPROVAL D4, R3/R4/R5). The header is
attacker-writable (it came from a sandbox the citizen's AI drove), so these are
attack-shaped: oversized/control-char/non-hex tokens, base64-transport bytes,
truncation, and the bounded-prefix read — plus the one real-git round trip that
proves the parser agrees with `git rev-parse HEAD`."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from src.services.storage.bundle import BundleValidationError, parse_bundle_head_sha

_MAGIC = b"# v2 git bundle\n"


def _bundle_bytes(header_lines: list[bytes]) -> bytes:
    """Assemble magic + ref lines + the blank header terminator + fake packfile."""
    return _MAGIC + b"".join(line + b"\n" for line in header_lines) + b"\nPACKfakebytes"


# --- the real thing ------------------------------------------------------------


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[bytes, str]:
    """A bundle produced by actual `git bundle create`, plus its `git rev-parse
    HEAD` SHA — the exact artifact `write_snapshot` ships (current tree, HEAD only)."""
    repo = tmp_path_factory.mktemp("bundle-repo")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(repo),  # no user gitconfig leaks in
        "PATH": "/usr/bin:/bin",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    Path(repo, "app.txt").write_text("hello\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    git("bundle", "create", "app.bundle", "HEAD")
    sha = git("rev-parse", "HEAD")
    return Path(repo, "app.bundle").read_bytes(), sha


def test_real_bundle_parses_to_the_rev_parse_sha(real_bundle: tuple[bytes, str]) -> None:
    raw, expected_sha = real_bundle
    assert raw.startswith(b"# v")  # the pinned raw-bundle assertion (git-bundle doc §5)
    assert parse_bundle_head_sha(raw) == expected_sha


def test_base64_transport_shape_is_rejected(real_bundle: tuple[bytes, str]) -> None:
    # base64 is a supervisor-transport artifact, never a storage format (R5): a
    # bundle that arrives still encoded must be refused, not silently stored.
    raw, _ = real_bundle
    with pytest.raises(BundleValidationError):
        parse_bundle_head_sha(base64.b64encode(raw))


# --- malformed headers -----------------------------------------------------------


@pytest.mark.parametrize(
    "junk",
    [
        b"",  # empty
        b"# v2 git bund",  # truncated mid-magic
        b"arbitrary bytes that are not a bundle at all",
        b"# v3 git bundle\n@object-format=sha256\n",  # v3/SHA-256: not ours to accept
    ],
)
def test_non_bundles_raise_typed_error(junk: bytes) -> None:
    with pytest.raises(BundleValidationError):
        parse_bundle_head_sha(junk)


def test_header_without_head_ref_raises() -> None:
    # Documented behavior (not incidental): refs may appear in any order, but a
    # header that ENDS without a HEAD ref is refused.
    sha = b"a" * 40
    with pytest.raises(BundleValidationError):
        parse_bundle_head_sha(_bundle_bytes([sha + b" refs/heads/main"]))


def test_head_ref_found_after_other_refs_and_prerequisites() -> None:
    sha_main, sha_head, prereq = b"1" * 39 + b"a", b"2" * 39 + b"b", b"3" * 39 + b"c"
    data = _bundle_bytes(
        [b"-" + prereq + b" prerequisite", sha_main + b" refs/heads/main", sha_head + b" HEAD"]
    )
    assert parse_bundle_head_sha(data) == sha_head.decode()


@pytest.mark.parametrize(
    "token",
    [
        b"f" * 200,  # oversized — must not be truncated into String(40)
        b"F" * 40,  # uppercase hex: git emits lowercase; anything else is forged
        b"g" * 40,  # non-hex
        b"a" * 39,  # too short
        b"\x1b]0;pwned\x07" + b"a" * 29,  # control chars must never flow onward
    ],
)
def test_forged_head_tokens_raise_not_flow(token: bytes) -> None:
    with pytest.raises(BundleValidationError):
        parse_bundle_head_sha(_bundle_bytes([token + b" HEAD"]))


def test_multi_megabyte_header_is_bounded() -> None:
    # No newline for megabytes: the parser must fail fast off its bounded prefix,
    # never scan the whole blob looking for structure.
    with pytest.raises(BundleValidationError):
        parse_bundle_head_sha(_MAGIC + b"x" * (8 * 1024 * 1024))
    # And a HEAD line hidden BEYOND the bound is invisible by design.
    beyond = _MAGIC + b"-" + b"a" * 5000 + b" pad\n" + b"b" * 40 + b" HEAD\n\nPACK"
    with pytest.raises(BundleValidationError):
        parse_bundle_head_sha(beyond)
