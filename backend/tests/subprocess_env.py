"""The environment a child interpreter needs to START, and deliberately nothing else.

Several suites spawn a fresh interpreter to prove something about a cold import — that
`reaper` loads without the FastAPI app, that the worker profile builds from `os.environ`,
that a sample `.env` file boots valid settings. The point of each is that the child inherits
NOTHING from the parent, so they hand `subprocess.run` a hand-built `env=` instead of a copy
of `os.environ`.

That dict was `{"PATH": ..., "ENV_FILE": ...}`, which is POSIX-shaped. On Windows, Winsock
cannot initialise without `SystemRoot`, so the child dies in `import asyncio` →
`_overlapped` with `OSError: [WinError 10106] The requested service provider could not be
loaded or initialized` — before any project code is reached. Every one of these tests then
fails for a reason unrelated to what it asserts, and the failure looks like a broken local
checkout rather than a portability bug (it was misread as exactly that, twice).

Linux CI never saw it, because POSIX needs no such variable.

So this adds ONLY what the OS needs to get an interpreter running. `PATH` and the
project-level variables stay explicit at each call site, and on POSIX the result is
byte-identical to what those sites built by hand before.
"""

from __future__ import annotations

import os

# Windows: Winsock initialisation reads SystemRoot, and `asyncio` imports `_overlapped` at
# module scope, so anything importing sqlalchemy/asyncio needs it. POSIX requires nothing,
# so this is empty there and the child env stays exactly as minimal as it was.
_OS_REQUIRED: tuple[str, ...] = ("SystemRoot",) if os.name == "nt" else ()


def child_env(**overrides: str) -> dict[str, str]:
    """`PATH` + whatever the host OS needs to boot Python + the caller's own variables.

    Deliberately NOT a copy of `os.environ`: the isolation these tests rely on is the whole
    reason they build an env by hand. Pass the project variables as keyword arguments, e.g.
    `child_env(ENV_FILE=".env.test")`.
    """
    env = {"PATH": os.environ["PATH"]}
    env.update({name: os.environ[name] for name in _OS_REQUIRED if name in os.environ})
    env.update(overrides)
    return env
