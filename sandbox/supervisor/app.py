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

# Never leak these into a child process's environment.
_SECRET_ENV_KEYS = {"SUPERVISOR_TOKEN"}

app = FastAPI(title="bial-sandbox-spike-supervisor")


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A scrubbed environment for unprivileged children — no supervisor secrets."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _SECRET_ENV_KEYS and not k.endswith(("_TOKEN", "_SECRET", "_KEY"))
    }
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
class _Dev:
    proc: subprocess.Popen[str] | None = None
    lines: list[str] = []
    ready: bool = False
    lock = threading.Lock()


def _pump(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        with _Dev.lock:
            _Dev.lines.append(line.rstrip("\n"))
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
        _Dev.lines = []
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
        return {"lines": _Dev.lines[since:], "next": len(_Dev.lines)}
