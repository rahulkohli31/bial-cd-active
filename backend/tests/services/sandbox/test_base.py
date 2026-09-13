"""Frozen-surface tests for the C2 sandbox-client ABC. The @abstractmethod set is a
cross-track contract (drift breaks SESSION-API/BRAIN), so it is pinned here; the
handle is asserted immutable, the exceptions asserted correctly rooted, and the
FileOp union asserted to discriminate on `action`.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from src.services.sandbox.base import (
    DevLogs,
    DevStatus,
    ExecResult,
    FileCreate,
    FileCreateBytes,
    FileInsert,
    FileOp,
    FileResult,
    FileStrReplace,
    FileView,
    SandboxClient,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
    SandboxNotReadyError,
)

# The frozen C2 method set. A test failure here means base.py drifted from the
# contract (a method added/removed/renamed) — a cross-track break.
_C2_METHODS = {
    "provision_new",
    "wait_ready",
    "attach_existing",
    "restore_from_snapshot",
    "exec",
    "files",
    "dev_start",
    "dev_status",
    "dev_logs",
    "teardown",
}


def test_client_is_abstract_and_cannot_be_instantiated() -> None:
    assert inspect.isabstract(SandboxClient)
    # Direct construction raises TypeError (every method is abstract) — SESSION-API
    # MUST implement all of them. `Any` dodges the abstract-instantiation diagnostic
    # while still exercising the runtime behaviour.
    cls: Any = SandboxClient
    with pytest.raises(TypeError):
        cls()


def test_abstractmethod_set_equals_the_pinned_contract() -> None:
    assert set(SandboxClient.__abstractmethods__) == _C2_METHODS


def test_handle_has_the_five_expected_fields_with_correct_types() -> None:
    handle = SandboxHandle(
        fqdn="app-xyz.westeurope.azurecontainerapps.io",
        token="tok",
        app_name="my-app",
        preview_url="https://app-xyz.westeurope.azurecontainerapps.io/",
        ready=False,
    )
    assert {f.name for f in dataclasses.fields(handle)} == {
        "fqdn",
        "token",
        "app_name",
        "preview_url",
        "ready",
    }
    assert handle.ready is False


def test_handle_is_frozen() -> None:
    handle = SandboxHandle(fqdn="f", token="t", app_name="a", preview_url="https://f/", ready=True)
    obj: Any = handle  # dodge the read-only-attr diagnostic; assert runtime frozen-ness
    with pytest.raises(dataclasses.FrozenInstanceError):
        obj.fqdn = "other"


def test_result_value_types_carry_the_expected_fields() -> None:
    assert {f.name for f in dataclasses.fields(ExecResult)} == {"stdout", "stderr", "exit"}
    assert {f.name for f in dataclasses.fields(DevStatus)} == {
        "running",
        "ready",
        # WHAT THE ROOT ANSWERED WITH, and it is a separate field from `ready` on purpose:
        # `ready` is fail-open and counts a 404, which is a dev server that is up with nothing
        # to show. Anything deciding whether to FRAME reads this one. See `DevStatus`.
        "root_status",
        "port",
        "exit_code",
    }
    assert {f.name for f in dataclasses.fields(DevLogs)} == {"lines", "next_cursor"}
    assert {f.name for f in dataclasses.fields(FileResult)} == {"ok", "detail"}


def test_exceptions_are_rooted_at_sandbox_error() -> None:
    assert issubclass(SandboxNotReadyError, SandboxError)
    assert issubclass(SandboxGoneError, SandboxError)
    assert issubclass(SandboxError, Exception)


# --- FileOp discriminated union -------------------------------------------------

_FILE_OP: TypeAdapter[FileOp] = TypeAdapter(FileOp)


def test_file_op_discriminates_each_action() -> None:
    assert isinstance(_FILE_OP.validate_python({"action": "view", "path": "a.tsx"}), FileView)
    assert isinstance(
        _FILE_OP.validate_python(
            {"action": "str_replace", "path": "a.tsx", "old_str": "x", "new_str": "y"}
        ),
        FileStrReplace,
    )
    assert isinstance(
        _FILE_OP.validate_python({"action": "create", "path": "a.tsx", "file_text": "z"}),
        FileCreate,
    )
    assert isinstance(
        _FILE_OP.validate_python(
            {"action": "insert", "path": "a.tsx", "insert_line": 1, "insert_text": "z"}
        ),
        FileInsert,
    )
    assert isinstance(
        _FILE_OP.validate_python(
            {"action": "create_bytes", "path": "att/book.xlsx", "file_b64": "AAEC"}
        ),
        FileCreateBytes,
    )


def test_create_bytes_is_a_separate_op_from_create() -> None:
    """The binary lane must not be reachable by accident.

    `create` writes through `write_text` and rewrites every CRLF to LF, which corrupts any
    binary carrying that byte pair — an Office file is a ZIP archive and carries it constantly.
    A separate action is what keeps the no-normalisation rule a property of the op a caller
    chose, so neither variant can be satisfied by the other's body.
    """
    with pytest.raises(ValidationError):
        _FILE_OP.validate_python({"action": "create_bytes", "path": "a.bin", "file_text": "z"})
    with pytest.raises(ValidationError):
        _FILE_OP.validate_python({"action": "create", "path": "a.bin", "file_b64": "AAEC"})


def test_file_op_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        _FILE_OP.validate_python({"action": "delete", "path": "a.tsx"})


def test_file_op_rejects_missing_action_subfield() -> None:
    # A str_replace with no old_str/new_str cannot be constructed (fail-first — the
    # per-variant required fields the flat C1 body could not enforce).
    with pytest.raises(ValidationError):
        _FILE_OP.validate_python({"action": "str_replace", "path": "a.tsx"})


# --- "would a citizen see a page?" ------------------------------------------------------------
#
# THE DISTINCTION THESE PIN, and it is the one a real build broke on 2026-09-10. `ready` is the
# supervisor's fail-open "something answered on the dev port" — 4xx and 5xx count, deliberately,
# so a compile error cannot wedge it False and mislead the model. A build spends its first seconds
# answering 404s because the agent has not written `app/page.tsx` yet: genuinely ready, nothing to
# show. Framing that window put a BLANK WHITE pane on screen under a live-preview label, which is
# worse than the black error page the whole branch exists to remove — that one at least had words.


def _dev(*, ready: bool = True, root_status: int | None = 200) -> DevStatus:
    # Explicit keywords, not a merged `dict[str, object]` unpacked into the constructor: ty reads
    # the merged dict's values as `object` and rejects them, and the mypy-coded ignore that hid it
    # locally is not one ty honours — it was the one red step on CI.
    return DevStatus(running=True, ready=ready, port=3000, root_status=root_status)


def test_a_page_is_a_page() -> None:
    assert _dev(root_status=200).shows_a_page is True


def test_a_redirect_off_the_root_is_still_a_page() -> None:
    """A 3xx is the app choosing where its first page lives, and the browser follows it. Only 4xx
    and 5xx mean the citizen is handed nothing."""
    assert _dev(root_status=302).shows_a_page is True
    assert _dev(root_status=399).shows_a_page is True


def test_the_line_is_drawn_between_399_and_400() -> None:
    """★ THE OFF-BY-ONE, pinned as an adjacent pair because that is the only way it is pinned.
    399 above and 404 below leave `< 400` and `<= 400` indistinguishable — the mutant survives the
    entire suite, and what it buys is a 400 Bad Request framed as a live preview: a page with no
    words on it, the same thing the citizen saw on 2026-09-10 and the same thing this predicate
    exists to refuse.

    400 is not a hypothetical status for an app root either. Agent-written middleware that
    rejects the request, or a route handler validating a search param, answers exactly one.

    Mutation check: `self.root_status < 400` -> `<= 400` and the second assertion here goes red
    while every other test in this file stays green."""
    assert _dev(root_status=399).shows_a_page is True
    assert _dev(root_status=400).shows_a_page is False


def test_a_ready_dev_server_with_nothing_to_show_is_not_a_page() -> None:
    """★ THE MEASURED DEFECT. `ready` is True and the root is 404 — the exact shape of a build
    between the dev server binding and the first route existing."""
    assert _dev(root_status=404).shows_a_page is False
    assert _dev(root_status=500).shows_a_page is False


def test_nothing_answering_is_never_a_page() -> None:
    assert _dev(ready=False, root_status=None).shows_a_page is False
    # Belt and braces: even a stale status cannot outvote `ready`.
    assert _dev(ready=False, root_status=200).shows_a_page is False


def test_a_supervisor_that_cannot_say_keeps_todays_behaviour() -> None:
    """★ THE ROLLOUT ARM. A container built before `root_status` existed answers `None`, and
    reading that as "no page" would refuse to frame the entire existing fleet — a false negative
    at fleet scale, which is worse than the window it closes. It self-expires as containers turn
    over. Delete this arm one fleet turnover after deploy, against this test."""
    assert _dev(root_status=None).shows_a_page is True
