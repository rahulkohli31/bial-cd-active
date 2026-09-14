"""A short, secret-safe name for an exception: its class chain, status, provider type and place.

For anywhere a failure must be identified without its message — a terminal row, a log line —
because exception text can carry bound SQL parameters, credentials, file content or a provider's
echo of the prompt. Imports nothing from `src.services`, so every layer and process may use it."""

from __future__ import annotations

import re
from typing import Final, cast

_ERROR_CHAIN_LIMIT: Final = 5
# The provider's error `type` (`overloaded_error`) is the one string in the signature that
# arrives from outside the process, so anything but a short lowercase token is dropped.
_PROVIDER_ERROR_TYPE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


def error_signature(exc: BaseException) -> str:
    """Class names along the cause chain, plus the HTTP status and the provider's error type token
    when the chain carries them, plus where it surfaced — never the message.

    `ModelAPIError <- APIStatusError status=200 type=overloaded_error at=module:function:line` is
    what a model stream that ended in an error event looks like."""
    names: list[str] = []
    status: int | None = None
    provider_type: str | None = None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(names) < _ERROR_CHAIN_LIMIT:
        seen.add(id(current))
        names.append(type(current).__name__)
        code: object = getattr(current, "status_code", None)
        if status is None and isinstance(code, int):
            status = code
        body: object = getattr(current, "body", None)
        if provider_type is None and isinstance(body, dict):
            error = cast("dict[str, object]", body).get("error")
            if isinstance(error, dict):
                token = cast("dict[str, object]", error).get("type")
                if isinstance(token, str) and _PROVIDER_ERROR_TYPE.fullmatch(token):
                    provider_type = token
        current = current.__cause__ or current.__context__
    signature = " <- ".join(names)
    if status is not None:
        signature += f" status={status}"
    if provider_type is not None:
        signature += f" type={provider_type}"
    site = raise_site(exc)
    if site is not None:
        signature += f" at={site}"
    return signature


def raise_site(exc: BaseException) -> str | None:
    """Where `exc` surfaced, as `module:function:line`: the innermost frame in the platform's own
    code when the traceback has one, otherwise the innermost frame at all. A place in the code
    carries no data, so it is safe wherever the signature goes."""
    site: str | None = None
    tb = exc.__traceback__
    while tb is not None:
        module = str(tb.tb_frame.f_globals.get("__name__", "?"))
        here = f"{module}:{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}"
        if site is None or module.startswith("src.") or not site.startswith("src."):
            site = here
        tb = tb.tb_next
    return site
