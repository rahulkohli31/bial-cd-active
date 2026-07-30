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


def test_abstractmethod_set_equals_the_c2_contract() -> None:
    assert set(SandboxClient.__abstractmethods__) == _C2_METHODS


def test_handle_has_the_five_c2_fields_with_correct_types() -> None:
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


def test_result_value_types_carry_the_c2_fields() -> None:
    assert {f.name for f in dataclasses.fields(ExecResult)} == {"stdout", "stderr", "exit"}
    assert {f.name for f in dataclasses.fields(DevStatus)} == {
        "running",
        "ready",
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


def test_file_op_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        _FILE_OP.validate_python({"action": "delete", "path": "a.tsx"})


def test_file_op_rejects_missing_action_subfield() -> None:
    # A str_replace with no old_str/new_str cannot be constructed (fail-first — the
    # per-variant required fields the flat C1 body could not enforce).
    with pytest.raises(ValidationError):
        _FILE_OP.validate_python({"action": "str_replace", "path": "a.tsx"})
