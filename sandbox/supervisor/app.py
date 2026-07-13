"""Minimal in-sandbox supervisor for the BIAL sandbox spike (ADR-0014 contract).

Exposes the supervisor HTTP API the production orchestrator will drive, guarded by a
per-session bearer token that lives ONLY in this (root) process's environment. Every child
it spawns (`npm`, `next dev`) runs as an unprivileged UID with a SCRUBBED environment, so the
untrusted generated app cannot read the token or drive the supervisor — the in-sandbox-RCE
closure ADR-0014 lines 47-52 require.

Routing: an in-container Caddy proxy fronts a single ACA ingress port (8080) and routes
`/_sup/*` here (127.0.0.1:9000) and everything else to `next dev` (127.0.0.1:3000, with
WebSocket upgrade for HMR).

Endpoints:
  GET  /health                                   -> {"ok": true}
  POST /exec       {cmd:[..], cwd?, timeout?}     -> {"stdout","stderr","exit"}
  POST /files      {action, path, ...}            -> view | str_replace | create | insert
  POST /dev/start  {cmd?:[..], cwd?}              -> {"pid": N}
  GET  /dev/status                                -> {"running","ready","port"}
  GET  /dev/logs?since=N                          -> {"lines":[..], "next": M}

Written LF-only with pathlib to satisfy the ADR-0015 Windows-built-image rule.
"""

from __future__ import annotations

import collections
import os
import pwd
import subprocess
import threading
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

# --- config (fail-fast: required settings have no defaults) --------------------------------
TOKEN = os.environ["SUPERVISOR_TOKEN"]
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace/app"))
APP_USER = os.environ.get("APP_USER", "appuser")
# Next 14/15 prints "✓ Ready in <ms>" only once it is actually serving — narrower than
# "Local:", which is printed as soon as the port is bound (before the first compile).
READY_MARKERS = ("Ready in",)

_pw = pwd.getpwnam(APP_USER)
APP_UID, APP_GID, APP_HOME = _pw.pw_uid, _pw.pw_gid, _pw.pw_dir

# --- child-env scrub: a FAIL-CLOSED allowlist ----------------------------------------------
# The child env is built from an EMPTY dict and copies ONLY these names/prefixes; everything
# else — SUPERVISOR_TOKEN, Azure's IDENTITY_HEADER, any injected *_DSN / *_URL / *_PASSWORD —
# is DENIED by default. A suffix denylist is fail-OPEN: it can't anticipate what the platform
# injects (`IDENTITY_HEADER` matches no `_TOKEN/_SECRET/_KEY` suffix; a `*_DSN` connection
# string sails straight through), so an allowlist is the only safe scrub for an untrusted child.
_ENV_ALLOW_NAMES = frozenset({"PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TERM", "PWD"})
_ENV_ALLOW_PREFIXES = ("LC_", "NODE_", "NEXT_", "CHOKIDAR_", "WATCHPACK_", "npm_")
# The four C9 app-identity vars ACA injects at provision — the ONLY BIAL_* the child may read.
# Listed explicitly (not via a suffix rule) because BIAL_DATA_BASE_URL ends in `_URL`, which a
# denylist would have wrongly dropped — one more reason the allowlist wins.
_BIAL_IDENTITY_KEYS = (
    "BIAL_APP_ID",
    "BIAL_APP_CREDENTIAL",
    "BIAL_DATA_BASE_URL",
    "BIAL_PORTAL_ORIGIN",
)

app = FastAPI(title="bial-sandbox-spike-supervisor")


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A fail-closed environment for unprivileged children — an allowlist, not a denylist.

    Built from an EMPTY dict; only the allowlisted `next dev` / node runtime knobs are copied
    from the parent, then the four C9 `BIAL_*` identity vars, then the caller's `extra`. Denied
    by default: `SUPERVISOR_TOKEN`, Azure `IDENTITY_HEADER`, any `*_DSN` / `*_URL` / `*_PASSWORD`
    — none of which a suffix denylist would have caught.
    """
    env: dict[str, str] = {
        k: v
        for k, v in os.environ.items()
        if k in _ENV_ALLOW_NAMES or k.startswith(_ENV_ALLOW_PREFIXES)
    }
    # Carry the C9 identity vars through the scrub (ACA injects them into the parent env).
    for k in _BIAL_IDENTITY_KEYS:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    # The child runs as APP_USER — its HOME/USER must be the unprivileged account's, not root's.
    env["HOME"] = APP_HOME
    env["USER"] = APP_USER
    if extra:
        env.update(extra)
    return env


# Children are demoted via subprocess's native user=/group=/extra_groups= (Python 3.9+),
# which sets the ids post-fork in C — no preexec_fn, so no multithreaded-fork deadlock — and
# extra_groups=[APP_GID] drops root's supplementary groups (incl gid 0) that a preexec_fn
# setuid/setgid would silently leave in place.
_DEMOTE: dict[str, object] = {"user": APP_UID, "group": APP_GID, "extra_groups": [APP_GID]}


def _resolve(path: str) -> Path:
    """Resolve a request path under WORKSPACE and refuse escapes."""
    p = (WORKSPACE / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if WORKSPACE.resolve() not in p.parents and p != WORKSPACE.resolve():
        raise HTTPException(400, f"path escapes workspace: {path}")
    return p


def _auth(authorization: str = Header(default="")) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad or missing bearer token")


# --- dev-server state ----------------------------------------------------------------------
# Cap the in-memory log ring so a chatty dev server can't OOM the supervisor. `lines` keeps only
# the newest _DEV_LOG_MAXLEN entries; `total_lines` counts EVERY line ever emitted so the /dev/logs
# `since`/`next` cursor stays a monotonic ABSOLUTE index, not a shifting list position.
_DEV_LOG_MAXLEN = 5000


class _Dev:
    proc: subprocess.Popen[str] | None = None
    lines: collections.deque[str] = collections.deque(maxlen=_DEV_LOG_MAXLEN)
    total_lines: int = 0  # monotonic count of all lines appended — the absolute log cursor.
    ready: bool = False
    lock = threading.Lock()


def _pump(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        with _Dev.lock:
            _Dev.lines.append(line.rstrip("\n"))  # deque drops the oldest past maxlen.
            _Dev.total_lines += 1
            if not _Dev.ready and any(m in line for m in READY_MARKERS):
                _Dev.ready = True


# --- models --------------------------------------------------------------------------------
class ExecBody(BaseModel):
    cmd: list[str]
    cwd: str | None = None
    timeout: int = 900


class FilesBody(BaseModel):
    action: str
    path: str
    old_str: str | None = None
    new_str: str | None = None
    file_text: str | None = None
    insert_line: int | None = None
    insert_text: str | None = None
    view_range: list[int] | None = None


class DevStartBody(BaseModel):
    cmd: list[str] = ["npm", "run", "dev"]
    cwd: str | None = None


# --- endpoints -----------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/exec", dependencies=[Depends(_auth)])
def exec_cmd(body: ExecBody) -> dict[str, Any]:
    cwd = str(_resolve(body.cwd)) if body.cwd else str(WORKSPACE)
    try:
        r = subprocess.run(  # noqa: S603 - args are a list, no shell
            body.cmd,
            cwd=cwd,
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=body.timeout,
            **_DEMOTE,  # type: ignore[arg-type]
        )
    except subprocess.TimeoutExpired as e:
        raise HTTPException(504, f"exec timed out after {body.timeout}s") from e
    return {"stdout": r.stdout, "stderr": r.stderr, "exit": r.returncode}


@app.post("/files", dependencies=[Depends(_auth)])
def files(body: FilesBody) -> dict[str, Any]:
    p = _resolve(body.path)
    if body.action == "view":
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        start, end = 1, len(lines)
        if body.view_range:
            start, end = body.view_range
        numbered = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))
        return {"ok": True, "content": numbered}

    if body.action == "str_replace":
        if body.old_str is None or body.new_str is None:
            raise HTTPException(400, "str_replace needs old_str and new_str")
        # Normalize to LF on both sides (CRLF has burned BIAL twice — ADR-0015).
        text = p.read_text(encoding="utf-8").replace("\r\n", "\n")
        old = body.old_str.replace("\r\n", "\n")
        new = body.new_str.replace("\r\n", "\n")
        count = text.count(old)
        if count == 0:
            raise HTTPException(422, "No match found for old_str")
        if count > 1:
            raise HTTPException(422, f"Found {count} matches for old_str; provide more context")
        p.write_text(text.replace(old, new), encoding="utf-8")
        return {"ok": True, "replacements": 1}

    if body.action == "create":
        if body.file_text is None:
            raise HTTPException(400, "create needs file_text")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.file_text.replace("\r\n", "\n"), encoding="utf-8")
        return {"ok": True, "created": str(p)}

    if body.action == "insert":
        if body.insert_line is None or body.insert_text is None:
            raise HTTPException(400, "insert needs insert_line and insert_text")
        lines = p.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        lines.insert(body.insert_line, body.insert_text)
        p.write_text("\n".join(lines), encoding="utf-8")
        return {"ok": True}

    raise HTTPException(400, f"unknown files action: {body.action}")


@app.post("/dev/start", dependencies=[Depends(_auth)])
def dev_start(body: DevStartBody) -> dict[str, Any]:
    with _Dev.lock:
        if _Dev.proc and _Dev.proc.poll() is None:
            raise HTTPException(409, "dev server already running")
        _Dev.lines = collections.deque(maxlen=_DEV_LOG_MAXLEN)
        _Dev.total_lines = 0
        _Dev.ready = False
    cwd = str(_resolve(body.cwd)) if body.cwd else str(WORKSPACE)
    proc = subprocess.Popen(  # noqa: S603
        body.cmd,
        cwd=cwd,
        env=_child_env({"PORT": "3000", "HOST": "0.0.0.0"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **_DEMOTE,  # type: ignore[arg-type]
    )
    _Dev.proc = proc
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    return {"pid": proc.pid}


@app.get("/dev/status", dependencies=[Depends(_auth)])
def dev_status() -> dict[str, Any]:
    running = bool(_Dev.proc and _Dev.proc.poll() is None)
    return {"running": running, "ready": _Dev.ready and running, "port": 3000}


@app.get("/dev/logs", dependencies=[Depends(_auth)])
def dev_logs(since: int = 0) -> dict[str, Any]:
    with _Dev.lock:
        total = _Dev.total_lines
        buffered = list(_Dev.lines)
        # Absolute index of the OLDEST line still retained (older ones were dropped by the ring).
        first_buffered = total - len(buffered)
        # Clamp `since` into the retained window: a cursor pointing at dropped lines resumes at
        # the oldest retained line; a caught-up cursor (since >= total) yields no lines. `next`
        # stays the absolute total, so the cursor only ever advances (contract preserved).
        start = max(0, since - first_buffered)
        return {"lines": buffered[start:], "next": total}
