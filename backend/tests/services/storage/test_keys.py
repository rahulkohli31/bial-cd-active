"""Security-load-bearing owner-scoping tests. Single-tenant isolation lives in the
`att/{user_id}/` key prefix, so these are attack-shaped: cross-owner access, the
trailing-slash boundary that defeats a prefix collision, the bare owner root that
is not itself an object, and the metadata charset round-trip both backends rely on.
"""

from __future__ import annotations

import re
import uuid

import pytest

from src.services.storage.errors import StorageError
from src.services.storage.keys import (
    app_file_key,
    assert_owned,
    attachment_key,
    container_name,
    normalize_metadata,
    normalize_metadata_key,
    owner_prefix,
    submission_key,
    submissions_prefix,
)

# Fixed UUIDs so the tests are deterministic. U1 and U2 are DISTINCT; the point of
# the boundary check is that no key under U2 ever passes for U1.
_U1 = uuid.UUID("019f1c00-0000-7000-8000-000000000001")
_U2 = uuid.UUID("019f1c00-0000-7000-8000-000000000002")
_ATT = uuid.UUID("019f1c00-0000-7000-8000-0000000000aa")
_APP = uuid.UUID("019f1c00-0000-7000-8000-0000000000bb")
_FILE = uuid.UUID("019f1c00-0000-7000-8000-0000000000cc")


# --- key builders ------------------------------------------------------------


def test_owner_prefix_has_trailing_slash() -> None:
    assert owner_prefix(_U1) == f"att/{_U1}/"


def test_attachment_key_is_owner_scoped() -> None:
    assert attachment_key(_U1, _ATT) == f"att/{_U1}/{_ATT}"


def test_app_file_key_uses_apps_namespace() -> None:
    assert app_file_key(_APP, _FILE) == f"apps/{_APP}/{_FILE}"


# --- container_name: per-app Blob container (C9 §6) ---------------------------

# Azure container-name rules: 3–63 chars, lowercase letters/digits/hyphen, must start with a
# letter or digit, and no consecutive hyphens. The `app-{uuid}` form is 40 chars and can never
# form a `--`, so it is structurally valid for any UUID.
_AZURE_CONTAINER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]$")


def test_container_name_uses_app_prefix() -> None:
    assert container_name(_APP) == f"app-{_APP}"


def test_container_name_is_deterministic() -> None:
    assert container_name(_APP) == container_name(_APP)


def test_container_name_is_a_valid_azure_container_name() -> None:
    name = container_name(_APP)
    assert 3 <= len(name) <= 63
    assert name == name.lower()
    assert "--" not in name
    assert _AZURE_CONTAINER_RE.match(name) is not None


def test_container_name_valid_for_many_random_uuids() -> None:
    # A UUIDv7 (and any uuid4) always renders to the same charset, so the invariant holds for
    # every app id, not just the fixed fixture.
    for _ in range(50):
        assert _AZURE_CONTAINER_RE.match(container_name(uuid.uuid4())) is not None


# --- submission keys (APPROVAL R1/R2/R23) --------------------------------------


_SUB = uuid.UUID("019f1c00-0000-7000-8000-0000000000dd")
_APP2 = uuid.UUID("019f1c00-0000-7000-8000-0000000000ee")


def test_submission_key_shape() -> None:
    assert submission_key(_APP, _SUB) == f"submissions/{_APP}/{_SUB}.bundle"


def test_submissions_prefix_has_trailing_slash() -> None:
    # The trailing slash is the boundary the delete-path sweep (R23) relies on.
    assert submissions_prefix(_APP) == f"submissions/{_APP}/"
    assert submissions_prefix(_APP).endswith("/")


def test_submission_key_lives_under_its_apps_prefix() -> None:
    assert submission_key(_APP, _SUB).startswith(submissions_prefix(_APP))


def test_submission_prefixes_never_collide_across_apps() -> None:
    # Deleting app A must never sweep app B: distinct app ids yield disjoint,
    # non-prefix-overlapping namespaces (fixed-length UUIDs + the trailing slash).
    a, b = submissions_prefix(_APP), submissions_prefix(_APP2)
    assert a != b
    assert not a.startswith(b)
    assert not b.startswith(a)
    assert not submission_key(_APP2, _SUB).startswith(a)


# --- assert_owned: happy path ------------------------------------------------


def test_assert_owned_accepts_the_users_own_key() -> None:
    assert_owned(attachment_key(_U1, _ATT), _U1)  # no raise


# --- assert_owned: fail-closed -----------------------------------------------


def test_assert_owned_rejects_another_owners_key() -> None:
    # The canonical cross-user leak: a key minted for U2 must never pass for U1.
    with pytest.raises(StorageError):
        assert_owned(attachment_key(_U2, _ATT), _U1)


def test_assert_owned_rejects_bare_owner_root() -> None:
    # The bare `att/{user_id}/` prefix carries no object beyond it — not a valid
    # object key (the length check, not just the prefix, is what rejects it).
    with pytest.raises(StorageError):
        assert_owned(owner_prefix(_U1), _U1)


def test_assert_owned_rejects_prefix_without_trailing_slash() -> None:
    # `att/{user_id}` (no slash) must not pass: the trailing-slash boundary is
    # what stops one owner id being a prefix of another.
    with pytest.raises(StorageError):
        assert_owned(f"att/{_U1}", _U1)


def test_assert_owned_rejects_app_file_key_for_a_user() -> None:
    # App-file keys live under `apps/`, not `att/{user_id}/` — an owner check on
    # one must fail closed.
    with pytest.raises(StorageError):
        assert_owned(app_file_key(_APP, _FILE), _U1)


# --- metadata key normalization ----------------------------------------------


def test_metadata_key_lowercased() -> None:
    assert normalize_metadata_key("RunId") == "runid"


def test_metadata_key_underscore_ok() -> None:
    assert normalize_metadata_key("run_id_2") == "run_id_2"


@pytest.mark.parametrize(
    "bad_key", ["has-hyphen", "1starts_with_digit", "has space", "dot.key", ""]
)
def test_metadata_key_invalid_charset_rejected(bad_key: str) -> None:
    with pytest.raises(StorageError):
        normalize_metadata_key(bad_key)


def test_normalize_metadata_roundtrips_and_rejects() -> None:
    assert normalize_metadata({"RunId": "abc", "Stage": "qa"}) == {"runid": "abc", "stage": "qa"}
    assert normalize_metadata(None) is None
    with pytest.raises(StorageError):
        normalize_metadata({"bad-key": "x"})
