"""Lake error hierarchy — one base carrying the coordinates the operator needs to tell two
indistinguishable failures apart.

THIS IS A DELIBERATE DEPARTURE FROM `services/storage/errors.py`, AND THE REASON MATTERS. The
storage backend's `raise_azure()` builds a fully STATIC message because its raw SDK text can
carry a SAS token or an account key, and a leaked message is a leaked credential. Nothing on a
lake path is a bearer credential: the coordinates are a URL and two identity identifiers, and the
credential is a managed identity Azure mints inside the container. So the account, the container
and the client id attempted may all appear on a raised error — and they are the ONLY thing that
tells a WRONG IDENTITY apart from a MISSING ROLE ASSIGNMENT, which fail with the same 403 and
whose confusion is this pass's central named risk.

Raw SDK text is still stripped, for the ordinary reason: it is unbounded, it is not ours, and it
has no audience. Every class ends in `Error` (N818).
"""

from __future__ import annotations


class LakeError(Exception):
    """Base for every connector-lake failure.

    `account`, `container` and `client_id` are diagnostic fields for logs and for the message —
    all three are labels, none is a credential. They are optional because a failure can happen
    before a coordinate is known (a configuration fault), and a `None` there is honest rather
    than a placeholder."""

    def __init__(
        self,
        message: str,
        *,
        account: str | None = None,
        container: str | None = None,
        client_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.account = account
        self.container = container
        self.client_id = client_id


class LakeAuthError(LakeError):
    """Azure refused the identity — a 403, or an explicit credential failure.

    THE ONE FAILURE WITH TWO INDISTINGUISHABLE CAUSES, which is why it has its own type and why
    its message names the identity that was used:

    1. The WRONG IDENTITY asked. `DefaultAzureCredential` reads the ambient `AZURE_CLIENT_ID`,
       which in these containers belongs to the platform's own identity — so a credential built
       without an explicit client id asks as somebody else and is correctly refused.
    2. The RIGHT identity asked and holds no `Storage Blob Data Reader` on the container.

    Azure returns the same status for both. The client id in the message is what settles it in
    one reading instead of a day spent in role assignments."""


class LakeNotFoundError(LakeError):
    """Azure NAMED the container or the file as absent.

    Only on Azure's own error code, never on a bare `ResourceNotFoundError` — the same rule
    `services/storage/azure_backend.py::_is_confirmed_absent` keeps, and for the same reason: a
    code-less 404 minted by a proxy between us and the account is a question that FAILED, not an
    answer about the object."""
