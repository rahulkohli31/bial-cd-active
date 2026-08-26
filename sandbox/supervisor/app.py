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
  GET  /dev/compile   -> {"state","errors","reason","connect_generation"}   (R17/R18)
  GET  /served                                    -> {"served": N, "truncated": bool}   (R14)

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
import json
import os
import pwd
import re
import socket
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
# `/dev/status` still answer False —
# `test_dev_status_is_not_ready_while_the_route_is_still_compiling`).
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
# injects these BIAL_* vars at provision — the ONLY BIAL_* the child may read. The two views below
# (child-env allowlist, redaction set) are DERIVED from this table so they can't drift.
# `description` stays even though `/env/manifest` — its one reader — is gone (U29, dead code:
# nothing ever called it): the table remains the documented contract surface. Listed EXPLICITLY
# (not via a suffix rule) because several end in `_URL`, which a denylist would wrongly drop; the
# SAS is a bearer CAPABILITY, not an identity label (C9 §6). Add a row here or the child never
# sees the var (fail closed).
class InjectedEnvVar(NamedTuple):
    name: str
    description: str
    secret: bool  # True => the VALUE is a bearer credential, redacted from observable output.


_INJECTED_ENV: tuple[InjectedEnvVar, ...] = (
    InjectedEnvVar("BIAL_APP_ID", "the app's id", False),
    InjectedEnvVar(
        "BIAL_PORTAL_ORIGIN", "the portal origin (preview framing / error relay)", False
    ),
    InjectedEnvVar("BIAL_BLOB_CONTAINER_URL", "the app's per-app Blob container URL", False),
    InjectedEnvVar("BIAL_BLOB_SAS", "the container-scoped SAS (secret — never printed)", True),
    InjectedEnvVar(
        "BIAL_DATABASE_URL",
        "the app's own PostgreSQL connection string (secret, server-only — never printed)",
        True,
    ),
    InjectedEnvVar(
        "BIAL_BASE_PATH",
        "the path this app is served under, e.g. /a/sbx-<28 hex> (read by next.config.ts)",
        False,
    ),
    InjectedEnvVar(
        "BIAL_APPS_HOSTNAME",
        "the public hostname every generated app is served from (Server Actions origin)",
        False,
    ),
)
# The child-env allowlist: exactly the injected names, carried through the fail-closed scrub.
_BIAL_INJECTED_KEYS = tuple(v.name for v in _INJECTED_ENV)
# The names whose VALUES are secret bearer credentials — redacted from observable output.
_SECRET_ENV_NAMES = tuple(v.name for v in _INJECTED_ENV if v.secret)
# A shorter value can't be a real SAS/credential; redacting it would blank ordinary text (KTD-8).
_MIN_SECRET_LEN = 8

# The exact shape the router will actually route: `/a/` plus the container app's own name, which
# is `sbx-`/`pub-` and 28 lowercase hex. Validated here rather than trusted because this value
# reaches `next dev` as configuration and reaches `http.client` as a request target, and a
# malformed one would be a silent 404 in the first case and a header-injection attempt in the
# second. Anything that does not match is treated as ABSENT, which degrades to the pre-base-path
# behaviour — the app at `/` — rather than to a half-configured server.
_BASE_PATH_RE = re.compile(r"^/a/(?:sbx|pub)-[0-9a-f]{28}$")


def _base_path() -> str:
    """The app's assigned base path, or `""` when it is serving at the root.

    Read from `os.environ` at CALL time, not bound at import: the supervisor starts before the
    dev server and a restore re-injects the environment, so a value captured at import could
    describe a previous life of this container. Every caller is cheap and infrequent.
    """
    raw = os.environ.get("BIAL_BASE_PATH", "").strip()
    return raw if _BASE_PATH_RE.match(raw) else ""


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
"""Read budget for `/dev/status`'s probe — sized for a real cold server-render, and the reason
`/dev/start`'s guard does not read at all (it asks `_dev_port_bound`). A generated BIAL app
server-renders against its per-project Postgres, so a first root render routinely exceeds a
second; at 1.0s such an app could NEVER latch ready, `are_we_there_yet` would burn its whole
budget and synthesize `dev_not_ready_error()`, and the model would be told its app "hangs at
startup" when it is merely slow. 10s fits INSIDE the consumers' own budgets rather than blowing
through them:
`are_we_there_yet` spends 30 polls x 1.0s (~30s, `orchestrator/constants.py`) and the control
plane caps a single `/dev/status` request at 30s (`sandbox/client.py:_OP_TIMEOUT_SECONDS`), so one
slow render is confirmed well within the existing wait. A read TIMEOUT still counts as
not-serving — that is the compile window, and excluding it is the point of this probe."""

_STATUS_PROBE_WAIT = 2.0
"""How long `/dev/status` waits on a probe IT started before answering from what is known.
The probe keeps running and publishes to the cache, so a slower render is picked up by the next
poll instead of holding this one for the full read budget — the watchers poll every second, and a
generous READ budget must never become a generous RESPONSE time."""


def _abandon_socket(sock: socket.socket) -> None:
    """Tear a socket down out from under the thread blocked on it — the probe's deadline watchdog.

    SHUTDOWN, NOT CLOSE, and the difference is the whole fix. `socket.close()` only closes the
    real descriptor once every `makefile()` wrapper is gone (`_io_refs`), and `getresponse()`
    holds exactly such a wrapper for the entire header parse — so closing here returns
    immediately, changes nothing, and leaves the reader blocked on a live connection. `shutdown`
    goes straight to the kernel: the blocked read sees EOF and `http.client` raises, which the
    probe already classifies as not-serving.

    Errors are dropped on purpose and it is the narrowest possible swallow: this runs on a timer
    thread with nobody to report to, and the only failure mode is racing the probe's own
    `conn.close()` (`ENOTCONN` / `EBADF`) — i.e. the answer already arrived. A raise here would
    surface as an unhandled exception in a daemon thread guarding work that already succeeded."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _dev_port_bound(port: int = _DEV_PORT, timeout: float = _READY_CONNECT_TIMEOUT) -> bool:
    """Is the dev port OCCUPIED? A completed TCP connect, nothing sent, nothing awaited.

    THIS IS `/dev/start`'s QUESTION, and it is deliberately not `_dev_port_serving`'s. What makes
    a second `next dev` dangerous is a BOUND port, not an answering one: Next 13.4+ finds 3000
    taken, falls back to the next free port, prints "Ready in" and mints a child Caddy never
    proxies — and it does that whether or not the incumbent has finished compiling. Asking
    "did anything answer within a second?" therefore reads a mid-recompile server as absent and
    spawns the very duplicate the guard exists to prevent. U1's relaunch attach arm and U2's
    Write-turn boot-at-attach both call `/dev/start` against containers that are ALREADY running
    a dev server, so that window is now walked routinely rather than exotically — and two
    Turbopack processes in a memory-capped ACA container is how `exit_code 137` gets into the
    logs.

    Connection REFUSED is the only genuine negative and on loopback it is instant, so the budget
    here bounds nothing but the pathological unreachable case. Fails CLOSED on any other
    socket error: "I could not tell" must never authorize a spawn.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.connect()
    except OSError:
        return False  # refused (nothing bound) / unreachable within the budget
    else:
        return True
    finally:
        conn.close()


def _dev_port_serving(
    port: int = _DEV_PORT,
    timeout: float = 1.0,
    read_timeout: float | None = None,
    path: str | None = None,
) -> bool:
    """True when something ANSWERS HTTP on the dev port — observed truth, not child state.

    The child/marker state only knows about the supervisor's OWN child; a dev server the agent
    relaunched itself (`pkill` + `nohup`) is invisible to it forever, and the preview never
    frames over a live app. So a response — from whoever — is what counts.

    This is `/dev/status`'s authority alone. `/dev/start`'s double-spawn guard asks the cheaper,
    stricter question next door (`_dev_port_bound`) because the two need DIFFERENT answers, not
    merely different budgets: a bound-but-still-compiling port is "not serving" here and
    "occupied" there, and both readings are correct for their caller.

    `timeout` bounds the CONNECT and `read_timeout` (default: `timeout`) bounds the wait for the
    response — split so a cold server-render can be waited out without making "nothing is there"
    a slow answer.

    ANY HTTP response counts as serving, INCLUDING a 4xx/5xx — a 500-ing dev server is still
    serving, and its brokenness is the app's business, not the supervisor's. That fail-open is
    load-bearing, not incidental: without it a compile error would wedge `ready` False forever
    and the platform would tell the model its app "hangs at startup" instead of showing it the
    real error. Connection-refused (nothing bound) and a read timeout (bound, but nothing
    answered yet — the compile window) both count as NOT serving.

    THE READ BUDGET IS PER-RECV; THE TIMER IS THE TOTAL. `settimeout` re-arms on every socket
    operation, and `http.client` performs MANY of them parsing a status line and headers — so a
    peer that trickles one byte every half-second satisfies each read forever and this call never
    returns. That is not a hypothetical for code the citizen's own agent wrote: the response here
    comes from an unreviewed generated app. An unbounded probe pins the single-flight slot, so
    `/dev/status` goes on reporting not-ready over an app that has since become healthy — a
    permanent state, not a slow one. The watchdog below tears the socket down at the deadline
    (see `_abandon_socket` — shutdown, not close), and the resulting failure lands in the same
    not-serving arm as any other unreachable peer.

    WHAT IT ASKS FOR is `path`, defaulting to the app's assigned base path (see `_base_path`)
    and to `/` when there is none. THE FAIL-OPEN IS UNCHANGED — this widens WHAT is asked
    without touching WHETHER a non-answer is tolerated, which is the only reason it is safe to
    touch a function on the readiness chain at all: a wrongly-negative readiness leads to a
    recovery path that silently rolls the workspace back to the last Save. Leaving the request
    at `/` was the behavioural change, not moving it — under a base path `/` belongs to no
    route, so "ready" would decay to "the dev server can render its own 404", which is true
    before the citizen's first route has compiled and true forever for an app that never
    compiles. That is the exact false-ready this probe was rewritten to kill.

    NO TRAILING SLASH on the base path, and that is measured rather than stylistic: Next
    redirects `/<base>/` to `/<base>` with a 308, so the slashed form would answer with a
    redirect instead of the app and tell us nothing about whether the app renders.
    """
    return _dev_port_status(port, timeout, read_timeout, path) is not None


def _dev_port_status(
    port: int = _DEV_PORT,
    timeout: float = 1.0,
    read_timeout: float | None = None,
    path: str | None = None,
) -> int | None:
    """The HTTP status the dev port answered with, or None when nothing answered at all.

    This holds the mechanics `_dev_port_serving` used to hold inline; that function is now the
    one-line predicate over it, so the fail-open lives in exactly one place and reads as what it
    is — "an answer, any answer". The status is surfaced because ONE caller genuinely needs to
    tell a 404 from a 200 (`_detect_base_path_tampering`), and readiness deliberately still does
    not.
    """
    read_budget = timeout if read_timeout is None else read_timeout
    target = (_base_path() or "/") if path is None else path
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    watchdog: threading.Timer | None = None
    try:
        conn.connect()  # refused/unreachable within `timeout` -> not serving
        sock = conn.sock
        if sock is not None:
            sock.settimeout(read_budget)
            watchdog = threading.Timer(read_budget, _abandon_socket, args=(sock,))
            watchdog.daemon = True
            watchdog.start()
        conn.request("GET", target)
        # Response HEADERS are the bar: `next dev` withholds them for the whole compile and
        # sends them once the route actually renders, so this is "a request succeeded" without
        # waiting on a streamed body.
        return conn.getresponse().status
    except (OSError, http.client.HTTPException):
        # ConnectionRefusedError / TimeoutError (both OSError) and a non-HTTP reply — including
        # the watchdog's own close, which surfaces as one of these. An HTTP ERROR STATUS is NOT
        # here — it returned a status above, which is the fail-open guard.
        return None
    finally:
        if watchdog is not None:
            watchdog.cancel()  # the common case: the response arrived, nothing was abandoned
        conn.close()


# How much of the root response to read when checking whether the served base path is the one
# we injected. The answer is in the first framework asset URL, which Next emits in the document
# head — about 400 bytes in on the pinned template. 4 KiB is generous cover for a bigger head
# without ever reading an app's whole page into the supervisor.
_TAMPER_BODY_MAX = 4096
_TAMPER_TIMEOUT = 3.0

# The prefix a framework asset URL carries. Next generates every `/_next/…` URL under whatever
# `basePath` the CONFIG ACTUALLY HAS, which is what makes this readable rather than inferred:
# the served value names itself, so the signal can say what the app is doing instead of only
# that it is wrong.
_NEXT_ASSET_RE = re.compile(r"""["'](?P<prefix>[^"']*?)/_next/""")

_TAMPERED_REASON = "config_tampered"
"""The supervisor's word for "the app is not at the address the platform routes to".

It exists so this failure NAMES ITSELF. Without it a removed `basePath` presents to the control
plane as a preview that 404s, self-heal converts that into "make sure `app/page.tsx` exists",
and the model burns metered tokens repairing a file that was never wrong."""


def _served_base_path() -> str | None:
    """The base path the dev server is ACTUALLY generating URLs under, or None if unreadable.

    Read from the ROOT rather than from the configured path, because the root is the request
    whose answer differs between the two cases. Under a correct `basePath` the root is a 404
    whose body still carries prefixed asset URLs; with `basePath` removed the root is the app
    itself, carrying unprefixed ones. Either way the first `/_next/` reference names the truth.

    Returns None — never a guess — when nothing identifiable comes back, so an app that replaces
    the not-found page with plain text is reported as unknown rather than as tampered.
    """
    conn = http.client.HTTPConnection("127.0.0.1", _DEV_PORT, timeout=_TAMPER_TIMEOUT)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read(_TAMPER_BODY_MAX).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    match = _NEXT_ASSET_RE.search(body)
    return match.group("prefix") if match else None


def _detect_base_path_tampering() -> None:
    """Publish `config_tampered` when the app is not serving under its assigned path.

    Runs at most once per readiness generation and only once an app has actually answered, so it
    cannot fire against a server that is merely still compiling. It NEVER touches readiness:
    a tampered config still counts as serving, exactly as a 500-ing dev server does, because the
    alternative is a false not-ready — and a false not-ready reaches a recovery path that
    silently rolls the workspace back to the last Save. This reports; it does not judge.
    """
    injected = _base_path()
    if not injected:
        return  # no assigned path; there is nothing to be wrong about
    served = _served_base_path()
    if served is None or served == injected:
        return
    _forget_compile(_TAMPERED_REASON)


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
    # The generation whose base path has already been checked. Once per dev-server life: the
    # config cannot change without a restart, and a restart bumps `generation`.
    base_path_checked: int = -1


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
        # Re-arm the base-path check. `base_path_checked` holds a generation, and the new one is
        # by construction unequal, but resetting it explicitly keeps the latch's meaning local
        # rather than dependent on the counter never wrapping or being restored.
        _Ready.base_path_checked = -1


def _run_probe(done: threading.Event, generation: int) -> None:
    """Probe the dev port once, then publish — unless a reset has since disowned this answer."""
    serving = False
    check_base_path = False
    try:
        serving = _dev_port_serving(_DEV_PORT, _READY_CONNECT_TIMEOUT, _READY_READ_TIMEOUT)
    finally:
        with _Ready.lock:
            if generation == _Ready.generation:  # a restart mid-probe discards the result
                if serving:
                    _Ready.served_until = time.monotonic() + _READY_CACHE_TTL
                    if _Ready.base_path_checked != generation:
                        _Ready.base_path_checked = generation
                        check_base_path = True
                _Ready.probing = None  # the single-flight slot is free again
        done.set()
    # OFF THE READINESS PATH ON PURPOSE, in its own thread and after `done` is set. The check
    # costs a second request, and the single-flight slot is held for the whole of `_run_probe`
    # — so doing it inline would make every caller of `/dev/status` wait on a diagnostic. A
    # readiness answer must never be slower because of something that only reports.
    if check_base_path:
        threading.Thread(target=_detect_base_path_tampering, daemon=True).start()


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


# --- R17/R18: the dev server's own compile state, read off its HMR socket --------------------
#
# WHY A SOCKET AND NOT THE LOG TAIL. `/dev/logs` was the only compile signal the platform had,
# which means string-matching a human-formatted stream at whatever cadence the control plane
# happens to poll. The dev server already publishes the answer as structured frames on its own
# HMR websocket, and — the decisive property — it sends the CURRENT state immediately on connect,
# so a consumer attaching at any moment learns whether the app is broken RIGHT NOW. There is no
# missed-edge problem and no scrollback to re-read. Measured latency from a file write to the
# `built` frame is ~100ms.
#
# WHY IT MUST NEVER FAIL OPEN. The platform covers the preview frame while this reads `building`
# or `failed`, and uncovers it on `clean`. An absent signal read as clean would clear the cover
# over the very error screen the cover exists to hide. So "no idea" is a FIRST-CLASS value
# (`unknown`) and every failure lands on it: never connected, connection dropped, unparseable
# frame, library missing from the image, or the protocol renamed upstream.
#
# THE PROTOCOL IS UNVERSIONED AND INTERNAL, AND IT HAS ALREADY MOVED ONCE UNDER US.
#
# This constant read `/_next/webpack-hmr` until 2026-08-26, with a comment asserting that the
# name survived the move to Turbopack. It did not. Measured against the pinned `next@16.3.1`:
# `_next/webpack-hmr` appears in exactly one file in the published package — a Next 12 upgrade
# guide — while the live endpoint registered by `server/lib/router-server.js` and dialled by
# `client/dev/hot-reloader/app/web-socket.js` is `/_next/hmr`. A handshake against the old path
# times out; a handshake against the new one returns 101 and the first `sync` frame arrives in
# milliseconds. So the compile signal had been dark for the whole Next 16 line: the consumer
# never connected, every attempt landed in the `disconnected` arm, and compile state sat at
# `unknown` forever. Nothing in the logs said so, which is precisely the silence the canary
# below exists to break — and the canary could not fire either, because it only arms AFTER a
# SUCCESSFUL connect.
#
# THE LESSON, recorded here rather than in a commit message: a connect that never succeeds is
# invisible to a drift alarm that arms on connect. If this path is ever wrong again the symptom
# is silence, not an error, so the test that covers it must assert a REAL connection is made —
# not merely that the constant has some value.
#
# Parsing stays defensive — unknown verbs and missing fields are ignored rather than thrown —
# which on its own would make an upstream RENAME invisible: we would quietly receive nothing
# forever while reporting a clean app. `_HMR_CANARY_S` is what gives that teeth. It exploits the
# sync-on-connect property: a SUCCESSFUL connect that produces no recognised frame within the
# window is reported as `unknown` with a `reason` the control plane raises a pinned alarm on.
# That is the one signal that says the protocol moved.
#
# The frames observed on 16.3.1 carry their verb in `type` (`turbopack-connected`, `sync`,
# `isrManifest`) rather than in `action`; `_derive_compile` already reads whichever is present,
# which is why only the PATH needed correcting and not the vocabulary.
_HMR_PATH = "/_next/hmr"

_HMR_CONNECT_TIMEOUT = 3.0
"""Connect budget for one attempt at the HMR socket. Loopback: a refusal (nothing listening
yet) is instant, so this only bounds the pathological case."""

_HMR_RETRY_S = 1.0
"""Pause between connect attempts. The dev server is restarted by `/dev/start`, by self-heal's
dead-child rescue, and by the agent's own shell — so this thread reconnects forever rather than
being wired to any one of those events."""

_HMR_CANARY_S = 5.0
"""How long we may be owed a frame we understand before calling it protocol drift.

Armed at connect (the server sends `sync` within milliseconds, so five seconds of nothing there
is not slowness) and re-armed by any frame we do NOT recognise — traffic we cannot read is the
signal that the vocabulary moved. Never armed by quiet alone: an idle dev server sends nothing
for minutes and is perfectly healthy."""

_COMPILE_DEBOUNCE_S = 0.4
"""How long `clean` must hold before it is published. One edit produces several `built` frames
(client and server compilations land separately), with `building` in between — publishing each
one would flap the platform's cover down and up mid-change.

DELIBERATELY ASYMMETRIC: `building` and `failed` publish IMMEDIATELY and only `clean` waits.
The covering states are the safe ones, so they must be fast; the clearing state is the one that
can lie, so it is the one that has to settle. This is also the interval the portal's cover is
specified to clear within — it imports the value rather than keeping a second copy of it."""

_COMPILE_MAX_ERRORS = 5
_COMPILE_MAX_ERROR_CHARS = 4000
"""Bounds on what one compile failure may hand upward. The text ends up in a model prompt, so a
webpack error dump with a thousand frames must not become the turn's whole context."""

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class _Compile:
    """The derived compile state, published by the HMR consumer thread and read by `/dev/compile`.

    `connect_generation` counts SUCCESSFUL connects. It is on the wire so the control plane can
    raise the protocol-drift alarm exactly once per connect instead of once per poll — without it,
    a drifted protocol would emit an alarm every second for the life of the container.

    `pending` holds a `clean` that has not served its debounce yet. Settling happens on the READ
    (`_settle_locked`), not on a timer in the writer: the consumer thread is blocked in `recv()`
    for most of its life and cannot wake itself to publish, and a read-side promotion is exactly
    as prompt for a caller polling every second while staying deterministic under test."""

    lock = threading.Lock()
    state: str = "unknown"  # "building" | "clean" | "failed" | "unknown"
    errors: tuple[str, ...] = ()
    reason: str | None = "never_connected"  # why the state is `unknown`; None when it is not
    connect_generation: int = 0
    pending: tuple[str, tuple[str, ...]] | None = None
    pending_at: float = 0.0
    consumer_started: bool = False


def _strip_ansi(text: str) -> str:
    """HMR error payloads carry terminal colour. It reaches a model prompt and a JSON field —
    both of which render it as literal garbage — so it comes off here, at the source."""
    return _ANSI_RE.sub("", text)


def _error_text(item: Any, secrets: tuple[str, ...]) -> str:
    """One HMR error entry as plain text, REDACTED. The wire form is normally a preformatted
    string, but webpack-shaped object entries are a documented variant, so both are handled and
    anything else degrades to its repr rather than throwing inside the consumer thread.

    THE REDACTION IS NOT OPTIONAL, and it is the same rule `/exec`, `/dev/logs` and `/files`
    already follow (see this module's header): a compile error is dev-server output, and dev-server
    output is exactly as credential-shaped as a log line. A Next error that echoes a bad
    `BIAL_DATABASE_URL` — an unparseable DSN is a compile-time failure, not an exotic one — would
    otherwise carry the per-project database password out of the container, into the control
    plane, and on into a model prompt. The scrub is a substring replace of known values, so it
    catches the whole DSN and its parsed password sub-token both (C9 §6.4)."""
    if isinstance(item, str):
        text = item
    elif isinstance(item, dict):
        parts = [str(item[k]) for k in ("moduleName", "message") if isinstance(item.get(k), str)]
        text = "\n".join(parts) if parts else json.dumps(item, default=str)
    else:
        text = str(item)
    return _redact(_strip_ansi(text), secrets)[:_COMPILE_MAX_ERROR_CHARS]


def _derive_compile(msg: Any) -> tuple[str, tuple[str, ...]] | None:
    """Derive `(state, errors)` from one HMR frame, or None if this frame says nothing about
    compilation. None is the ONLY answer for an unrecognised frame — never a state — because
    an unknown verb must not be read as good news.

    The verb is taken from `action` OR `type`: the frames carry `action`, the surrounding
    protocol documentation says `type`, and reading whichever is present costs one line and
    removes a whole class of silent break."""
    if not isinstance(msg, dict):
        return None
    verb = msg.get("action")
    if not isinstance(verb, str):
        verb = msg.get("type")
    if not isinstance(verb, str):
        return None
    if verb == "building":
        return ("building", ())
    if verb in ("built", "sync"):
        raw = msg.get("errors")
        # Secrets resolved ONCE per frame rather than per error, exactly as `/dev/logs` does it.
        secrets = _redaction_secrets() if isinstance(raw, list) and raw else ()
        errors = (
            tuple(_error_text(e, secrets) for e in raw[:_COMPILE_MAX_ERRORS])
            if isinstance(raw, list)
            else ()
        )
        return ("failed", errors) if errors else ("clean", ())
    return None


def _publish_locked(state: str, errors: tuple[str, ...], reason: str | None = None) -> None:
    _Compile.state = state
    _Compile.errors = errors
    _Compile.reason = reason
    _Compile.pending = None


def _note_frame(state: str, errors: tuple[str, ...]) -> None:
    """Record a recognised frame. See `_COMPILE_DEBOUNCE_S` for why only `clean` waits."""
    with _Compile.lock:
        if state == "clean":
            _Compile.pending = (state, errors)
            _Compile.pending_at = time.monotonic()
            return
        _publish_locked(state, errors)


def _settle_locked(now: float) -> None:
    pending = _Compile.pending
    if pending is not None and now - _Compile.pending_at >= _COMPILE_DEBOUNCE_S:
        _publish_locked(pending[0], pending[1])


def _forget_compile(reason: str) -> None:
    """Drop to `unknown` with a stated reason — every path that loses the signal comes here."""
    with _Compile.lock:
        _publish_locked("unknown", (), reason)


def _consume_hmr() -> None:
    """Hold a connection to the dev server's HMR socket and publish what it says. Forever, and
    without ever raising: this runs in a daemon thread whose death would silently pin the state
    at whatever it last published, which is the one outcome worse than `unknown`."""
    try:
        from websockets.sync.client import connect
    except Exception:  # pragma: no cover - a broken image, not a runtime condition
        # The library ships with `uvicorn[standard]` and is pinned in the image. If it is
        # somehow absent the supervisor must still serve /exec and /dev/* — a module-level
        # import would have taken the whole container down for a diagnostic feature.
        _forget_compile("consumer_unavailable")
        return
    # THE SOCKET LIVES BEHIND THE BASE PATH TOO. `basePath` gates every route Next serves, the
    # dev endpoints included, so an app at `/a/sbx-<key>` answers the handshake only at
    # `/a/sbx-<key>/_next/hmr`. Resolved per attempt rather than captured once: the loop
    # reconnects forever across dev-server restarts and a restore can re-inject the environment
    # underneath it.
    url = f"ws://127.0.0.1:{_DEV_PORT}{_base_path()}{_HMR_PATH}"
    while True:
        try:
            with connect(url, open_timeout=_HMR_CONNECT_TIMEOUT, close_timeout=1.0) as ws:
                with _Compile.lock:
                    _Compile.connect_generation += 1
                    _publish_locked("unknown", (), "connected_no_frame_yet")
                # The canary is ARMED WHENEVER WE ARE OWED AN ANSWER, not just at connect.
                #
                # A one-shot latch was the obvious shape and it disarms the alarm for every case
                # except connect-time drift: one recognised frame set the timeout to None forever,
                # so a rename that lands mid-session — or a PARTIAL rename, where `sync` still
                # parses and `built` stops — would pin the state at whatever was last understood
                # and never fire. That is precisely the silent failure the alarm exists for.
                #
                # SILENCE ALONE IS NOT DRIFT. An idle dev server sends nothing for minutes and is
                # perfectly healthy, so the window is armed only while UNRECOGNISED frames have
                # arrived since the last recognised one. Traffic we cannot read is the signal;
                # quiet is not.
                owed_since: float | None = time.monotonic()
                while True:
                    timeout = (
                        None
                        if owed_since is None
                        else max(0.0, owed_since + _HMR_CANARY_S - time.monotonic())
                    )
                    try:
                        raw = ws.recv(timeout=timeout)
                    except TimeoutError:
                        # We were owed an answer and did not get one we understand.
                        owed_since = None
                        _forget_compile("no_recognised_frame")
                        continue
                    text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    derived = _derive_compile(msg)
                    if derived is None:
                        # Traffic we cannot read. Start owing an answer again (unless we already
                        # are), so a vocabulary that moves mid-session still trips the alarm.
                        if owed_since is None:
                            owed_since = time.monotonic()
                        continue
                    owed_since = None
                    _note_frame(*derived)
        except Exception:  # noqa: BLE001 - every transport failure is the same answer: unknown
            _forget_compile("disconnected")
        time.sleep(_HMR_RETRY_S)


def _ensure_hmr_consumer() -> None:
    """Start the consumer on first ask. Lazy rather than at import so nothing spawns a
    reconnect loop in a test process or in a container whose app never gets polled, and
    single-flight so the 1s control-plane cadence cannot stack threads."""
    with _Compile.lock:
        if _Compile.consumer_started:
            return
        _Compile.consumer_started = True
    try:
        threading.Thread(target=_consume_hmr, daemon=True, name="hmr-compile").start()
    except BaseException:
        # Hand the slot back, exactly as `_dev_is_serving` does: a spawn that failed under
        # memory pressure must not latch "started" for the life of the container.
        with _Compile.lock:
            _Compile.consumer_started = False
        raise


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
    "instead, and make the question unnecessary rather than trying to answer it: split the work "
    "so the tool has nothing ambiguous to ask about (for a migration, make ONE kind of schema "
    "change per generate), or set the tool's non-interactive/CI option."
)
"""U20 correction: this used to say "pass the flag that supplies the answer (for example
`--name <what_changed>`)". Measured against the pinned drizzle-kit 0.31.10, that is false —
`--name` names the output file and answers nothing. The rename resolver is an interactive select
NO flag answers, and under this sandbox's own conditions (stdin=DEVNULL, no TTY) it does not even
hang: it prints "Interactive prompts require a TTY terminal", writes no migration, and exits 0.
Teaching the agent a flag that cannot work sends it hunting for a longer flag list; teaching it to
avoid the ambiguity is the instruction that actually resolves the situation."""


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
    # OCCUPIED, not ANSWERING: the fallback is triggered by the bind, so a server that is merely
    # mid-recompile still owns the port. `/dev/status`'s answer-based probe would call that
    # absent and let the duplicate spawn — see `_dev_port_bound`.
    # `_DEV_PORT` passed explicitly, exactly as `_run_probe` does: a default argument is bound
    # once at def-time, so relying on it would read a port this module can no longer redirect.
    if _dev_port_bound(_DEV_PORT):
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
    # Same reasoning as `_forget_ready` above, for the compile signal: the server about to be
    # spawned has compiled nothing, and its predecessor's verdict describes a process that no
    # longer exists. The consumer reconnects on its own; this only stops the gap being narrated
    # with the dead server's answer.
    _forget_compile("dev_restarted")
    cwd = str(_resolve(body.cwd)) if body.cwd else str(WORKSPACE)
    proc = subprocess.Popen(  # noqa: S603
        body.cmd,
        cwd=cwd,
        # NEXT_PRIVATE_DISABLE_DEV_OVERLAY_UX (Next 16.3+, PR #94346) suppresses BOTH the compile
        # and the runtime error overlay (ASM15) — defence in depth behind plan one's portal cover
        # (U12) and client-error arm (U13), which is why this can't land before those do (a
        # runtime crash would otherwise go silent end-to-end). Set here, as a literal `extra`
        # entry rather than an image env var, so it's baked outside `/workspace/app`: the agent's
        # write surface (R19) never reaches it, and a restore can't overlay it away.
        env=_child_env(
            {"PORT": "3000", "HOST": "0.0.0.0", "NEXT_PRIVATE_DISABLE_DEV_OVERLAY_UX": "1"}
        ),
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


@app.get("/dev/compile", dependencies=[Depends(_auth)])
def dev_compile() -> dict[str, Any]:
    """Is the app currently compiling, compiled, or broken? — the signal the platform covers
    the preview frame with (R17/R18).

    `state` is one of `building` | `clean` | `failed` | `unknown`, and `unknown` is a real
    answer, not an error: the consumer has not connected yet, the socket is down between
    reconnects, or a successful connect produced nothing recognisable. `reason` names which.
    A caller must treat `unknown` as "hold whatever you are showing" — never as clean.

    `connect_generation` counts successful connects, so the control plane can raise the
    protocol-drift alarm once per connect rather than once per poll.

    Starting the consumer HERE, on first ask, is deliberate: nothing spawns a reconnect loop
    in a container the platform never polls, and the first answer is honestly `unknown`."""
    _ensure_hmr_consumer()
    with _Compile.lock:
        _settle_locked(time.monotonic())
        return {
            "state": _Compile.state,
            "errors": list(_Compile.errors),
            "reason": _Compile.reason,
            "connect_generation": _Compile.connect_generation,
        }


# --- R14: what the generated app actually served -----------------------------------------
#
# Caddy logs to this file from a SITE-LEVEL logger, and `/_sup/*` opts out of it with `log_skip`.
# The app block is therefore still the only thing recorded — but by exclusion, not because the
# control plane's requests reach a different handler. The distinction matters: the logger cannot
# be moved back inside `handle` (it is not an ordered HTTP handler, so Caddy refuses to start),
# and deleting `log_skip` as "redundant" would silently start counting the platform's own probes
# as user traffic. Counting these lines is how the control plane tells "a builder is clicking
# through their app" from "a container nobody has touched in an hour".
#
# WHY NOT AZURE'S `Requests` METRIC: our own FQDN health check enters through the same public
# ingress as a real user, so every idle container reports steady traffic forever. Azure cannot
# see the difference; Caddy can, because it sees the request line.
_SERVED_LOG = Path("/tmp/bial-served.log")  # noqa: S108 - the container's own tmpfs, root-owned
# Paths the platform itself calls. Excluded from the count, because a signal that includes our
# own probes says "in use" about every container forever — which is the exact failure mode of
# the Azure metric this endpoint exists to replace.
_CONTROL_PLANE_PATHS = ("/_sup/", "/__bial_probe")
# A ceiling on how much of the log one call will read. The file is roll-limited to 1 MiB, but a
# report path must be cheap and bounded no matter what is on disk.
_SERVED_TAIL_BYTES = 256 * 1024


def _served_request_count(raw: str) -> int:
    """Count app requests in a slice of Caddy's JSON access log.

    LINE-BY-LINE AND FORGIVING, deliberately: the slice starts mid-file so its first line is
    usually a fragment, a roll can truncate the last one, and neither is a reason to fail a
    liveness report. An unparseable line is not counted and not fatal.

    NOTHING FROM THE LOG IS RETURNED — only how many lines matched. The URIs in it belong to a
    generated app and may carry anything a citizen put in a query string."""
    served = 0
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        # `request` must be checked for SHAPE, not just presence. A line that parses as JSON but
        # whose `request` is not an object — a truncated write, or a future log schema — would
        # otherwise raise AttributeError here, and AttributeError is not ValueError, so it would
        # escape the `continue` above and 500 the whole /served call. That contradicts this
        # function's entire contract: one malformed line must cost one line, never the report.
        request = entry.get("request")
        uri = request.get("uri", "") if isinstance(request, dict) else ""
        if not isinstance(uri, str) or not uri.startswith("/"):
            continue
        if any(uri.startswith(prefix) for prefix in _CONTROL_PLANE_PATHS):
            continue
        served += 1
    return served


@app.get("/served", dependencies=[Depends(_auth)])
def served() -> dict[str, Any]:
    """How many requests the generated app has served, excluding control-plane probes (R14).

    A COUNT AND A TIMESTAMP, never a path. The control plane compares successive counts to decide
    whether anyone is using this app; it has no business knowing which pages they visited, and a
    generated app's URLs are citizen-authored input this process must not forward upward.

    PULL, NOT PUSH. The control plane asks during a reclamation pass rather than the sandbox
    calling out. That keeps the sandbox with no outbound credential, no new egress path and no
    knowledge of the control plane's address — and it means a sandbox cannot keep itself alive by
    talking, only by being used.

    A missing log file is `served: 0`, not an error: it is what a container that has never served
    a request looks like, and that is exactly the container this signal exists to identify."""
    try:
        size = _SERVED_LOG.stat().st_size
        with _SERVED_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            if size > _SERVED_TAIL_BYTES:
                fh.seek(size - _SERVED_TAIL_BYTES)
            raw = fh.read()
    except OSError:
        return {"served": 0, "truncated": False}
    return {"served": _served_request_count(raw), "truncated": size > _SERVED_TAIL_BYTES}
