"""Sandbox runtime service (contracts C2 / C4).

`SandboxConfig` (the typed config, U5) plus the frozen C2 surface (U7): the
`SandboxClient` ABC, the `SandboxHandle` value type, the typed `FileOp` request
union + result value types (`ExecResult` / `DevStatus` / `DevLogs` / `FileResult`),
and the typed exceptions. SESSION-API implements the ABC (Wave 1); BRAIN imports it
read-only. Public surface is re-exported explicitly (`X as X`) so ty / mypy
--strict / pyright read it as an intentional re-export — never an `__all__` list
(`.claude/rules/modules.md`).
"""

from __future__ import annotations

from src.services.sandbox.base import (
    DevLogs as DevLogs,
)
from src.services.sandbox.base import (
    DevStatus as DevStatus,
)
from src.services.sandbox.base import (
    ExecResult as ExecResult,
)
from src.services.sandbox.base import (
    FileCreate as FileCreate,
)
from src.services.sandbox.base import (
    FileInsert as FileInsert,
)
from src.services.sandbox.base import (
    FileOp as FileOp,
)
from src.services.sandbox.base import (
    FileResult as FileResult,
)
from src.services.sandbox.base import (
    FileStrReplace as FileStrReplace,
)
from src.services.sandbox.base import (
    FileView as FileView,
)
from src.services.sandbox.base import (
    SandboxClient as SandboxClient,
)
from src.services.sandbox.base import (
    SandboxError as SandboxError,
)
from src.services.sandbox.base import (
    SandboxGoneError as SandboxGoneError,
)
from src.services.sandbox.base import (
    SandboxHandle as SandboxHandle,
)
from src.services.sandbox.base import (
    SandboxNotReadyError as SandboxNotReadyError,
)
from src.services.sandbox.client import AcaSandboxClient as AcaSandboxClient
from src.services.sandbox.client import SandboxNotConfiguredError as SandboxNotConfiguredError
from src.services.sandbox.client import create_sandbox as create_sandbox
from src.services.sandbox.client import get_sandbox as get_sandbox
from src.services.sandbox.client import reset_sandbox_for_tests as reset_sandbox_for_tests
from src.services.sandbox.client import set_sandbox_for_tests as set_sandbox_for_tests
from src.services.sandbox.config import SandboxConfig as SandboxConfig
from src.services.sandbox.lifecycle import aclose_sandbox as aclose_sandbox
