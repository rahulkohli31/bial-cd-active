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
  GET  /dev/status                                -> {"running","ready","port","exit_code"}
  GET  /dev/logs?since=N                          -> {"lines":[..], "next": M}
  GET  /env/manifest              -> {"vars":[{name,description}]}  (names only, no values)

Injected secret values (the Blob SAS, the app credential, the per-project database DSN) are
REDACTED from every observable output surface — `/exec` stdout+stderr, each `/dev/logs` line, and
`/files` `view` — so a secret never rides the orchestrator's context (C9 §6.4). A URL-shaped
secret is registered BOTH whole and as its parsed password sub-token, since the scrub is a
substring replace of known values. This is the accidental-leak guard; the real isolation boundary
is container-scope + TTL (and, for the database, the `REVOKE CONNECT` wall), not redaction.

Written LF-only with pathlib to satisfy the ADR-0015 Windows-built-image rule.
"""

from __future__ import annotations

import collections
import http.client
import os
import pwd
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

# --- config (fail-fast: required settings have no defaults) --------------------------------
TOKEN = os.environ["SUPERVISOR_TOKEN"]
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace/app"))
APP_USER = os.environ.get("APP_USER", "appuser")
# The dev server's self-announcement. It no longer decides ANYTHING: "✓ Ready in <ms>" is printed
# once the server is listening, which is BEFORE the first route has compiled, so it announced a
# blank page. A served response is the authority now — see `dev_status`.
#
# `_Dev.ready` is therefore write-only, and that is deliberate rather than leftover: it is the
# handle the tests use to PROVE the marker cannot decide readiness (set it True, watch
# `/dev/status` still answer False — `test_dev_status_is_not_ready_while_the_route_is_still_compiling`).
# Delete the field and you delete the guard against the single most dangerous regression here,
# which is someone restoring the marker as a precondition and pinning `ready` False forever over
# a dev server the agent started itself. The marker LINE is still appended to `_Dev.lines`, so
# `/dev/logs` loses nothing either way.
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
# The SINGLE source of truth for the injected-env contract: (name, description, secret?). ACA
# injects these BIAL_* vars at provision — the ONLY BIAL_* the child may read. The three views below
# (child-env allowlist, /env/manifest, redaction set) are DERIVED from this table so they can't
# drift. Listed EXPLICITLY (not via a suffix rule) because several end in `_URL`, which a denylist
# would wrongly drop; the SAS is a bearer CAPABILITY, not an identity label (C9 §6). Add a row here
# or the child never sees the var (fail closed).
class InjectedEnvVar(NamedTuple):
    name: str
    description: str
    secret: bool  # True => the VALUE is a bearer credential, redacted from observable output.


_INJECTED_ENV: tuple[InjectedEnvVar, ...] = (
    InjectedEnvVar("BIAL_APP_ID", "the app's id", False),
    InjectedEnvVar("BIAL_PORTAL_ORIGIN", "the portal origin (preview framing / error relay)", False),
    InjectedEnvVar("BIAL_BLOB_CONTAINER_URL", "the app's per-app Blob container URL", False),
    InjectedEnvVar("BIAL_BLOB_SAS", "the container-scoped SAS (secret — never printed)", True),
    InjectedEnvVar(
        "BIAL_DATABASE_URL",
        "the app's own PostgreSQL connection string (secret, server-only — never printed)",
        True,
    ),
)
# The child-env allowlist: exactly the injected names, carried through the fail-closed scrub.
_BIAL_INJECTED_KEYS = tuple(v.name for v in _INJECTED_ENV)
# The names whose VALUES are secret bearer credentials — redacted from observable output.
_SECRET_ENV_NAMES = tuple(v.name for v in _INJECTED_ENV if v.secret)
# A shorter value can't be a real SAS/credential; redacting it would blank ordinary text (KTD-8).
_MIN_SECRET_LEN = 8

app = FastAPI(title="bial-sandbox-spike-supervisor")


def _embedded_password(value: str) -> str | None:
    """The `password` sub-token of a URL-shaped secret, or None when there is not one.

    Deliberately total: `urlsplit` raises ValueError on some malformed inputs (a bad port, an
    unclosed IPv6 bracket) and this runs INSIDE the redactor, where a raise would fail the
    request it was supposed to be sanitizing. Anything unparseable simply contributes nothing.
    """
    try:
        return urlsplit(value).password
    except ValueError:
        return None


def _register_secret(out: list[str], value: str | None) -> None:
    """Add `value` and its URL-decoded form to the redaction set, honouring the length guard."""
    if not value or len(value) < _MIN_SECRET_LEN:
        return
    out.append(value)
    decoded = unquote(value)
    if decoded != value:
        out.append(decoded)


def _redaction_secrets() -> tuple[str, ...]:
    """The known secret values to strip from observable output, read from `os.environ` at call time
    (so a value injected after import is still covered). For each secret env var (non-empty, len >=
    `_MIN_SECRET_LEN`) we redact BOTH the raw value AND its `urllib.parse.unquote` (URL-decoded)
    form: the Azure SDK returns the SAS ALREADY percent-encoded, so the raw value is what a logged
    URL carries, while the decoded `sig=…+…=…` is what a layer that parsed the query emits. We do
    NOT add the double-encoded `quote()` form (it appears in no log). See KTD-8 / C1.

    A URL-shaped secret ALSO contributes its parsed password sub-token. `_redact` is a blind
    whole-VALUE substring replace, so the two registrations cover different leaks and neither is
    redundant: registering only the whole `BIAL_DATABASE_URL` lets a line that prints just the
    password (a libpq/`pg` auth error, a `console.log(cfg.password)`) sail straight through, while
    registering only the password lets a printed DSN leak its host, database and role name. Both,
    or the guard has a hole (ADR-0028 / D18)."""
    out: list[str] = []
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if not value:
            continue
        _register_secret(out, value)
        _register_secret(out, _embedded_password(value))
    return tuple(out)


def _redact(text: str, secrets: tuple[str, ...] | None = None) -> str:
    """Replace every known secret value with `***` via a plain substring `str.replace` —
    ReDoS-immune (a known-value replace, never a credential-shape regex; the lesson of the
    redaction solution doc). `secrets` is computed once per request so a multi-line call reuses it.
    """
    for secret in _redaction_secrets() if secrets is None else secrets:
        text = text.replace(secret, "***")
    return text


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A fail-closed environment for unprivileged children — an allowlist, not a denylist.

    Built from an EMPTY dict; only the allowlisted `next dev` / node runtime knobs are copied
    from the parent, then the injected `BIAL_*` vars, then the caller's `extra`. Denied by default:
    `SUPERVISOR_TOKEN`, Azure `IDENTITY_HEADER`, any `*_DSN` / `*_URL` / `*_PASSWORD` — none of
    which a suffix denylist would have caught.
    """
    env: dict[str, str] = {
        k: v
        for k, v in os.environ.items()
        if k in _ENV_ALLOW_NAMES or k.startswith(_ENV_ALLOW_PREFIXES)
    }
    # Carry the injected BIAL_* vars through the scrub (ACA injects them into the parent env).
    for k in _BIAL_INJECTED_KEYS:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    # The child runs as APP_USER — its HOME/USER must be the unprivileged account's, not root's.
    env["HOME"] = APP_HOME
    env["USER"] = APP_USER
    # No TTY ever reaches a demoted child, so an interactive CLI (drizzle-kit's rename-vs-create
    # disambiguation, npm/next confirmations) would otherwise block on stdin until the command's
    # timeout burns — the exact 600s hang the walkthrough QA hit. CI=1 makes well-behaved tools
    # refuse to prompt and fail fast. Set before `extra` so a caller can still override it.
    env["CI"] = "1"
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

_DEV_PORT = 3000


class _Dev:
    proc: subprocess.Popen[str] | None = None
    lines: collections.deque[str] = collections.deque(maxlen=_DEV_LOG_MAXLEN)
    total_lines: int = 0  # monotonic count of all lines appended — the absolute log cursor.
    ready: bool = False  # the stdout MARKER was seen; a record, NOT `/dev/status`'s answer.
    lock = threading.Lock()


# --- the readiness probe's budgets ----------------------------------------------------------
_READY_CONNECT_TIMEOUT = 1.0
"""Connect budget for `/dev/status`'s probe. Connection REFUSED — nothing bound — is the only
genuine negative, and on loopback it is instant; this only bounds the pathological unreachable
case. Kept SHORT and split from the read budget so "nothing is there" stays a fast answer."""

_READY_READ_TIMEOUT = 10.0
"""Read budget for `/dev/status`'s probe — sized for a real cold server-render, deliberately NOT
inherited from the 1.0s the `/dev/start` guard uses. A generated BIAL app server-renders against
its per-project Postgres, so a first root render routinely exceeds a second; at 1.0s such an app
could NEVER latch ready, `are_we_there_yet` would burn its whole budget and synthesize
`dev_not_ready_error()`, and the model would be told its app "hangs at startup" when it is merely
slow. 10s fits INSIDE the consumers' own budgets rather than blowing through them:
`are_we_there_yet` spends 30 polls x 1.0s (~30s, `orchestrator/constants.py`) and the control
plane caps a single `/dev/status` request at 30s (`sandbox/client.py:_OP_TIMEOUT_SECONDS`), so one
slow render is confirmed well within the existing wait. A read TIMEOUT still counts as
not-serving — that is the compile window, and excluding it is the point of this probe."""

_STATUS_PROBE_WAIT = 2.0
"""How long `/dev/status` waits on a probe IT started before answering from what is known.
The probe keeps running and publishes to the cache, so a slower render is picked up by the next
poll instead of holding this one for the full read budget — the watchers poll every second, and a
generous READ budget must never become a generous RESPONSE time."""


def _dev_port_serving(
    port: int = _DEV_PORT, timeout: float = 1.0, read_timeout: float | None = None
) -> bool:
    """True when something ANSWERS HTTP on the dev port — observed truth, not child state.

    The child/marker state only knows about the supervisor's OWN child; a dev server the agent
    relaunched itself (`pkill` + `nohup`) is invisible to it forever, and the preview never
    frames over a live app. So a response — from whoever — is what counts.

    `timeout` bounds the CONNECT and `read_timeout` (default: `timeout`) bounds the wait for the
    response, because the two callers need different budgets: `/dev/start`'s double-spawn guard
    only asks "is anything already there?" and keeps the opportunistic 1.0s, while `/dev/status`
    must wait out a real cold server-render before calling the app not-ready.

    ANY HTTP response counts as serving, INCLUDING a 4xx/5xx — a 500-ing dev server is still
    serving, and its brokenness is the app's business, not the supervisor's. That fail-open is
    load-bearing, not incidental: without it a compile error would wedge `ready` False forever
    and the platform would tell the model its app "hangs at startup" instead of showing it the
    real error. Connection-refused (nothing bound) and a read timeout (bound, but nothing
    answered yet — the compile window) both count as NOT serving.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.connect()  # refused/unreachable within `timeout` -> not serving
        if conn.sock is not None:
            conn.sock.settimeout(timeout if read_timeout is None else read_timeout)
        conn.request("GET", "/")
        # Response HEADERS are the bar: `next dev` withholds them for the whole compile and
        # sends them once the route actually renders, so this is "a request succeeded" without
        # waiting on a streamed body.
        conn.getresponse()
        return True
    except (OSError, http.client.HTTPException):
        # ConnectionRefusedError / TimeoutError (both OSError) and a non-HTTP reply. An HTTP
        # ERROR STATUS is NOT here — it returned True above, which is the fail-open guard.
        return False
    finally:
        conn.close()


_READY_CACHE_TTL = 5.0
"""How long an affirmative may be trusted before it must be re-earned.

The cache exists so two 1s watchers plus `are_we_there_yet` do not cost three loopback requests
a second against a healthy app. It EXPIRES, rather than merely being invalidated at every reset
point we can name, because an enumeration is only ever as good as its completeness — and this
supervisor keeps growing ways for a dev server to appear and vanish (the open-sandbox
`run_command` surface lets the agent `pkill` our child and `nohup` its own replacement, which is
precisely why the probe answers for servers we do not own). One such server latching the
affirmative and then dying left `/dev/status` reporting `ready: true` over a dead app forever,
with no event remaining that could clear it. A deadline makes that state unrepresentable; the
explicit resets below stay, for promptness rather than for correctness.

5s bounds the lie at roughly five watcher polls while still absorbing ~5 of every 6 of them."""


class _Ready:
    """`/dev/status`'s readiness cache and single-flight probe guard.

    `served_until` is the CACHED AFFIRMATIVE, held as a monotonic DEADLINE rather than a bool so
    it cannot outlive the thing it describes (see `_READY_CACHE_TTL`). Nothing is ever cached
    negatively — a not-yet-ready server must be re-probed on the next poll.

    It lives HERE rather than inside `_dev_port_serving`, which is also `/dev/start`'s
    refuse-to-double-spawn guard: a cache there would let the guard read a stale True after the
    server died, `/dev/start` would 409 forever, and `selfheal.verify`'s dead-child rescue could
    never restart it.

    `generation` disowns the answer of a probe that was already in flight when readiness reset,
    so a restarted server can never inherit the previous server's `ready`.
    """

    lock = threading.Lock()
    served_until: float = 0.0  # monotonic deadline; anything <= now means "no affirmative"
    generation: int = 0
    probing: threading.Event | None = None  # the in-flight probe's signal; None = idle
    mourned: object | None = None  # the child whose death already invalidated the cache


def _forget_ready(mourned: object | None = None) -> None:
    """Drop the cached affirmative — called wherever readiness itself resets.

    Promptness, not correctness: `_READY_CACHE_TTL` already bounds how long a stale affirmative
    can survive, so a reset point this function does not know about costs seconds, never
    forever. `mourned` records the dead child so a poll loop invalidates ONCE, on the death
    transition — invalidating on every subsequent poll would re-probe every second on the
    dead-child-with-a-live-unowned-port path and never cache at all.
    """
    with _Ready.lock:
        if mourned is not None and _Ready.mourned is mourned:
            return
        _Ready.mourned = mourned
        _Ready.served_until = 0.0
        _Ready.generation += 1
        _Ready.probing = None


def _run_probe(done: threading.Event, generation: int) -> None:
    """Probe the dev port once, then publish — unless a reset has since disowned this answer."""
    serving = False
    try:
        serving = _dev_port_serving(_DEV_PORT, _READY_CONNECT_TIMEOUT, _READY_READ_TIMEOUT)
    finally:
        with _Ready.lock:
            if generation == _Ready.generation:  # a restart mid-probe discards the result
                if serving:
                    _Ready.served_until = time.monotonic() + _READY_CACHE_TTL
                _Ready.probing = None  # the single-flight slot is free again
        done.set()


def _dev_is_serving() -> bool:
    """Has a request to the app root actually been answered? — `/dev/status`'s `ready`.

    Single-flight, ONE ANSWER: only one probe runs at a time, and every caller — the one that
    started it and the ones that arrived while it was in flight — waits on that same probe for a
    bounded `_STATUS_PROBE_WAIT`. The watchers poll every second, so without the single flight a
    slow route would stack N concurrent requests against a dev server already busy compiling.

    The waiting matters as much as the flight. An earlier cut let a caller that found a probe
    running return immediately, on the theory that it would get "the last known answer" — but
    negatives are deliberately never cached, so that answer was an unconditional False. Two
    watchers at 1s against a 10s probe meant most polls in that window reported not-ready over a
    perfectly healthy app; paired with `running: false` (a dev server the agent started itself,
    which is exactly what this probe exists to see) `_watch_preview` reads that as a crash edge
    and re-mounts the citizen's iframe. A healthy app flapped.
    """
    done: threading.Event | None = None
    generation = 0
    i_started_it = False
    with _Ready.lock:
        if time.monotonic() < _Ready.served_until:
            return True  # a FRESH affirmative: no request, no probe.
        if _Ready.probing is None:
            done = _Ready.probing = threading.Event()
            generation = _Ready.generation
            i_started_it = True
        else:
            done = _Ready.probing  # somebody else is already asking; wait for THEIR answer
    if i_started_it and done is not None:
        try:
            threading.Thread(target=_run_probe, args=(done, generation), daemon=True).start()
        except BaseException:
            # A spawn that fails under memory pressure would otherwise leave the single-flight
            # slot occupied by a probe that will never run or clear it — and unlike a stale
            # affirmative, which `_READY_CACHE_TTL` bounds, that state is permanent: `ready`
            # would read False forever. Hand the slot back before the failure propagates.
            with _Ready.lock:
                if _Ready.probing is done:
                    _Ready.probing = None
            done.set()
            raise
    if done is not None:
        done.wait(_STATUS_PROBE_WAIT)
    with _Ready.lock:
        return time.monotonic() < _Ready.served_until


def _pump(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        with _Dev.lock:
            _Dev.lines.append(line.rstrip("\n"))  # deque drops the oldest past maxlen.
            _Dev.total_lines += 1
            if not _Dev.ready and any(m in line for m in READY_MARKERS):
                _Dev.ready = True


# --- the TTY escape hatch ------------------------------------------------------------------
# `stdin=DEVNULL` below is not enough, and we have the trace to prove it: told to run a bare
# `npx drizzle-kit generate`, which prompts, the agent worked around the closed stdin by
# MANUFACTURING a terminal — `python3 -c "import pty; pty.spawn([...])"` — and the command then
# sat at its prompt for 4 minutes 9 seconds until the timeout fired. A real TTY defeats every
# `isTTY` check we rely on, so the only place to stop it is here.
#
# A TARGETED DENYLIST, not a general shell filter. `run_command` is deliberately unrestricted in
# Write mode (that is the open-sandbox model), so the goal is narrow: refuse the handful of
# invocations whose ONLY purpose is to synthesize a terminal, and leave everything else alone.
_TTY_BINARIES = frozenset({"script", "expect", "unbuffer"})
"""argv[0] programs that exist to allocate a pty. `script -qec …` is the common form."""

_TTY_TOKENS = ("pty.spawn", "pty.fork", "pty.openpty", "os.openpty", "openpty(")
"""Source-level pty calls, matched inside an INLINE PROGRAM argument only (`-c`, `-e`, `--eval`).
Scoped that way on purpose: a file that merely mentions `openpty` — a test, a lockfile, a grep
pattern — must still be readable, so matching the whole argv would refuse ordinary work."""

_INLINE_PROGRAM_FLAGS = frozenset({"-c", "-e", "--eval", "--exec", "-p", "-E"})

_TTY_REFUSAL = (
    "This command allocates a terminal, which is not available here — nothing is watching it, "
    "so an interactive prompt would hang until the timeout. Run the command non-interactively "
    "instead: pass the flag that supplies the answer (for example `--name <what_changed>` for a "
    "migration), or set the tool's non-interactive/CI option."
)


def _refuse_a_manufactured_tty(cmd: list[str]) -> str | None:
    """The refusal message for a pty-manufacturing invocation, or None to let it run."""
    if not cmd:
        return None
    program = os.path.basename(cmd[0])
    if program in _TTY_BINARIES:
        return _TTY_REFUSAL
    # Only INLINE program text is scanned — `python3 -c "import pty; pty.spawn(...)"` is the
    # observed escalation; `grep -rn pty.spawn src/` is ordinary work and must still run.
    for index, token in enumerate(cmd[1:], start=1):
        if token in _INLINE_PROGRAM_FLAGS and index + 1 < len(cmd):
            program_text = cmd[index + 1]
            if any(marker in program_text for marker in _TTY_TOKENS):
                return _TTY_REFUSAL
    return None


# --- the dev-server kill hatch -------------------------------------------------------------
# The round-3 walkthrough recorded the failure this steers away from: the agent pkill'd the
# dev server the supervisor had started, nohup'd its own replacement, and the replacement died
# unwatched after the turn — a 502 preview in front of the client. The PROBE in /dev/status is
# the fix (an unowned restart is still observed serving); this denylist only removes the
# common path to the situation. Denylist weakness is accepted: `sh -c "kill …"`, `fuser` via a
# script, or Node's process.kill all slip it — steering, not security.
_KILL_BINARIES = frozenset({"kill", "pkill", "killall", "fuser"})
"""argv[0] programs whose purpose in this workspace is killing processes."""

_KILL_REFUSAL = (
    "The dev server is managed for you here: the platform starts and watches it, so nothing "
    "needs killing. If it looks stuck or unhealthy, keep working and report what you observed "
    "instead — its status is tracked automatically, and a replacement you start by hand is "
    "nobody's job to restart when it dies."
)


def _refuse_a_process_kill(cmd: list[str]) -> str | None:
    """The refusal message for a process-kill invocation, or None to let it run."""
    if cmd and os.path.basename(cmd[0]) in _KILL_BINARIES:
        return _KILL_REFUSAL
    return None


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
    refusal = _refuse_a_manufactured_tty(body.cmd) or _refuse_a_process_kill(body.cmd)
    if refusal is not None:
        # A NORMAL result, not an HTTP error: the caller is a model, and a 4xx becomes an
        # opaque tool failure it cannot learn from. Exit 1 plus a correctable message on
        # stderr is the shape it already knows how to read.
        return {"stdout": "", "stderr": refusal, "exit": 1}
    cwd = str(_resolve(body.cwd)) if body.cwd else str(WORKSPACE)
    try:
        r = subprocess.run(  # noqa: S603 - args are a list, no shell
            body.cmd,
            cwd=cwd,
            env=_child_env(),
            # Closed stdin (immediate EOF) is the belt to CI=1's suspenders: a CLI that probes
            # `process.stdin.isTTY` (drizzle-kit's prompt renderer does exactly this) sees no TTY
            # and fails fast instead of waiting on input that will never come.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=body.timeout,
            **_DEMOTE,  # type: ignore[arg-type]
        )
    except subprocess.TimeoutExpired as e:
        raise HTTPException(504, f"exec timed out after {body.timeout}s") from e
    # Redact any injected secret the command echoed (e.g. `printenv`, or a URL carrying the SAS).
    secrets = _redaction_secrets()
    return {
        "stdout": _redact(r.stdout, secrets),
        "stderr": _redact(r.stderr, secrets),
        "exit": r.returncode,
    }


@app.post("/files", dependencies=[Depends(_auth)])
def files(body: FilesBody) -> dict[str, Any]:
    p = _resolve(body.path)
    if body.action == "view":
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        start, end = 1, len(lines)
        if body.view_range:
            start, end = body.view_range
            if end == -1:  # C2/C7 tool promise: -1 = end of file (not an empty range)
                end = len(lines)
            start = max(1, start)
        numbered = "\n".join(
            f"{i}\t{lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1)
        )
        # Redact any injected secret a viewed file happens to contain (e.g. a stray .env.local).
        return {"ok": True, "content": _redact(numbered)}

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
    # An unowned server holding the port would NOT make this spawn fail: Next 13.4+ falls
    # back to the next free port and still prints "Ready in", minting a sticky marker-`ready`
    # child that Caddy never proxies — the same false-ready class the probe exists to kill.
    if _dev_port_serving():
        raise HTTPException(409, "something is already serving on the dev port")
    with _Dev.lock:
        _Dev.lines = collections.deque(maxlen=_DEV_LOG_MAXLEN)
        _Dev.total_lines = 0
        _Dev.ready = False
    # Readiness resets with the marker: the server about to be spawned has served nothing yet,
    # and a cached affirmative from its predecessor would report THAT server's `ready` while
    # this one is still compiling. `selfheal.verify`'s dead-child rescue restarts the dev server
    # mid-verify, so this path is walked at runtime, not just at provision.
    _forget_ready()
    cwd = str(_resolve(body.cwd)) if body.cwd else str(WORKSPACE)
    proc = subprocess.Popen(  # noqa: S603
        body.cmd,
        cwd=cwd,
        env=_child_env({"PORT": "3000", "HOST": "0.0.0.0"}),
        stdin=subprocess.DEVNULL,
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
    proc = _Dev.proc
    # `exit_code` is the child's post-mortem: None while alive (or never started), the exit
    # status once dead (137 = SIGKILL, the OOM-killer's signature). Surfaced so the platform
    # can report WHY the dev server died instead of misdiagnosing a dead process as an app
    # rendering bug — the 2026-07-30 calculator build burned 3 repair runs on that misread.
    exit_code = proc.poll() if proc else None
    running = bool(proc and exit_code is None)
    if proc is not None and exit_code is not None:
        # The child just died: whatever answered the last probe may have BEEN it, so the cached
        # affirmative cannot outlive it. Once only — `_forget_ready` remembers this corpse.
        _forget_ready(proc)
    # `ready` means A REQUEST ACTUALLY SUCCEEDED. It used to be
    # `(_Dev.ready and running) or _dev_port_serving()`, and `or` short-circuits, so on the
    # healthy owned path the probe NEVER ran and `ready` meant only "the child printed its
    # marker" — which `next dev` does as soon as it is listening, before the first route
    # compiles. Every consumer (`wait_ready`, both `_watch_preview`s, `are_we_there_yet`)
    # believed a still-compiling app was up, and the preview framed a blank page.
    #
    # The marker cannot GATE this either, only the cache may skip its cost: `/dev/start` is not
    # the only way a dev server comes to exist — the agent has been observed pkill-ing the child
    # and nohup-ing its own replacement — so a marker precondition would pin `ready` False over
    # a live app forever. A served response is the sole authority; `running` stays
    # child-process truth, which is what tells the rescue path a dead child is genuinely down.
    ready = _dev_is_serving()
    return {"running": running, "ready": ready, "port": _DEV_PORT, "exit_code": exit_code}


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
        # Redact any injected secret a dev-server log line printed (secrets computed once here).
        secrets = _redaction_secrets()
        lines = [_redact(line, secrets) for line in buffered[start:]]
        return {"lines": lines, "next": total}


@app.get("/env/manifest", dependencies=[Depends(_auth)])
def env_manifest() -> dict[str, Any]:
    # NAMES + descriptions only — NEVER values (the SAS value is redacted everywhere else, and is
    # absent here by construction). Derived from _INJECTED_ENV so it can never drift from the
    # allowlist. Documents the contract surface, so a name appears whether or not it is set.
    return {"vars": [{"name": v.name, "description": v.description} for v in _INJECTED_ENV]}
