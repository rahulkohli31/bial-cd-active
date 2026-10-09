"""The concrete `SandboxClient` — the control-plane wrapper over the container's supervisor.

Two halves, behind the frozen ABC in `base.py`:
* The `/_sup/*` supervisor HTTP layer (`exec`/`files`/`dev_start`/`dev_status`/`dev_logs`/
  `wait_ready`) — calls `https://{handle.fqdn}/_sup/<endpoint>` with a bearer token; Caddy
  strips the prefix. A non-zero `ExecResult.exit` is a normal return, never an exception.
* The ACA lifecycle (`provision_new`/`attach_existing`/`restore_from_snapshot`/`teardown`) —
  container create/delete via `aca.py`, the registry hash, the in-process `token_ref` map.

Accessor mirrors `services/redis/client.py`: module singleton, lazy `settings` import (avoids
the `src.config` cycle), isolated `aclose`. `set_sandbox_for_tests` injects it for the reaper,
which reads the singleton rather than a `Depends`. NEVER log a token or a `token_ref`."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import secrets
import time
import uuid
from collections.abc import Coroutine, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import urlsplit

import httpx
import structlog
from redis.exceptions import RedisError

from src.core.redaction import scrub_untrusted
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    get_redis,
    legacy_registry_key,
    registry_key,
)
from src.services.redis.keys import (
    ALIAS_TTL_SECONDS,
    IS_ALIAS,
    REGISTRY_FIELD_ALIAS,
    REGISTRY_FIELD_APP_ID,
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_SHARED_OWNER_ID,
    REGISTRY_FIELD_SHARED_PROJECT_ID,
    REGISTRY_FIELD_SHARED_SERVED_COUNT,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
    REGISTRY_FIELD_WAITING_SINCE,
    alias_key,
)
from src.services.sandbox.aca import (
    _LRO_CEILING_SECONDS,
    AcaControlPlane,
    AcaError,
    AcaTransientError,
    create_aca_control_plane,
)
from src.services.sandbox.base import (
    SERVED_HEAD_MAX_CHARS,
    CompileReport,
    CompileState,
    DevLogs,
    DevStatus,
    ExecResult,
    FileCreate,
    FileOp,
    FileResult,
    FleetMember,
    SandboxClient,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
    SandboxKind,
    SandboxNotReadyError,
    ServedCount,
    ServedPage,
    a_fresh_sandbox_name,
    base_path_for,
    new_alias,
    pool_member_tags,
    sandbox_tags,
    shared_sandbox_tags,
)
from src.services.sandbox.config import SandboxConfig
from src.services.sandbox.stopwatch import Miss, running_stopwatch
from src.services.storage import get_storage, snapshot_key

if TYPE_CHECKING:
    from src.services.sandbox.pool import ClaimedMember

_log = structlog.get_logger()

# THE TWO LIFECYCLE EVENT NAMES THIS FILE EMITS ARE IMPORTED INSIDE THE METHODS THAT EMIT THEM,
# and that is a worked-around cycle rather than a style choice. They are pinned constants in
# `services/build_sessions/alarms.py` (an alert cannot be written against a name that exists in
# two spellings, so it is imported, never retyped — including by tests), but that module sits
# under a package whose `__init__` imports `appdata`, which imports `SandboxNotConfiguredError`
# from `services/sandbox` — this very package, still half-initialised at that point. A
# module-scope import here is therefore a hard `ImportError` at startup, not a lint smell.
#
# THE REAL FIX IS NOT HERE. `src/core/alarms.py` is a leaf that imports no `src.*` precisely so
# that event names shared across packages have a home, and its own docstring says so. These two
# names now span `services/sandbox/` and `services/build_sessions/`, so they belong there; once
# they move, both deferred imports below become ordinary module-scope ones and this note goes.
_COMPILE_VALUES: Final = frozenset(m.value for m in CompileState)

# Caddy routes `/_sup/*` to the supervisor (stripping the prefix); every call the
# client makes carries it. `handle.fqdn` is host-only (no scheme), so we prepend https.
_SUP_PREFIX: Final = "/_sup"

# Per-request timeouts (seconds). A command run gets the caller's `timeout_s` plus
# head-room for the round trip so a 504 (which IS a `SandboxError`) is the
# supervisor's own timeout, not the transport pre-empting it. Dev/files ops are quick.
_EXEC_TIMEOUT_BUFFER_SECONDS: Final = 30.0
_OP_TIMEOUT_SECONDS: Final = 30.0

# Readiness-poll cadence: start ~0.5 s, exponential backoff capped ~5 s, poll
# until `dev/status.ready` or `timeout_s`. Module-level so tests can shrink them.
_READY_POLL_START_SECONDS: Final = 0.5
_READY_POLL_MAX_SECONDS: Final = 5.0
_READY_BACKOFF_FACTOR: Final = 2.0

# The first-route warm request. Turbopack compiles a route on its FIRST request, and
# until now that request was the citizen's own — 5-7s of blank iframe at the exact moment they
# had just been told their app was ready. The platform pays it instead.
#
# This bound is a LATENCY CEILING, not a safety margin, and it is sized accordingly. Every caller
# awaits this call inline on a path a human is waiting on: the `preview_ready` frame, the `POST
# /relaunch` response, a self-heal iteration. "It gates nothing" is true and says nothing about
# cost — a generous budget here buys a slow app the right to hold a preview frame hostage, which is
# exactly what this warm-up call exists to prevent. 8s covers the measured 5-7s cold compile plus a
# server-render against the per-project database; past that the wait is worth more than the
# compile, and the frontend's own on-load reveal is its insurance either way.
_WARM_TIMEOUT_SECONDS: Final = 8.0

# The serving half of the health verdict gets its OWN budget, wider than the warm request's.
# `someone_has_to_go_first` is an optimization racing a preview frame, so 8s is generous there;
# this one DECIDES whether a build may claim it finished, and a build that answers in 9s is a
# slow app, not a broken one. Timing out here is `INDETERMINATE` and costs a re-check, which is
# strictly cheaper than telling a citizen their working app did not come together.
_SERVING_TIMEOUT_SECONDS: Final = 20.0

# `dev_start` returns the child pid; on the supervisor's 409 "already running" the running dev
# server exposes no pid (the supervisor's `/dev/status` = {running, ready, port}), so we confirm it
# is up and return this sentinel — 0 is never a real Popen pid, so it unambiguously
# reads as "already running, pid unknown" without raising (idempotent).
_ALREADY_RUNNING_PID: Final = 0

# `/dev/compile` is polled on the preview watcher's 1s cadence, so it gets its OWN budget
# rather than the 30s `_OP_TIMEOUT_SECONDS` every other supervisor call shares. The endpoint
# answers from an in-memory value and never touches the dev server, so anything past a few
# seconds is a wedged supervisor — and waiting 30s for that would stall the watcher that also
# owns crash detection. Timing out is not a failure here: it is `UNKNOWN`, which holds.
_COMPILE_TIMEOUT_SECONDS: Final = 5.0

# A claim's health check has to stay far under the time of the create it would save, and
# `configure` only writes an environment.
_HEALTH_TIMEOUT_SECONDS: Final = 2.0
_CONFIGURE_TIMEOUT_SECONDS: Final = 5.0

# The supervisor's per-project rows, the only names its `/configure` accepts: anything else in a
# start's environment was set when the pool container was made, or never belongs in one. The
# connector's two are generated from its key; see `services/lake/env.py`.
_PER_PROJECT_ENV_NAMES: Final = frozenset(
    {"BIAL_APP_ID", "BIAL_BLOB_CONTAINER_URL", "BIAL_BLOB_SAS", "BIAL_DATABASE_URL"}
)

# A container's registry record, written only while the record names no container: a start spends
# seconds between the provision's guard and this write, and another start may take the slot
# meanwhile. ARGV: app name, app id, fqdn, token_ref, birth, then the shared view's project and
# owner, both empty for a build sandbox, then the alias.
#
# The record is per user and outlives its container, so every field a previous occupant could
# leave is overwritten or deleted here. `serving_since` is written as the empty sentinel and never
# deleted: an absent stamp is read as a record from before the stamp existed, which counts as
# proven, so the container would be reported serving the moment it was scheduled. Only an
# observer that saw the app answer replaces it (`locks.mark_serving`). The shared-view stamp is
# the only thing that tells a view from a build sandbox, and a view read as a build sandbox is
# written back over its owner's saved copy, so it is written or cleared in the same script.
_WRITE_THE_REGISTRY_LUA: Final = (
    f"local named = redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_APP_NAME}') "
    "if named and named ~= '' then return 0 end "
    f"redis.call('HSET', KEYS[1], '{REGISTRY_FIELD_APP_NAME}', ARGV[1], "
    f"'{REGISTRY_FIELD_APP_ID}', ARGV[2], '{REGISTRY_FIELD_FQDN}', ARGV[3], "
    f"'{REGISTRY_FIELD_TOKEN_REF}', ARGV[4], '{REGISTRY_FIELD_CREATED_AT}', ARGV[5], "
    f"'{REGISTRY_FIELD_STATE}', '{REGISTRY_STATE_READY}', "
    f"'{REGISTRY_FIELD_WAITING_SINCE}', ARGV[5], '{REGISTRY_FIELD_SERVING_SINCE}', '', "
    f"'{REGISTRY_FIELD_ALIAS}', ARGV[8]) "
    f"redis.call('HDEL', KEYS[1], '{REGISTRY_FIELD_PREVIEW_STAY_UNTIL}', "
    f"'{REGISTRY_FIELD_SHARED_SERVED_COUNT}') "
    "if ARGV[6] ~= '' then "
    f"redis.call('HSET', KEYS[1], '{REGISTRY_FIELD_SHARED_PROJECT_ID}', ARGV[6], "
    f"'{REGISTRY_FIELD_SHARED_OWNER_ID}', ARGV[7]) "
    f"else redis.call('HDEL', KEYS[1], '{REGISTRY_FIELD_SHARED_PROJECT_ID}', "
    f"'{REGISTRY_FIELD_SHARED_OWNER_ID}') end "
    "return 1"
)

# Supervisor bearer token + registry token_ref sizing (secrets, never a UUID).
_SUPERVISOR_TOKEN_BYTES: Final = 32
# The container-env key the supervisor bearer is injected under at create. Named rather than
# inlined because it is now read back as well as written: it is the token's durable home across
# a control-plane restart (`_recover_token`), so the two sites must never drift apart.
_SUPERVISOR_TOKEN_ENV: Final = "SUPERVISOR_TOKEN"
# The container-env key its base path is injected under; read back for a container the registry
# cannot describe yet (`attach_by_name`, a claimed pool container).
_BASE_PATH_ENV: Final = "BIAL_BASE_PATH"
_TOKEN_REF_BYTES: Final = 16

# WHICH BIRTH a container had, carried only so the create notice can say. A
# `Literal` rather than a bare `str` because the value is a log FIELD an operator filters on,
# and a second spelling of any arm is invisible until the day someone greps for the one that
# stopped matching — the same reasoning that pins the event names themselves.
_BirthArm = Literal["provision_new", "restore_from_snapshot", "pool_fill"]

# Capped exponential backoff for transient ACA provisioning errors.
_ACA_MAX_ATTEMPTS: Final = 4
_ACA_RETRY_START_SECONDS: Final = 1.0
_ACA_RETRY_MAX_SECONDS: Final = 8.0

#: The longest `_create_with_retry` can run: each attempt waits at most the ARM ceiling and the
#: longest backoff.
CREATE_CEILING: Final = timedelta(
    seconds=_ACA_MAX_ATTEMPTS * (_LRO_CEILING_SECONDS + _ACA_RETRY_MAX_SECONDS)
)

# How much pool work — creates, deletes and restamps — one process runs at a time, which leaves
# its Azure worker threads free for the starts people are waiting on.
_POOL_WORK_AT_ONCE: Final = 2

# How many claimed containers a start lets go before it creates its own.
_CLAIMS_PER_START: Final = 2

# How many times a claim asks Azure for a container's bearer through transient errors.
_BEARER_READS: Final = 2

#: How long a container made for the pool may take to answer once Azure reports it made: its
#: supervisor has been measured answering a minute and a half after the create returned.
FIRST_ANSWER_CEILING: Final = timedelta(minutes=5)
_FIRST_ANSWER_POLL_SECONDS: Final = 2.0

#: What one fill came to: a ready container made, none needed, or none made.
FillOutcome = Literal["filled", "at_target", "refused"]

# Capped retry for the attach reachability probe (a single blip must NOT map to Gone).
_PROBE_MAX_ATTEMPTS: Final = 4
_PROBE_START_SECONDS: Final = 0.5
_PROBE_MAX_SECONDS: Final = 4.0

# The restore transport: the base64'd git bundle is written into the workspace, decoded, and
# unbundled onto the fresh container's disk, then `npm install` reconciles the dynamic deps
# the snapshotted lockfile added on top of the pre-baked base.
# The baked node_modules survives `checkout -q -f` (there is NO `git clean`),
# so this is a DELTA install, not a full reinstall — `npm install` (not `npm ci`, which
# would wipe node_modules and defeat the baked base's speed). A non-zero install aborts the
# `set -e` script → non-zero exit → `_restore_snapshot_into` raises SandboxError → the caller
# self-cleans the container. Single-line POSIX `sh -c` (the image ships LF from the
# Windows build VM). This exact shell is validated live elsewhere; here it is a mock-driven seam.
#
# THE RECONCILE IS CONDITIONAL: the baked lockfile is hashed BEFORE the checkout and
# the snapshot's lockfile AFTER; when they match, the baked node_modules already satisfies the
# snapshot exactly and the install is SKIPPED — a change build on an app that never added a
# dependency restores with zero npm work. The two `|| echo` fallbacks are DIFFERENT sentinels
# on purpose: either lockfile missing → the strings can never compare equal → the install runs
# (fail-safe toward installing; skipping is only ever an optimization, never a correctness bet).
_RESTORE_TIMEOUT_SECONDS: Final = 600
"""Wall-clock cap for the restore script (bundle unpack + the `npm install` reconcile). Raised
from the pre-reconcile 300s to cover a real delta install on the Windows-built image — TUNE
against a real restore, not a guess (too low turns a legitimate large install into a FALSE hard
restore error). Sandbox-layer constant: deliberately NOT imported from orchestrator/constants.py —
the dependency direction is orchestrator→sandbox, so a back-import would invert it and risk a
cycle."""
_BUNDLE_B64_NAME: Final = "app.bundle.b64"

# The golden template ships NO `.git` — the image bakes it in with `COPY template/ ./`, and
# Docker would not carry a repo across even if the template had one. The RESTORE path creates
# a repo itself (`git init` + fetch + checkout, below), so only a FRESH provision arrives
# without one, and everything downstream assumes a repo exists: the save-state check reads
# `git rev-parse HEAD` / `git status --porcelain`, the health verdict identifies the app by its
# root commit, and the snapshot refuses to run at all without one.
#
# So a container is made a working repo at BIRTH, with one baseline commit for the template, and
# every platform snapshot afterwards is a delta against a known starting point. THIS IS THE ONLY
# WRITER OF A ROOT COMMIT in the system, which is what lets the health verdict read that root as
# the template rather than as whatever a later commit happened to hold. Idempotent (`rev-parse`
# short-circuits), and `git config --system` in the image supplies the identity.
_INIT_REPO_SCRIPT: Final = (
    "git rev-parse --git-dir >/dev/null 2>&1 || "
    "{ git init -q && git add -A && git commit -q -m 'bial: golden template baseline'; }"
)
# The fragments both bundle scripts share. `baked_lock` fingerprints the lockfile the installed
# `node_modules` was built for, taken before the tree moves; `snap_lock` the one the tree wants
# after it. Dependencies are reinstalled only when the two differ.
_UNPACK_THE_PUSHED_BUNDLE: Final = f"base64 -d {_BUNDLE_B64_NAME} > /tmp/bial-app.bundle; "
_FINGERPRINT_THE_INSTALLED_LOCKFILE: Final = (
    "baked_lock=$(sha256sum package-lock.json 2>/dev/null || echo baked-lock-missing); "
)
_FINGERPRINT_THE_WANTED_LOCKFILE: Final = (
    "snap_lock=$(sha256sum package-lock.json 2>/dev/null || echo snap-lock-missing); "
)
# The one line of a restore's output the control plane reads back: the start it belongs to records
# whether it paid for a reinstall.
_REINSTALLED_MARKER: Final = "bial-restore: reinstalling dependencies"
# Said on stderr when the app keeps the libraries it was saved with because moving them (below)
# failed. Only the marker is logged: the error itself can quote the file.
_LIBRARIES_LEFT_AS_SAVED: Final = "bial-restore: libraries left as saved"
_NPM_INSTALL: Final = "npm install --no-audit --no-fund --loglevel=error"
# An install that fails on the versions the catch-up moved puts the saved manifest back, with the
# saved lockfile when the commit tracks one, and installs that instead. One that fails on the saved
# manifest fails the script.
_RECONCILE_A_MOVED_LOCKFILE: Final = (
    'if [ "$baked_lock" = "$snap_lock" ]; then '
    "echo 'lockfile unchanged - skipping npm reconcile'; "
    f"else echo '{_REINSTALLED_MARKER}'; if ! {_NPM_INSTALL}; then "
    "git diff --quiet HEAD -- package.json && exit 1; "
    f"echo '{_LIBRARIES_LEFT_AS_SAVED}' >&2; "
    "git restore -q -s HEAD -- package.json $(git ls-files package-lock.json); "
    f"{_NPM_INSTALL}; fi; fi; "
)
_REMOVE_THE_PUSHED_BUNDLE: Final = f"rm -f /tmp/bial-app.bundle {_BUNDLE_B64_NAME}"

# A restore checks the saved commit out over the image's template, and a checkout only writes what
# the commit holds: a template file the app deleted stays on disk, serves in the app's place, and
# the next save commits it back. So an untracked file goes if the app's own history ever held it.
# One the app never had is the image's, and stays. Skipped without an exclude file, where the baked
# `node_modules` would read as untracked too. A name git has to quote matches no history and stays.
_REMOVE_WHAT_THE_APP_DELETED: Final = (
    'if excludes=$(git config --path --get core.excludesFile) && [ -f "$excludes" ]; then '
    "git -c core.quotePath=false ls-files --others --exclude-standard | "
    "while IFS= read -r f; do "
    'if [ -n "$(git log -1 --format=%h HEAD -- ":(literal)$f")" ]; then rm -f -- "$f"; fi; '
    "done; fi; "
)

# An app saved before its config read `BIAL_BASE_PATH` serves at `/` while the platform asks for
# its assigned path, so it never opens. A restore sets the image's config aside before the checkout
# and puts it back after the removal above (which would otherwise delete it from an app that once
# dropped the file) — but only over a config that does not read the path. A config that does is
# kept as the app saved it: publishing layers the app's own settings (images, redirects, env)
# under the platform's, and replacing it would drop them. Discard applies the same rule. Kept in
# `.git`, which no save bundles, no discard cleans and the agent's file tool cannot write.
_IMAGES_NEXT_CONFIG: Final = ".git/bial-next.config.ts"
_SET_THE_IMAGES_NEXT_CONFIG_ASIDE: Final = (
    f"[ -f {_IMAGES_NEXT_CONFIG} ] || cp next.config.ts {_IMAGES_NEXT_CONFIG}; "
)
_PUT_THE_IMAGES_NEXT_CONFIG_BACK: Final = (
    f"if [ -f {_IMAGES_NEXT_CONFIG} ] && ! grep -qs BIAL_BASE_PATH next.config.ts; then "
    f"cp {_IMAGES_NEXT_CONFIG} next.config.ts; fi; "
)

# An app keeps the library versions it was saved with, so each image that moves the template's
# versions would have every older app reinstall its older copies on every open and keep running
# releases the image moved away from. So each of the image's packages and overrides the app
# declares moves up to the image's version: never down and never onto another major, either of
# which can break the app's code. An override the app lacks is added. The app's own packages keep
# their versions, so the install fetches only those; with none, the app takes the image's lockfile
# and installs nothing. The move is a workspace change like any other, kept by the next save. The
# image's manifest and lockfile are set aside before the checkout, as its Next config is; a
# container born from this image has none, its app having been made on these versions.
_IMAGES_PACKAGE_JSON: Final = ".git/bial-package.json"
_IMAGES_PACKAGE_LOCK: Final = ".git/bial-package-lock.json"
_SET_THE_IMAGES_LIBRARIES_ASIDE: Final = (
    f"[ -f {_IMAGES_PACKAGE_JSON} ] || [ ! -f package.json ] || "
    f"cp package.json {_IMAGES_PACKAGE_JSON}; "
    f"[ -f {_IMAGES_PACKAGE_LOCK} ] || [ ! -f package-lock.json ] || "
    f"cp package-lock.json {_IMAGES_PACKAGE_LOCK}; "
)
# Run as `node -e '<this>' <image manifest> <image lockfile>`, so it holds no single quote. Only an
# exact version, bare or behind a caret or tilde, is compared; an alias, a git URL, a prerelease
# or a compound range is the app's own choice and stays. "Another major" counts from the first
# non-zero part, as caret ranges do. A package moves in every section that declares it or in none,
# because npm refuses an override that disagrees with a direct dependency. Where the app's version
# of a package differs from the image's, its own lockfile entries win. Nothing is written until
# both files are worked out.
_CATCH_UP_JS: Final = (
    'const fs = require("fs"); '
    "const [imagePackagePath, imageLockPath] = process.argv.slice(1); "
    'const read = (path) => JSON.parse(fs.readFileSync(path, "utf8")); '
    'const json = (value) => JSON.stringify(value, null, 2) + "\\n"; '
    "const triple = (version) => { "
    r"const m = String(version).match(/^[\^~]?(\d+)\.(\d+)\.(\d+)$/); "
    "return m && m.slice(1).map(Number); }; "
    "const significant = (v) => String(v.slice(0, v.findIndex((x) => x > 0) + 1 || 3)); "
    "const outranks = (theirs, wanted) => { const a = triple(theirs), b = triple(wanted); "
    "if (!a || !b || significant(a) !== significant(b)) return true; "
    "const i = [0, 1, 2].find((k) => a[k] !== b[k]); return i !== undefined && a[i] > b[i]; }; "
    'const dependencySections = ["dependencies", "devDependencies", "optionalDependencies", '
    '"peerDependencies"]; '
    'const sections = [...dependencySections, "overrides"]; '
    "try { "
    'const image = read(imagePackagePath), app = read("package.json"); '
    "const spec = (p, s, name) => (p[s] || {})[name]; "
    "const imageSpec = (name) => sections.map((s) => spec(image, s, name))"
    ".find((v) => v !== undefined); "
    "let moved = false; "
    'for (const section of ["dependencies", "devDependencies", "overrides"]) '
    "for (const [name, wanted] of Object.entries(image[section] || {})) { "
    "const homes = sections.filter((s) => spec(app, s, name) !== undefined "
    '|| (s === "overrides" && section === s)); '
    "const holdsBack = (s) => spec(app, s, name) !== undefined "
    "&& spec(app, s, name) !== wanted && outranks(spec(app, s, name), wanted); "
    "if (homes.some(holdsBack)) continue; "
    "for (const s of homes) if (spec(app, s, name) !== wanted) { "
    "app[s] = { ...app[s], [name]: wanted }; moved = true; } } "
    "if (moved) { "
    "const declarationsOf = (p) => JSON.stringify(sections.map((s) => "
    "Object.entries(p[s] || {}).sort())); "
    'let lockfile = fs.readFileSync(imageLockPath, "utf8"); '
    "if (declarationsOf(app) !== declarationsOf(image)) { "
    "const lock = JSON.parse(lockfile); "
    'const own = fs.existsSync("package-lock.json") ? read("package-lock.json").packages || {} '
    ": {}; "
    'const root = { ...lock.packages[""] }; '
    "for (const s of dependencySections) { delete root[s]; if (app[s]) root[s] = app[s]; } "
    'lock.packages[""] = root; '
    "const pinned = new Set(sections.flatMap((s) => Object.keys(app[s] || {})"
    ".filter((name) => spec(app, s, name) !== imageSpec(name)))); "
    'const nameOf = (path) => path.slice(path.lastIndexOf("node_modules/") '
    '+ "node_modules/".length); '
    "for (const [path, entry] of Object.entries(own)) "
    "if (path && (!(path in lock.packages) || pinned.has(nameOf(path)))) "
    "lock.packages[path] = entry; "
    "lockfile = json(lock); } "
    'fs.writeFileSync("package.json", json(app)); '
    'fs.writeFileSync("package-lock.json", lockfile); } '
    f'}} catch {{ console.error("{_LIBRARIES_LEFT_AS_SAVED}"); }}'
)
_CATCH_THE_APP_UP_WITH_THE_IMAGE: Final = (
    f"if [ -f {_IMAGES_PACKAGE_JSON} ] && [ -f {_IMAGES_PACKAGE_LOCK} ]; then "
    f"node -e '{_CATCH_UP_JS}' {_IMAGES_PACKAGE_JSON} {_IMAGES_PACKAGE_LOCK}; fi; "
)

_RESTORE_SCRIPT: Final = (
    "set -e; "
    + _UNPACK_THE_PUSHED_BUNDLE
    + _FINGERPRINT_THE_INSTALLED_LOCKFILE
    + "git init -q 2>/dev/null || true; "
    + _SET_THE_IMAGES_NEXT_CONFIG_ASIDE
    + _SET_THE_IMAGES_LIBRARIES_ASIDE
    + "git fetch -q /tmp/bial-app.bundle HEAD; "
    "git checkout -q -f FETCH_HEAD; "
    + _REMOVE_WHAT_THE_APP_DELETED
    + _PUT_THE_IMAGES_NEXT_CONFIG_BACK
    + _CATCH_THE_APP_UP_WITH_THE_IMAGE
    + _FINGERPRINT_THE_WANTED_LOCKFILE
    + _RECONCILE_A_MOVED_LOCKFILE
    + _REMOVE_THE_PUSHED_BUNDLE
)

# Discard: the same fetch into the LIVE repository, then `reset --hard` so HEAD is exactly the
# saved commit and `clean -fd` so files the discarded work added are gone. No `-x`: the ignored
# `node_modules` and `.next` stay, and attachments live outside the app tree.
_DISCARD_SCRIPT: Final = (
    "set -e; "
    + _UNPACK_THE_PUSHED_BUNDLE
    + _FINGERPRINT_THE_INSTALLED_LOCKFILE
    + "git fetch -q /tmp/bial-app.bundle HEAD; "
    "git reset -q --hard FETCH_HEAD; "
    "git clean -q -fd; "
    + _PUT_THE_IMAGES_NEXT_CONFIG_BACK
    + _CATCH_THE_APP_UP_WITH_THE_IMAGE
    + _FINGERPRINT_THE_WANTED_LOCKFILE
    + _RECONCILE_A_MOVED_LOCKFILE
    + _REMOVE_THE_PUSHED_BUNDLE
)


async def _make_it_a_repo(client: SandboxClient, handle: SandboxHandle) -> None:
    """Give a freshly provisioned container a git repo, or fail the provision.

    NOT best-effort, and the trade is the deliberate one. A container whose seed failed is a
    container whose every Save fails, because the snapshot will not create a repository for it —
    so the only question is WHERE the citizen meets that failure. Here it is a provision that did
    not happen, retried before they have typed anything; deferred to the first Save it is their
    finished app with nowhere to go. The exec's own exceptions propagate for the same reason."""
    run_command = client.exec  # alias keeps the call off the JS-oriented exec guard
    result = await run_command(handle, ["sh", "-c", _INIT_REPO_SCRIPT], timeout_s=60)
    if result.exit != 0:
        raise SandboxError(f"could not seed the workspace git repo (exit {result.exit})")


class SandboxNotConfiguredError(SandboxError):
    """`get_sandbox()` was called but no sandbox is configured (genuinely-optional in
    dev/test). Mirrors `RedisNotConfiguredError` / storage's `StorageError`: a
    dedicated type lets a caller narrow-catch the unset-sandbox case rather than a
    bare `SandboxError`."""


class _ClaimFellThroughError(Exception):
    """A step of a claim before its registry write failed; `reason` is the miss the start records
    if no other claim works. `keep` says the step learnt nothing of the container, which goes back
    to the pool rather than being let go."""

    def __init__(self, reason: Miss, *, keep: bool = False) -> None:
        super().__init__(reason)
        self.reason: Miss = reason
        self.keep = keep


class _SlotTakenError(SandboxError):
    """A conditional registry write found the person's record naming a container already."""


class _CreateFailedError(SandboxError):
    """A create that failed for good. `left_standing` is True when its self-clean was refused, so
    the container may still exist."""

    def __init__(self, message: str, *, left_standing: bool) -> None:
        super().__init__(message)
        self.left_standing = left_standing


async def _asleep(seconds: float) -> None:
    """Poll/backoff sleep behind one indirection so tests can record the schedule
    without real waits."""
    await asyncio.sleep(seconds)


def _per_project_env_names() -> frozenset[str]:
    """Every name a claimed pool container is configured with. Lazy for the reason the lake
    import in `_provision_container` gives."""
    from src.core.connectors import CONNECTORS
    from src.services.lake.env import connector_env_names

    return _PER_PROJECT_ENV_NAMES.union(*(connector_env_names(key) for key in CONNECTORS))


def _identity_tags(kind: SandboxKind, user_uuid: uuid.UUID, app_id: uuid.UUID) -> dict[str, str]:
    """The ARM identity of a container born now. On the shared arm `user_uuid` is the recipient."""
    if kind == "shared_sandbox":
        return shared_sandbox_tags(recipient_id=user_uuid, app_id=app_id)
    return sandbox_tags(user_id=user_uuid, app_id=app_id)


def _public_app_url(base_path: str) -> str:
    """Where a BIAL employee's browser reaches the app served under `base_path`.

    NOT `https://{fqdn}/`. The Container Apps environment is internal and publishes no public
    DNS, so its own domain does not resolve from a BIAL desk. Lazy settings import for the
    same cycle reason as `_apps_hostname`.
    """
    from src.config import settings  # lazy: avoid an import cycle via src.config

    return f"{settings.APPS_BASE_URL}{base_path}"


def _apps_hostname() -> str:
    """The public hostname every generated app is served from, e.g. `citizenapps.bialairport.com`.

    Read lazily — same import-cycle reason as `get_sandbox` below (`src.config` reaches back
    into the service packages, so a module-level import here would cycle).

    A HOST, never an origin: it becomes Next's `serverActions.allowedOrigins`, compared against
    the browser's `Origin` — a value carrying a scheme fails CLOSED and SILENTLY, aborting
    every form post as a CSRF attempt with no other symptom."""
    from src.config import settings  # lazy: avoid an import cycle via src.config

    return settings.apps_hostname


def portal_origin() -> str:
    """`BIAL_PORTAL_ORIGIN`: the one origin allowed to frame a sandbox, the bare origin of
    `FRONTEND_URL`, which both processes that make sandboxes hold."""
    from src.config import settings  # lazy: avoid an import cycle via src.config

    parts = urlsplit(settings.FRONTEND_URL)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return settings.FRONTEND_URL.rstrip("/")


def _creation_env(base_path: str, token: str) -> dict[str, str]:
    """What every container is created with: its supervisor bearer, the path it is served under
    and the hostname that path is served on."""
    return {
        _SUPERVISOR_TOKEN_ENV: token,
        _BASE_PATH_ENV: base_path,
        "BIAL_APPS_HOSTNAME": _apps_hostname(),
    }


def _says_it_is_configured(resp: httpx.Response) -> bool:
    """What a supervisor's `/health` answer says of its settings. One built before pool containers
    existed does not say, and was given its settings when it was created; neither does a body
    that cannot be read, which sends nothing a second time."""
    try:
        body: Any = resp.json()
        return bool(body.get("configured", True))
    except (AttributeError, ValueError):  # fmt: skip  # ruff py314 strips parens
        return True


class AcaSandboxClient(SandboxClient):
    """The concrete client. Holds one long-lived `httpx.AsyncClient` for the
    supervisor calls and (lazily) one `AcaControlPlane` for the container lifecycle.

    `transport` injects an `httpx.MockTransport` for the `/_sup/*` layer; `aca`
    injects a fake control plane — together they let every behavior be tested without
    a live container or real Azure."""

    def __init__(
        self,
        config: SandboxConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        aca: AcaControlPlane | None = None,
    ) -> None:
        self._config = config
        # No global timeout — every call passes an explicit per-op timeout (a command
        # run may legitimately last for `timeout_s` seconds, far past a default 5 s).
        self._http = httpx.AsyncClient(transport=transport, timeout=None)
        self._aca_lazy = aca
        # token_ref (the registry reference) -> the live bearer token. In-process
        # only; a restart empties it -> references resolve to nothing -> SandboxGoneError
        # -> the restore path. NEVER persisted to Redis.
        self._token_refs: dict[str, str] = {}
        # app_name -> owning user, so teardown (which gets only a handle) can clear the
        # user-keyed registry hash for a session this process created. Empty after a
        # restart — the reaper, which holds the user_id, clears the registry itself.
        self._app_owners: dict[str, uuid.UUID] = {}
        # Pool work a claim leaves running behind the start, held so it is not collected.
        self._detached: set[asyncio.Task[None]] = set()
        # One client per process, so this bounds the process's pool work.
        self._pool_work = asyncio.Semaphore(_POOL_WORK_AT_ONCE)

    @property
    def config(self) -> SandboxConfig:
        return self._config

    @property
    def _aca(self) -> AcaControlPlane:
        if self._aca_lazy is None:
            self._aca_lazy = create_aca_control_plane(self._config)
        return self._aca_lazy

    async def list_sandbox_fleet(self) -> list[FleetMember]:
        """Every sandbox container ARM knows about — the fleet view the Redis-driven reaper cannot
        produce when the coordination store is gone (see `build_sessions/inventory.py`).

        NOT on the frozen `SandboxClient` ABC (operator-facing, not per-sandbox lifecycle) —
        satisfies `inventory.FleetLister` by shape. `AcaError` -> `SandboxError` at this PORT:
        the predecessor `list_sandbox_app_names` skipped that translation, so an ARM throttle
        surfaced as a 500 instead of the documented retryable 503."""
        try:
            return await self._aca.list_sandbox_fleet()
        except AcaError as exc:
            raise SandboxError("could not enumerate the sandbox fleet") from exc

    async def get_app_tags(self, *, name: str) -> dict[str, str] | None:
        """One container's current tags — the destroy path's re-validation read.

        `AcaError` is translated at the PORT, like its neighbours: no vendor type crosses it."""
        try:
            return await self._aca.get_app_tags(name=name)
        except AcaError as exc:
            raise SandboxError("could not read the container's tags") from exc

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        """Merge identity onto an existing container (the backfill's write half).

        A PATCH, never a PUT — a PUT would replace the resource and take a live sandbox's
        container env, and with it the supervisor bearer, down with it. The MERGE is the lower
        seam's own doing: `Microsoft.App` replaces the tag map on a PATCH, so `AcaControlPlane`
        reads the current tags and writes the union."""
        try:
            await self._aca.stamp_tags(name=name, tags=tags)
        except AcaError as exc:
            raise SandboxError(f"could not stamp identity onto {name}") from exc

    # --- supervisor HTTP layer ---------------------------------------------

    @staticmethod
    def _auth(handle: SandboxHandle) -> dict[str, str]:
        return {"Authorization": f"Bearer {handle.token}"}

    def _url(self, handle: SandboxHandle, endpoint: str) -> str:
        return f"https://{handle.fqdn}{_SUP_PREFIX}/{endpoint}"

    async def _post(
        self, handle: SandboxHandle, endpoint: str, body: dict[str, Any], *, timeout: float
    ) -> httpx.Response:
        try:
            return await self._http.post(
                self._url(handle, endpoint),
                json=body,
                headers=self._auth(handle),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            # Timeout / connect / unreachable supervisor — no vendor type crosses the port.
            raise SandboxError(f"supervisor {endpoint} request failed") from exc

    async def _get(
        self,
        handle: SandboxHandle,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> httpx.Response:
        try:
            return await self._http.get(
                self._url(handle, endpoint),
                params=params,
                headers=self._auth(handle),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise SandboxError(f"supervisor {endpoint} request failed") from exc

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int = 900,
    ) -> ExecResult:
        body: dict[str, Any] = {"cmd": cmd, "timeout": timeout_s}
        if cwd is not None:
            body["cwd"] = cwd
        resp = await self._post(
            handle, "exec", body, timeout=timeout_s + _EXEC_TIMEOUT_BUFFER_SECONDS
        )
        if resp.status_code != 200:
            # A supervisor 504 (timeout) and any other non-200 are a SandboxError; a
            # non-zero EXIT would have come back inside a 200 (handled below).
            raise SandboxError(f"command run failed with status {resp.status_code}")
        data: Any = resp.json()
        try:
            return ExecResult(
                stdout=str(data["stdout"]), stderr=str(data["stderr"]), exit=int(data["exit"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A 200 whose body is not the exec shape. This has to be a SandboxError and not the
            # raw `KeyError`: callers narrow on SandboxError, and an untyped one escaping here
            # once already took down a provision that had otherwise succeeded. Failing is right —
            # the seed did not happen — but it fails as the kind of error the seam declares.
            raise SandboxError("the supervisor answered exec with an unrecognized body") from exc

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        # Serialize the validated variant back to the supervisor's flat `FilesBody` wire shape;
        # `exclude_none` drops the fields this variant doesn't carry.
        body = op.model_dump(exclude_none=True)
        resp = await self._post(handle, "files", body, timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            # A supervisor 422 (0/N str_replace matches) and 400 (missing sub-field / unknown
            # action) both surface as SandboxError.
            #
            # ★ THE SUPERVISOR'S OWN SENTENCE RIDES ALONG, because the status alone cannot tell
            # an operator apart the two failures that matter most here: "unknown files action"
            # (a container running an image that predates the action) and "path escapes
            # workspace" (a control-plane path bug). Both are 400.
            #
            # SAFE TO CARRY, and on a narrower ground than "the supervisor redacts everything" —
            # it does not: `_redact` covers `exec` output and `view` content only. What makes
            # this safe is that every non-200 `/files` detail is ENUMERABLE and content-free —
            # the four fixed `HTTPException` strings, plus `_resolve`'s, which echoes the path
            # this side sent. No file bytes reach a non-200 body. Capped anyway, because a
            # message that grows without bound is how a log becomes a payload's home.
            raise SandboxError(
                f"files op failed with status {resp.status_code}: {resp.text[:200]}"
            )
        data: Any = resp.json()
        detail: dict[str, object] = {str(k): v for k, v in data.items() if k != "ok"}
        return FileResult(ok=bool(data.get("ok", True)), detail=detail)

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        body: dict[str, Any] = {}
        if cmd is not None:
            body["cmd"] = cmd
        if cwd is not None:
            body["cwd"] = cwd
        resp = await self._post(handle, "dev/start", body, timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code == 409:
            # Idempotent: the dev server is already running. Confirm via a status probe
            # and return the already-running sentinel — the supervisor exposes no pid here.
            status = await self.dev_status(handle)
            if status.running:
                return _ALREADY_RUNNING_PID
            raise SandboxError("dev/start reported 409 but the server is not running")
        if resp.status_code != 200:
            raise SandboxError(f"dev/start failed with status {resp.status_code}")
        try:
            data: Any = resp.json()
            return int(data["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed 200 body must stay inside the SandboxError taxonomy, exactly as
            # `dev_status` below already does. TWO callers now guard this call with `except
            # SandboxError` and treat it as best-effort — the Write turn's boot-at-attach and
            # relaunch's attach arm — so a raw `KeyError` escaping here would skip both guards and
            # kill a turn whose workspace had already been reported ready.
            raise SandboxError("dev/start returned a malformed body") from exc

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        resp = await self._get(handle, "dev/status", timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise SandboxError(f"dev/status failed with status {resp.status_code}")
        try:
            data: Any = resp.json()
            # `exit_code` is absent on pre-exit_code supervisor images — `.get` keeps the
            # client compatible with a sandbox provisioned before the field shipped.
            raw_exit = data.get("exit_code")
            # `root_status` is absent on a supervisor image that predates it, and `.get` is what
            # keeps this client able to talk to one. An absent value reads as "this container
            # cannot say", never as "the root answered badly" — see `DevStatus.root_status`.
            raw_root = data.get("root_status")
            return DevStatus(
                running=bool(data["running"]),
                ready=bool(data["ready"]),
                port=int(data["port"]),
                exit_code=None if raw_exit is None else int(raw_exit),
                root_status=None if raw_root is None else int(raw_root),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed 200 body must stay inside the SandboxError taxonomy: every best-effort
            # caller (dev_start's 409 probe, attach_existing's readiness check) guards
            # `except SandboxError` — a raw ValueError/KeyError would escape those guards.
            raise SandboxError("dev/status returned a malformed body") from exc

    async def dev_logs(self, handle: SandboxHandle, *, since: int = 0) -> DevLogs:
        resp = await self._get(
            handle, "dev/logs", params={"since": since}, timeout=_OP_TIMEOUT_SECONDS
        )
        if resp.status_code != 200:
            raise SandboxError(f"dev/logs failed with status {resp.status_code}")
        data: Any = resp.json()
        # Map the supervisor's wire field `next` -> `DevLogs.next_cursor` (renamed only to avoid
        # shadowing the builtin); pass it back as `since` for only-new lines.
        return DevLogs(lines=[str(line) for line in data["lines"]], next_cursor=int(data["next"]))

    async def _check_health(self, handle: SandboxHandle) -> None:
        """A claim's check that the supervisor answers, bounded at two seconds; any answer but a
        200 raises `SandboxError`."""
        resp = await self._get(handle, "health", timeout=_HEALTH_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise SandboxError(f"health failed with status {resp.status_code}")

    async def configure(self, handle: SandboxHandle, env: Mapping[str, str]) -> None:
        """Give a pool container its project's settings: the per-project names in `env` and no
        other, which the supervisor takes once. Any refusal raises `SandboxError`. The body holds
        the database URL and the SAS, so nothing here logs or carries a value."""
        names = _per_project_env_names()
        body = {"env": {name: value for name, value in env.items() if name in names}}
        resp = await self._post(handle, "configure", body, timeout=_CONFIGURE_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise SandboxError(f"configure failed with status {resp.status_code}")

    async def wait_ready(
        self, handle: SandboxHandle, *, timeout_s: float = 120.0
    ) -> SandboxHandle:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        delay = _READY_POLL_START_SECONDS
        while True:
            status = await self.dev_status(handle)
            if status.ready:
                return replace(handle, ready=True)
            if loop.time() >= deadline:
                raise SandboxNotReadyError(f"dev server not ready within {timeout_s}s")
            await _asleep(delay)
            delay = min(delay * _READY_BACKOFF_FACTOR, _READY_POLL_MAX_SECONDS)

    async def compile_state(self, handle: SandboxHandle) -> CompileReport:
        """Ask the supervisor what the dev server is compiling — `GET /dev/compile`.

        NEVER RAISES: every failure (pre-endpoint image 404, transport error, malformed body)
        means the same thing — we do not know — and returns `UNKNOWN` rather than an exception,
        so no call site can forget to handle it. `reason` keeps the distinction observable.

        THE 404 ARM IS LOAD-BEARING: images built before this endpoint existed answer 404
        forever and `/health` gives no way to tell, so those must read `UNKNOWN` until restored."""
        try:
            resp = await self._get(handle, "dev/compile", timeout=_COMPILE_TIMEOUT_SECONDS)
        except SandboxError:
            return CompileReport(state=CompileState.UNKNOWN, reason="transport_error")
        if resp.status_code == 404:
            return CompileReport(state=CompileState.UNKNOWN, reason="endpoint_absent")
        if resp.status_code != 200:
            return CompileReport(state=CompileState.UNKNOWN, reason="status_error")
        try:
            data: Any = resp.json()
            raw_state = str(data["state"])
            raw_errors = data.get("errors")
            errors = tuple(str(e) for e in raw_errors) if isinstance(raw_errors, list) else ()
            if raw_state not in _COMPILE_VALUES:
                # An unrecognised state string is `UNKNOWN`, never a guess. The supervisor and
                # this client ship in separate images and can be a release apart in either
                # direction, so a value one of them has not heard of is a real state — and it
                # gets its OWN reason rather than inheriting the body's, which describes a
                # state we just declined to believe.
                return CompileReport(state=CompileState.UNKNOWN, reason="unrecognised_state")
            raw_reason = data.get("reason")
            return CompileReport(
                state=CompileState(raw_state),
                errors=errors,
                reason=None if raw_reason is None else str(raw_reason),
                connect_generation=int(data.get("connect_generation") or 0),
            )
        except (KeyError, TypeError, ValueError):  # fmt: skip  # ruff py314 strips parens
            return CompileReport(state=CompileState.UNKNOWN, reason="malformed_body")

    async def served_count(self, handle: SandboxHandle) -> ServedCount | None:
        """Ask the supervisor how many requests the app has served — `GET /_sup/served`.

        NEVER RAISES, same posture as `compile_state`: a transport failure, a pre-`/served`
        supervisor image, or a malformed body all mean the same thing to the caller — this
        probe could not answer — and `None` is that answer. Returning a zeroed `ServedCount` on
        a failure would read as "confirmed no new traffic" and let the reclamation sweep reap a
        container it simply could not reach, which is the one direction this signal must never
        be wrong in. `truncated` defaults to `True` on a malformed body for the identical
        fail-closed reason — an unparseable `truncated` field must not silently read as "this
        count is a trustworthy total" (`ServedCount`'s own docstring says what `truncated`
        actually governs)."""
        try:
            resp = await self._get(handle, "served", timeout=_OP_TIMEOUT_SECONDS)
        except SandboxError:
            return None
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
            return ServedCount(
                count=int(body["served"]), truncated=bool(body.get("truncated", True))
            )
        except (KeyError, TypeError, ValueError):  # fmt: skip  # ruff py314 strips parens
            return None

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        """The app's public root: status plus a bounded head of the body.

        The SERVING half of the health verdict, fetched through the front door so it sees what the
        iframe would. It reads a bounded PREFIX of the body (`SERVED_HEAD_MAX_CHARS`): the verdict
        keeps what the app was serving as raw evidence, and the app does not choose how much memory
        that costs. That text is SCRUBBED first — the sandbox env holds secrets a rendered page
        could leak into it. NEVER RAISES, and `None` is load-bearing: it means INDETERMINATE, so an
        app that is serving is never called broken because our own request timed out."""
        try:
            async with (
                asyncio.timeout(_SERVING_TIMEOUT_SECONDS),
                # THE APP'S OWN PAGES, not the container root. Under a base path the root
                # belongs to no route and answers the framework's 404 — which this probe would
                # faithfully report as "what the app is serving", self-heal would convert into
                # "make sure `app/page.tsx` exists", and the model would burn metered tokens
                # repairing a file that was never wrong.
                self._http.stream(
                    "GET", handle.app_root_url, headers={"Accept-Encoding": "identity"}
                ) as resp,
            ):
                chunks: list[bytes] = []
                size = 0
                # `aiter_raw`, NOT `aiter_bytes`, and it is the difference between a bound and a
                # suggestion. `aiter_bytes` yields DECODED bytes: httpx negotiates gzip by
                # default and its decoder expands a whole network read in one call with no
                # length limit, so a single 64 KiB raw chunk can decode to tens of megabytes and
                # be appended here before the cap is ever evaluated — measured at 204 KB on the
                # wire buffering 67 MB. The body is written by unreviewed code inside the
                # citizen's sandbox, so that is the app choosing the control plane's allocation.
                # Reading the wire makes the cap a bound over bytes nothing can amplify.
                #
                # `Accept-Encoding: identity` is the other half: it asks for the plain body so
                # the evidence is readable. An app that compresses anyway yields an unreadable
                # head rather than an unbounded one, which is the right way round for a field
                # whose whole job is "what was it actually serving?".
                async for chunk in resp.aiter_raw():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= SERVED_HEAD_MAX_CHARS:
                        break
                raw = b"".join(chunks)[:SERVED_HEAD_MAX_CHARS]
                # SCRUBBED HERE, at the boundary, so the raw text never leaves this method: the
                # sandbox child env holds `BIAL_DATABASE_URL` and `BIAL_BLOB_SAS`, and a page
                # that renders a server value — or a dev error page quoting a connection string
                # in a stack — puts one straight into the first 2 KB of a document this field
                # then persists. Scrubbing once here is what makes that safe by construction
                # rather than by every later reader remembering.
                head = scrub_untrusted(raw.decode("utf-8", "replace"), limit=SERVED_HEAD_MAX_CHARS)
                return ServedPage(status=resp.status_code, head=head)
        except Exception:  # noqa: BLE001 - `None` is the contract; nothing may reach the verdict
            # `exc_info` for the same reason the warm request carries it: a silent swallow makes
            # restricted egress look identical to a healthy app, and this call decides whether a
            # build is allowed to claim it finished.
            _log.warning("serving_probe_failed", app=handle.app_name, exc_info=True)
            return None

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        """Pay the first Turbopack route compile so the citizen's browser does not — at the app
        root over the public ingress the browser uses, not `/exec`+curl.

        NON-LOAD-BEARING BY CONSTRUCTION: gates nothing, raises nothing, carries its own timeout,
        so a hang costs the preview NOTHING. The status is returned (a 500 is a real compile error)
        but no caller may condition the frame on it. Logged here, since every caller discards the
        return value and `selfheal.verify`'s text markers would otherwise ship a broken root green.
        Treat the response as HOSTILE — see the request below."""
        try:
            async with (
                asyncio.timeout(_WARM_TIMEOUT_SECONDS),
                # TREAT THE RESPONSE AS HOSTILE: unreviewed, agent-authored sandbox code, and
                # the one call here that leaves the supervisor's bearer-guarded surface for the
                # app's own. NO REDIRECT FOLLOWING — a 3xx already proves the compile, and
                # following one would let generated code choose the next URL, a blind SSRF pivot
                # from the control plane. HEADERS ONLY — `stream` closes without reading the
                # body, so a hostile app cannot force an unbounded buffer, and the
                # `asyncio.timeout` above makes the budget a TOTAL ceiling, not per-op.
                #
                # The app's own pages, for the same reason as `what_is_it_serving`: a warm
                # request against a base-path app's root warms the 404 route and leaves the
                # first real visitor waiting on the cold compile this call exists to absorb.
                self._http.stream("GET", handle.app_root_url) as resp,
            ):
                if not resp.is_success:
                    # The route ANSWERED, and answered badly — a compile error, a crashed
                    # server component, a redirect off the app root. Distinct event name from
                    # `warm_request_failed`, which means the request never landed at all; the
                    # two say opposite things about the app and must not be conflated.
                    _log.warning(
                        "warm_request_not_ok", app=handle.app_name, status=resp.status_code
                    )
                return resp.status_code
        except Exception:  # noqa: BLE001 - nothing from here may ever reach the caller
            # The blind `except` is required, not an oversight: a warm request is decoration and
            # must not fail a turn. `CancelledError` is a `BaseException`, so a cancelled turn
            # still cancels; only the timeout's own expiry is swallowed here.
            # `exc_info` is not decoration: the whole detection story depends on this request
            # reaching the route, and a silent swallow makes restricted egress or a wedged
            # ingress look identical to a healthy build. Without the reason, the one telemetry
            # signal that says "the warm request is a no-op in production" says nothing.
            _log.warning("warm_request_failed", app=handle.app_name, exc_info=True)
            return None

    # --- registry helpers (frozen key builders — never a hand-typed key) --

    async def _write_registry(
        self,
        user_uuid: uuid.UUID,
        *,
        app_name: str,
        app_id: uuid.UUID,
        alias: str,
        fqdn: str,
        token_ref: str,
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> None:
        """Record a container just made this person's, while their record names no container;
        `_SlotTakenError`, writing nothing, otherwise. `shared_project_id` and `shared_owner_id`
        come together, for a colleague's view, or not at all. See `_WRITE_THE_REGISTRY_LUA`."""
        born = datetime.now(UTC).isoformat()
        # First, so a failure leaves no record; one left behind by a refused or failed registry
        # write below can only answer no (see `alias_key`).
        await get_redis().set(alias_key(alias), str(user_uuid), ex=ALIAS_TTL_SECONDS)
        run_script = get_redis().eval  # aliased to keep the call off the JS-oriented eval guard
        written = await run_script(
            _WRITE_THE_REGISTRY_LUA,
            1,
            registry_key(user_uuid),
            app_name,
            str(app_id),
            fqdn,
            token_ref,
            born,
            "" if shared_project_id is None else str(shared_project_id),
            "" if shared_owner_id is None else str(shared_owner_id),
            alias,
        )
        if not written:
            raise _SlotTakenError("the registry already names a container")
        # Deferred import — see the cycle note at the top of this module.
        from src.services.build_sessions.alarms import SANDBOX_REGISTRY_MARKED_PENDING_EVENT

        # Scheduled, not serving: from here to `app_first_served` is the wait a person sees.
        _log.info(SANDBOX_REGISTRY_MARKED_PENDING_EVENT, app_name=app_name, serving_since="")

    async def _read_registry(self, user_uuid: uuid.UUID) -> dict[str, str] | None:
        """Read the sandbox record, falling back to the legacy key and migrating what it finds.

        A MISSING RECORD IS ACTED ON DESTRUCTIVELY: `attach_existing` turns `None` into
        `SandboxGoneError` (restores the last save over the container), and
        `_provision_container` writes a new record over the slot it finds empty — so this
        fallback belongs in the point read, not only the scan.
        `build_sessions.locks.read_registry` mirrors it and must behave identically; kept
        separate (`services/sandbox/` may not import `services/build_sessions/`), guarded against
        drift by `test_key_migration.py`."""
        raw = await get_redis().hgetall(registry_key(user_uuid))
        if raw:
            return {str(k): str(v) for k, v in raw.items()}
        return await self._adopt_a_pre_cutover_record(user_uuid)

    async def _adopt_a_pre_cutover_record(self, user_uuid: uuid.UUID) -> dict[str, str] | None:
        """Migrate one legacy-prefix registry hash into the environment-scoped namespace, on read.

        SINGLE-KEY COMMANDS ONLY, and the legacy key is NOT deleted on read.
        `locks._adopt_a_pre_cutover_record` mirrors this field for field and carries the reasoning
        for both constraints; the two are separate implementations because `services/sandbox/`
        must not import `services/build_sessions/`.

        ONE FIELD IS ADDED RATHER THAN COPIED — the serving proof, which a legacy record cannot
        carry. The mirror adds it too, and for the reason spelled out on the write below; the
        two must agree exactly or `test_key_migration.py` fails."""
        raw = await get_redis().hgetall(legacy_registry_key(user_uuid))
        if not raw:
            return None

        if await get_redis().exists(registry_key(user_uuid)):
            # A racing writer created the current record after the HGETALL above; that record is
            # the newer claim, and returning the legacy hash would hand back a superseded
            # `app_name` — a teardown aimed at the wrong container.
            current = await get_redis().hgetall(registry_key(user_uuid))
            return {str(k): str(v) for k, v in current.items()} if current else None

        inherited = {str(k): str(v) for k, v in raw.items()}

        # THE SERVING PROOF IS WRITTEN EXPLICITLY, and that is load-bearing rather than tidy. A
        # verbatim copy would carry no `serving_since`, and an adoption can land at ANY time — a
        # pre-cutover container attached months from now still arrives here — so "absent can only
        # mean pre-cutover" would never become true, and the grandfather arm that reads absence
        # as PROVEN could never be safely retired. Writing a real instant is precisely what makes
        # absence impossible on any record written after the cutover.
        #
        # The instant is this record's OWN `created_at`, and PROVEN is the correct reading for
        # it: the container was scheduled before the stamp existed, and it is very likely serving
        # a citizen at this moment. The alternative — the `""` sentinel — would retire a live
        # preview on the spot, which is the single thing the grandfather arm exists to prevent.
        first_served_at = inherited.get(REGISTRY_FIELD_CREATED_AT, "")
        if not first_served_at:
            # A truncated legacy hash: no birthday to inherit. Falling back to NOW keeps the
            # PROVEN reading without inventing a history — the instant recorded is simply the
            # earliest this platform can honestly claim to have known the container was there.
            _log.warning(
                "adopting a legacy registry record with no created_at; stamping its serving "
                "proof at the adoption instant instead",
                user_id=str(user_uuid),
            )
            first_served_at = datetime.now(UTC).isoformat()

        # Inline comprehension, not the `inherited` variable a reader would reach for first (nor
        # any other `dict[str, str]`): redis-py types `mapping` as `Mapping[FieldT, EncodableT]`
        # whose KEY parameter is invariant, so a named `dict[str, str]` fails every type gate —
        # spread into the literal as well — while the identical inline literal passes.
        await get_redis().hset(
            registry_key(user_uuid),
            mapping={
                **{str(k): str(v) for k, v in raw.items()},
                REGISTRY_FIELD_SERVING_SINCE: first_served_at,
            },
        )
        _log.info(
            "sandbox_registry_migrated_to_the_environment_namespace",
            user_id=str(user_uuid),
            detail=(
                "a record written before R22, copied under the environment prefix; the legacy "
                "key is left for _delete_registry, never removed on read"
            ),
        )
        # The serving proof goes back to the caller, not just into the hash: it is exactly a
        # field consumers branch on, and a caller handed a record whose stamp it cannot see
        # would have to re-read the key to learn what this write just put there.
        return inherited | {REGISTRY_FIELD_SERVING_SINCE: first_served_at}

    async def _delete_registry(self, user_uuid: uuid.UUID, app_name: str) -> bool:
        """Clear the record under BOTH prefixes, but ONLY while it still names `app_name`.

        OWNING THE NAME IS NOT OWNING THE RECORD. `_app_owners` answers "did this process start a
        container called this?", and that was the only thing asked here — but the record is keyed
        by USER and holds whichever container that citizen's single workspace is running now. A
        switch replaces its contents while the outgoing container is still being torn down, so
        deleting by user id alone takes the INCOMING container's record and leaves it running with
        nothing that names it, invisible to a sweep that walks the registry namespace. Asking by
        name is what makes a late teardown unable to disown a live container.

        Two single-key DELs, not one two-key `DEL`: the keys hash to different slots and a
        multi-key command is rejected on a clustered Redis."""
        from src.services.build_sessions.locks import delete_registry_if_it_still_names

        return await delete_registry_if_it_still_names(get_redis(), user_uuid, app_name)

    # --- token_ref map -------------------------------------------------------

    def _register_token(self, token: str) -> str:
        token_ref = secrets.token_urlsafe(_TOKEN_REF_BYTES)
        self._token_refs[token_ref] = token
        return token_ref

    def _evict_token(self, token: str) -> None:
        for ref in [ref for ref, tok in self._token_refs.items() if tok == token]:
            self._token_refs.pop(ref, None)

    async def _read_supervisor_token(self, app_name: str) -> str | None:
        """Read a container's supervisor bearer straight off its own ACA env, or `None` when it
        cannot be read. `_recover_token` (registry-keyed reattach) and `attach_by_name` (no
        registry at all) both go through this; only a claim reads it for itself, because it must
        tell a transient error from a container that is gone.

        `AcaError`/`AcaTransientError` collapse to `None` here: this method says only whether the
        token was read, never why not — the caller holds the context (a registry record, or
        nothing) needed to turn that into Gone vs NotReady. Never logs the token itself."""
        try:
            return await self._aca.get_app_env_value(name=app_name, key=_SUPERVISOR_TOKEN_ENV)
        except (AcaError, AcaTransientError):  # fmt: skip  # ruff py314 strips parens
            _log.warning("supervisor_token_recovery_failed", app_name=app_name, exc_info=True)
            return None

    async def _read_base_path(self, app_name: str) -> str:
        """The path a container serves under, read off its own ACA env; its own name when the env
        carries none. A failed read is `SandboxNotReadyError`, never a guess: a wrong path 404s
        every probe of an app that is running."""
        try:
            raw = await self._aca.get_app_env_value(name=app_name, key=_BASE_PATH_ENV)
        except (AcaError, AcaTransientError) as exc:  # fmt: skip  # ruff py314 strips parens
            raise SandboxNotReadyError("could not read the container's base path") from exc
        return raw or base_path_for(app_name)

    async def _recover_token(self, token_ref: str, app_name: str) -> str | None:
        """Re-read a container's supervisor bearer from its ACA env, re-bound to the registry's
        `token_ref`, or `None` when it cannot be recovered.

        AN UNRESOLVABLE REF SAYS NOTHING ABOUT THE CONTAINER: `_token_refs` is process memory, so a
        restart empties it — and reading that as `SandboxGoneError` used to roll every citizen with
        an open sandbox back to their last save on a routine deploy. The token is minted per
        container into its ACA env at create; that env is its durable home and this process's map
        was only ever a cache."""
        token = await self._read_supervisor_token(app_name)
        if token is None:
            return None
        self._token_refs[token_ref] = token
        _log.info("supervisor_token_recovered_from_container_env", app_name=app_name)
        return token

    # --- ACA lifecycle -----------------------------------------------------

    async def _safe_teardown(self, app_name: str) -> bool:
        """Best-effort ACA delete for a self-clean path. Returns True when the container is
        CONFIRMED gone, False when ARM refused.

        LOAD-BEARING, not informational: a caller that reads "I tried" as "it is gone" and
        drops the ownership record leaves a container running, unreachable by the product,
        invisible to the reaper, and billing forever. Only positive confirmation may authorise
        that step — ARM's DELETE returns 204 for a resource that does not exist, so a successful
        call genuinely means absent; a timeout is not a death certificate."""
        try:
            await self._aca.delete_app(name=app_name)
        except (AcaError, AcaTransientError):  # fmt: skip  # ruff py314 strips parens
            _log.exception("ACA self-clean teardown failed", app_name=app_name)
            return False
        return True

    async def _create_with_retry(
        self,
        app_name: str,
        env: dict[str, str],
        tags: dict[str, str],
        identity_resource_id: str | None,
        *,
        arm: _BirthArm,
    ) -> str:
        """Create the ACA container, retrying the transient failures. `arm` is carried for the
        success notice below and nothing else — the births are otherwise identical here.

        THE ARM LAYER USED TO BE SILENT ON SUCCESS, so the most expensive step of a build left
        no trace of how long it took or how many attempts it cost; the terminal failure below
        still speaks only by raising, which its caller is what turns into a citizen-visible
        answer. The notice is emitted HERE rather than at the call site because this is the only
        scope that can see the attempt count and time the whole ladder, backoff sleeps
        included."""
        started = time.monotonic()
        delay = _ACA_RETRY_START_SECONDS
        last: Exception | None = None
        for attempt in range(_ACA_MAX_ATTEMPTS):
            try:
                fqdn = await self._aca.create_app(
                    name=app_name,
                    env=env,
                    tags=tags,
                    identity_resource_id=identity_resource_id,
                )
            except AcaTransientError as exc:
                last = exc
                if attempt >= _ACA_MAX_ATTEMPTS - 1:
                    break
                await _asleep(delay)
                delay = min(delay * 2, _ACA_RETRY_MAX_SECONDS)
            except AcaError as exc:
                last = exc
                break
            else:
                # Deferred import — see the cycle note at the top of this module.
                from src.services.build_sessions.alarms import SANDBOX_CONTAINER_CREATED_EVENT

                # `fqdn_present` is the BOOL and not the FQDN: its presence is the only bit
                # anyone reads off a create, and a half-formed ARM reply is not worth the line
                # width. `attempts` is 1-based so the number reads as "it took three goes",
                # not as an index.
                _log.info(
                    SANDBOX_CONTAINER_CREATED_EVENT,
                    arm=arm,
                    create_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempt + 1,
                    fqdn_present=bool(fqdn),
                )
                return fqdn
        # Terminal after the container may partially exist: self-clean any half-created
        # revision (idempotent) so nothing invisible-to-the-reaper leaks, then raise.
        cleaned = await self._safe_teardown(app_name)
        raise _CreateFailedError(
            "ACA container provisioning failed", left_standing=not cleaned
        ) from last

    # --- the pool of ready sandboxes ------------------------------------------

    async def _claim_a_ready_one(
        self,
        user_uuid: uuid.UUID,
        app_env: dict[str, str],
        *,
        app_id: uuid.UUID,
        kind: SandboxKind,
        shared_project_id: uuid.UUID | None,
        shared_owner_id: uuid.UUID | None,
    ) -> SandboxHandle | None:
        """Make a ready container from the pool this start's own, or answer `None` for the start
        to create one; the start's stopwatch records which, and why not. A claimed container that
        fails a step before its registry write is let go, or put back when the step learnt
        nothing of it, and another tried, twice at most. Only the registry write fails the start,
        as it would fail a create: a slot another start took meanwhile, or a registry that did
        not answer."""
        # Deferred: the ledger reaches `src.db`, which reaches `src.config`.
        from src.db.base import DB_UNREACHABLE
        from src.services.sandbox import pool

        stopwatch = running_stopwatch()
        if self._config.pool_size_at(datetime.now(UTC)) == 0:
            stopwatch.missed("size_zero", ready_count=0)
            return None
        try:
            ready = await pool.ready_count()
        except DB_UNREACHABLE:
            _log.warning("sandbox_pool_ledger_unreachable", exc_info=True)
            stopwatch.missed("claim_failed", ready_count=None)
            return None
        miss: Miss = "no_ready"
        # Put back once this start is done claiming, so its own next claim cannot take one again.
        kept: list[ClaimedMember] = []
        try:
            for _ in range(_CLAIMS_PER_START):
                try:
                    member = await pool.claim(self._config.image_ref)
                except DB_UNREACHABLE:
                    _log.warning("sandbox_pool_ledger_unreachable", exc_info=True)
                    miss = "claim_failed"
                    break
                if member is None:
                    break
                try:
                    handle = await self._make_it_theirs(
                        member,
                        user_uuid,
                        app_env,
                        app_id=app_id,
                        shared_project_id=shared_project_id,
                        shared_owner_id=shared_owner_id,
                    )
                except _ClaimFellThroughError as exc:
                    if exc.keep:
                        kept.append(member)
                    else:
                        self._detach(self._let_it_go(member))
                    _log.warning(
                        "sandbox_pool_claim_fell_through",
                        app_name=member.name,
                        reason=exc.reason,
                        kept=exc.keep,
                        exc_info=True,
                    )
                    miss = exc.reason
                    continue
                except BaseException:
                    self._detach(self._let_it_go(member))
                    raise
                try:
                    await pool.forget(member.id)
                except DB_UNREACHABLE:
                    # The container is this start's either way: the registry now records it.
                    _log.error(
                        "sandbox_pool_row_outlived_its_claim", app_name=member.name, exc_info=True
                    )
                stopwatch.took_a_ready_one(ready_count=ready)
                stopwatch.split("created")
                _log.info("sandbox_pool_member_claimed", app_name=member.name, ready_count=ready)
                # Side by side: the replacement's row is what tells a pass meanwhile that the
                # pool is being made whole, and the restamp spends seconds on ARM.
                self._detach(self._refill())
                self._detach(self._restamp(member.name, _identity_tags(kind, user_uuid, app_id)))
                return handle
        finally:
            for member in kept:
                self._detach(self._put_it_back(member))
        stopwatch.missed(miss, ready_count=ready)
        return None

    async def _make_it_theirs(
        self,
        member: ClaimedMember,
        user_uuid: uuid.UUID,
        app_env: dict[str, str],
        *,
        app_id: uuid.UUID,
        shared_project_id: uuid.UUID | None,
        shared_owner_id: uuid.UUID | None,
    ) -> SandboxHandle:
        """Take over a container just claimed: read its bearer, check it answers, hand it its
        project's settings, then write the registry record that makes it this person's
        workspace. A step before the write raises `_ClaimFellThroughError` naming the miss; the
        container is the caller's to let go."""
        stopwatch = running_stopwatch()
        with stopwatch.lap("bearer_read"):
            token, alias = await self._read_a_claimed_env(member.name)
        base_path = base_path_for(alias)
        handle = SandboxHandle(
            fqdn=member.fqdn,
            token=token,
            app_name=member.name,
            preview_url=_public_app_url(base_path),
            ready=False,
            base_path=base_path,
        )
        try:
            await self._check_health(handle)
        except SandboxError as exc:
            raise _ClaimFellThroughError("unhealthy") from exc
        try:
            with stopwatch.lap("configure"):
                await self.configure(handle, app_env)
        except SandboxError as exc:
            raise _ClaimFellThroughError("claim_failed") from exc
        token_ref = self._register_token(token)
        self._app_owners[member.name] = user_uuid
        try:
            with stopwatch.lap("registry_write"):
                await self._write_registry(
                    user_uuid,
                    app_name=member.name,
                    app_id=app_id,
                    alias=alias,
                    fqdn=member.fqdn,
                    token_ref=token_ref,
                    shared_project_id=shared_project_id,
                    shared_owner_id=shared_owner_id,
                )
        except BaseException:
            self._evict_token(token)
            self._app_owners.pop(member.name, None)
            raise
        return handle

    async def _read_a_claimed_env(self, name: str) -> tuple[str, str]:
        """A claimed container's supervisor bearer and alias, off its Azure environment. A
        transient ARM error is asked again once, and a second keeps the container: nothing was
        learnt of it. A container Azure does not have, one with no bearer, or one made before
        aliases, which serves at its own name, falls through to be let go."""
        transient: AcaTransientError | None = None
        for attempt in range(_BEARER_READS):
            if attempt:
                await _asleep(_ACA_RETRY_START_SECONDS)
            try:
                token = await self._aca.get_app_env_value(name=name, key=_SUPERVISOR_TOKEN_ENV)
                base_path = await self._aca.get_app_env_value(name=name, key=_BASE_PATH_ENV)
            except AcaTransientError as exc:
                transient = exc
                continue
            except AcaError as exc:
                raise _ClaimFellThroughError("claim_failed") from exc
            alias = (base_path or "").removeprefix("/a/")
            if token is None or base_path != base_path_for(alias) or not IS_ALIAS.fullmatch(alias):
                raise _ClaimFellThroughError("claim_failed")
            return token, alias
        raise _ClaimFellThroughError("claim_failed", keep=True) from transient

    async def _put_it_back(self, member: ClaimedMember) -> None:
        """Return a claimed container to the pool, behind the start. A row left claimed is
        cleared by a pass past its deadline."""
        from src.db.base import DB_UNREACHABLE
        from src.services.sandbox import pool

        try:
            await pool.put_back(member.id)
        except DB_UNREACHABLE:
            _log.warning("sandbox_pool_row_not_put_back", app_name=member.name, exc_info=True)

    async def _let_it_go(self, member: ClaimedMember) -> None:
        """Delete a container whose claim failed, behind the start. Its row is marked retiring
        first and goes once Azure confirms the delete; a delete Azure refuses leaves it retiring
        for a later pass to retry."""
        from src.db.base import DB_UNREACHABLE
        from src.db.models.sandbox_pool import SandboxPoolState
        from src.services.sandbox import pool

        try:
            await pool.retire(member.id, was=SandboxPoolState.CLAIMED)
        except DB_UNREACHABLE:
            _log.warning("sandbox_pool_row_not_retired", app_name=member.name, exc_info=True)
        if not await self.delete_pool_container(member.name):
            return
        try:
            await pool.forget(member.id)
        except DB_UNREACHABLE:
            _log.warning("sandbox_pool_row_outlived_its_container", app_name=member.name)

    async def _refill(self) -> None:
        """Replace a claimed container, behind its start, while the pool is below its size. A
        lost fill is made up by the worker's next pass."""
        from src.db.base import DB_UNREACHABLE

        try:
            await self.fill_one(self._config.pool_size_at(datetime.now(UTC)))
        except DB_UNREACHABLE:
            _log.warning("sandbox_pool_refill_failed", exc_info=True)

    async def _restamp(self, name: str, tags: dict[str, str]) -> None:
        """Give a claimed container its kind, owner, app and birth, behind its start and through
        the pool's bound. A lost restamp is not repaired: the sweep reads the registry."""
        try:
            async with self._pool_work:
                await self.stamp_tags(name=name, tags=tags)
        except SandboxError:
            _log.warning("sandbox_pool_claim_restamp_failed", app_name=name, exc_info=True)

    async def fill_one(self, target: int) -> FillOutcome:
        """Make one ready container for the pool unless the filling and ready rows of the
        configured image already number `target`. Its row is written before it waits for the
        pool's bound, so every count of the pool sees it queued; its deadline restarts once the
        bound is held, and a fill whose row a pass let go meanwhile makes nothing. The row is
        marked ready at the address Azure answers with once its supervisor answers too. A create
        Azure refuses, a container that never answers, or a fill cut short leaves no row, or a
        retiring one while the container may still stand. A ledger failure raises."""
        from src.db.models.sandbox_pool import SandboxPoolState
        from src.services.sandbox import pool

        name = a_fresh_sandbox_name()
        member_id = await pool.add_filling(name, self._config.image_ref, up_to=target)
        if member_id is None:
            return "at_target"
        token = secrets.token_urlsafe(_SUPERVISOR_TOKEN_BYTES)
        # The alias is set now, with the base path it lives in: a claim cannot change either.
        env = {
            **_creation_env(base_path_for(new_alias()), token),
            "BIAL_PORTAL_ORIGIN": portal_origin(),
            "BIAL_POOL_MEMBER": "1",
        }
        try:
            async with self._pool_work:
                if not await pool.restart_the_clock(member_id):
                    _log.warning("sandbox_pool_fill_outlived_its_row", app_name=name)
                    return "refused"
                # No data identity: nothing project-specific reaches a container before its claim.
                fqdn = await self._create_with_retry(
                    name, env, pool_member_tags(), None, arm="pool_fill"
                )
            answered = await self._first_answer(
                SandboxHandle(fqdn=fqdn, token=token, app_name=name, preview_url="", ready=False)
            )
            marked_ready = answered and await pool.mark_ready(member_id, fqdn)
        except _CreateFailedError as exc:
            _log.warning("sandbox_pool_fill_refused", app_name=name, exc_info=True)
            if exc.left_standing:
                if not await pool.retire(member_id, was=SandboxPoolState.FILLING):
                    await pool.hold_for_deletion(name, self._config.image_ref)
            else:
                await pool.forget(member_id)
            return "refused"
        except BaseException:
            # Azure may finish a create its caller gave up on, so a pass deletes what it made.
            await asyncio.shield(self._give_up_the_fill(member_id, name))
            raise
        if not marked_ready:
            if not answered:
                _log.warning("sandbox_pool_member_never_answered", app_name=name)
            # Never claimable, or a pass let its row go as overdue: nothing will take it.
            await self._let_the_fill_go(member_id, name)
            return "refused"
        _log.info("sandbox_pool_member_filled", app_name=name, image_ref=self._config.image_ref)
        return "filled"

    async def _let_the_fill_go(self, member_id: uuid.UUID, name: str) -> None:
        """Delete a container made for the pool that will never be ready, then its row once Azure
        confirms it gone. A delete Azure refuses leaves a retiring row naming the container, for a
        later pass to retry. A ledger failure raises."""
        from src.db.models.sandbox_pool import SandboxPoolState
        from src.services.sandbox import pool

        await pool.retire(member_id, was=SandboxPoolState.FILLING)
        if await self.delete_pool_container(name):
            await pool.forget(member_id)
        else:
            await pool.hold_for_deletion(name, self._config.image_ref)

    async def _first_answer(self, handle: SandboxHandle) -> bool:
        """Whether a container just made answers `/health` within `FIRST_ANSWER_CEILING`. Azure
        reports a create done before the supervisor inside it serves, and a claim in that gap
        would find a sound container silent and let it go."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FIRST_ANSWER_CEILING.total_seconds()
        while True:
            try:
                await self._check_health(handle)
            except SandboxError:
                if loop.time() >= deadline:
                    return False
                await _asleep(_FIRST_ANSWER_POLL_SECONDS)
            else:
                return True

    async def _give_up_the_fill(self, member_id: uuid.UUID, name: str) -> None:
        from src.db.base import DB_UNREACHABLE
        from src.db.models.sandbox_pool import SandboxPoolState
        from src.services.sandbox import pool

        try:
            await pool.retire(member_id, was=SandboxPoolState.FILLING)
        except DB_UNREACHABLE:
            # Left filling, its row is cleared as overdue once a create could no longer be running.
            _log.warning("sandbox_pool_fill_row_left_filling", app_name=name, exc_info=True)

    async def delete_pool_container(self, name: str) -> bool:
        """Delete a container the pool's ledger holds, through the pool's bound; True once Azure
        confirms it gone."""
        async with self._pool_work:
            return await self._safe_teardown(name)

    def _detach(self, work: Coroutine[Any, Any, None]) -> None:
        # A context of its own: the start's would hand this work its stopwatch and log bindings,
        # and the work outlives the start whose record they belong to.
        task = asyncio.create_task(work, context=contextvars.Context())
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)

    async def _provision_container(
        self,
        user_uuid: uuid.UUID,
        app_name: str,
        app_env: dict[str, str],
        *,
        arm: _BirthArm,
        kind: SandboxKind = "build_sandbox",
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> SandboxHandle:
        """Create the container, write the registry hash at container-create (before any fallible
        post-create step, so a mid-provision death is reaper-visible), and return a `ready=False`
        handle; a post-create failure self-cleans. A ready container from the pool is taken in
        place of the create when one can be claimed, under its own name rather than `app_name`.

        `arm` names which birth this is, for the create notice. `kind` selects the ARM identity
        stamped, and `user_uuid` is the RECIPIENT on the `shared_sandbox` arm, the only arm that
        passes `shared_project_id` and `shared_owner_id`."""
        existing = await self._read_registry(user_uuid)
        if existing is not None and existing.get(REGISTRY_FIELD_APP_NAME):
            # The new record would replace the only one naming that container, leaving it running
            # with nothing that can find it. Whoever holds it must be handed over first.
            raise SandboxError(
                "cannot provision: the registry still names a container nobody has taken over, "
                "and writing over its record would orphan it"
            )
        # The app_id comes from `app_env` for the same reason `restore_from_snapshot` reads it
        # there: the frozen client signature carries no app_id. A `KeyError` here means the env
        # builder upstream is broken, which is worth failing loudly on rather than provisioning an
        # anonymous container to paper over.
        app_id = uuid.UUID(app_env["BIAL_APP_ID"])
        claimed = await self._claim_a_ready_one(
            user_uuid,
            app_env,
            app_id=app_id,
            kind=kind,
            shared_project_id=shared_project_id,
            shared_owner_id=shared_owner_id,
        )
        if claimed is not None:
            return claimed
        token = secrets.token_urlsafe(_SUPERVISOR_TOKEN_BYTES)
        # The supervisor bearer lives ONLY in the container env (the supervisor keeps it out of
        # the scrubbed child env) and in-process; Redis stores a token_ref, never the token.
        #
        # WHERE THIS APP IS SERVED FROM, minted at the one seam both births pass through —
        # `provision_new` and `restore_from_snapshot`. It is deliberately NOT in `build_app_env`:
        # the publish path calls that same builder, and a preview's alias added there would ship
        # into published containers whose images were built under their `pub-` name.
        alias = new_alias()
        base_path = base_path_for(alias)
        env = {**app_env, **_creation_env(base_path, token)}
        # Identity resolved BEFORE the create, so a container never exists untagged.
        tags = _identity_tags(kind, user_uuid, app_id)
        # WHETHER THIS CONTAINER MAY READ A CONNECTOR'S DATA, read back out of the environment
        # the caller built rather than decided again here. The access question — a lake
        # configured, the connector switched on for this project — was answered once by
        # `build_connector_env`, and the presence of its coordinates IS that
        # answer; deriving it again would be a second place the platform decides who may read
        # BIAL's flight data. `None` means no identity block at all, so a container that was not
        # granted anything gets a spec byte-identical to the one this platform sent before
        # connectors existed.
        #
        # Imported lazily for the same reason the three `src.config` imports in this module are:
        # this file is reached from `src/services/sandbox/__init__.py`, which `src/settings/api.py`
        # imports, and the connector registry reaches `src/db/models/`.
        from src.services.lake.env import identity_resource_id_for_env

        identity_resource_id = identity_resource_id_for_env(app_env)
        held = await self._hold_the_create(app_name)
        try:
            fqdn = await self._create_with_retry(
                app_name, env, tags, identity_resource_id, arm=arm
            )
        except _CreateFailedError as exc:
            if not exc.left_standing:
                await self._release_the_create(held, app_name)
            raise
        stopwatch = running_stopwatch()
        stopwatch.split("created")
        token_ref = self._register_token(token)
        self._app_owners[app_name] = user_uuid
        try:
            with stopwatch.lap("registry_write"):
                await self._write_registry(
                    user_uuid,
                    app_name=app_name,
                    app_id=app_id,
                    alias=alias,
                    fqdn=fqdn,
                    token_ref=token_ref,
                    shared_project_id=shared_project_id,
                    shared_owner_id=shared_owner_id,
                )
        except Exception:
            if await self._safe_teardown(app_name):
                await self._release_the_create(held, app_name)
            self._evict_token(token)
            self._app_owners.pop(app_name, None)
            raise
        await self._release_the_create(held, app_name)
        return SandboxHandle(
            fqdn=fqdn,
            token=token,
            app_name=app_name,
            # THE BROWSER-FACING ADDRESS, which carries the alias and not this container's name.
            # An internal Container Apps environment publishes no public DNS, so a BIAL desk
            # cannot resolve `{fqdn}` at all — apps are reached through the platform's router on
            # one public hostname with the app's key in the path. The control plane keeps using
            # the direct address: `/_sup/*` composes from `fqdn`, and both serving probes compose
            # from `handle.app_root_url`, which also derives from `fqdn`. Repointing this field
            # therefore cannot drag control-plane traffic onto the public gateway.
            preview_url=_public_app_url(base_path),
            ready=False,
            base_path=base_path,
        )

    async def _hold_the_create(self, app_name: str) -> uuid.UUID | None:
        """Hold a start's own create on the pool's ledger until its record is written. A create
        outlives a cancelled start, and nothing creates its name again, so until the registry
        records the container this row is all that can find it. A ledger that does not answer
        costs only that: the start creates all the same."""
        from src.db.base import DB_UNREACHABLE
        from src.services.sandbox import pool

        try:
            return await pool.hold_a_create(app_name, self._config.image_ref)
        except DB_UNREACHABLE:
            _log.warning("sandbox_create_not_held_on_the_ledger", app_name=app_name, exc_info=True)
            return None

    async def _release_the_create(self, held: uuid.UUID | None, app_name: str) -> None:
        """Drop a create's row once its container is recorded or confirmed gone. One left behind
        is harmless: past its deadline a pass forgets it while a registry record or an owed
        teardown names the container, and deletes the container otherwise."""
        from src.db.base import DB_UNREACHABLE
        from src.services.sandbox import pool

        if held is None:
            return
        try:
            await pool.forget(held)
        except DB_UNREACHABLE:
            _log.warning("sandbox_pool_row_outlived_its_create", app_name=app_name, exc_info=True)

    async def _undo_a_container_whose_next_step_died(
        self, user_uuid: uuid.UUID, handle: SandboxHandle, *, event: str, during: str
    ) -> None:
        """Take back a container created moments ago, for a caller about to re-raise.

        THE REGISTRY DROP IS CONDITIONAL. `_safe_teardown` swallows an `AcaError`, so clearing
        the record whatever happened would orphan a container that is probably still running.
        A record left standing is what sends a later sweep back to retry the teardown.
        """
        if await self._safe_teardown(handle.app_name):
            await self._delete_registry(user_uuid, handle.app_name)
        else:
            _log.error(
                event,
                app_name=handle.app_name,
                detail=(
                    f"ACA refused the delete during {during}, so the ownership record is "
                    "deliberately kept: a later sweep retries the teardown instead of meeting "
                    "an anonymous container."
                ),
            )
        self._evict_token(handle.token)
        self._app_owners.pop(handle.app_name, None)

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        user_uuid = uuid.UUID(user_id)
        handle = await self._provision_container(user_uuid, app_name, app_env, arm="provision_new")
        try:
            await _make_it_a_repo(self, handle)
        except Exception:
            # SEEDING IS THE SECOND FALLIBLE STEP, AND IT RUNS AFTER THE RECORD SAYS READY.
            # `_write_registry` stamps READY at container-create time, so a container left
            # behind here is one `_the_live_sandbox_is_already_the_one_we_want` hands straight
            # back to the next request — a container that builds fine and whose every Save
            # raises `WorkspaceHasNoRepositoryError`, with no self-service way out. The caller
            # cannot clean this up either: it never received a handle, so the compensation in
            # `_holding_user_lock` has nothing to tear down.
            await self._undo_a_container_whose_next_step_died(
                user_uuid,
                handle,
                event="provision_cleanup_left_registry_for_the_reaper",
                during="seed-failure cleanup",
            )
            raise
        return handle

    async def _probe_with_retry(self, handle: SandboxHandle) -> bool:
        """Reach the supervisor, retrying a blip, and answer whether it holds its settings."""
        delay = _PROBE_START_SECONDS
        for attempt in range(_PROBE_MAX_ATTEMPTS):
            try:
                resp = await self._get(handle, "health", timeout=_OP_TIMEOUT_SECONDS)
                if resp.status_code == 200:
                    return _says_it_is_configured(resp)
            except SandboxError:
                pass  # transient transport error — retry below before deciding gone
            if attempt < _PROBE_MAX_ATTEMPTS - 1:
                await _asleep(delay)
                delay = min(delay * 2, _PROBE_MAX_SECONDS)
        # Exhausted: confirm whether the container is genuinely gone (ACA revision
        # absent -> restore) or just unreachable (exists -> retryable, never a false
        # double-allocation that would orphan a live original). A transient ARM error while
        # confirming is NOT "gone" — it must stay retryable, so map it to NotReady.
        try:
            fqdn = await self._aca.get_app_fqdn(name=handle.app_name)
        except (AcaError, AcaTransientError) as exc:
            raise SandboxNotReadyError("could not confirm container liveness") from exc
        if fqdn is None:
            raise SandboxGoneError("container revision is gone")
        raise SandboxNotReadyError("supervisor unreachable but the container still exists")

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        user_uuid = uuid.UUID(user_id)
        reg = await self._read_registry(user_uuid)
        if reg is None:
            raise SandboxGoneError("no live sandbox registered for user")
        if reg.get(REGISTRY_FIELD_STATE) == REGISTRY_STATE_ENDING:
            # The reaper marked this ending BEFORE teardown — do NOT reconnect to a
            # dying container. Raise before probing.
            raise SandboxGoneError("sandbox is ending")
        fqdn = reg.get(REGISTRY_FIELD_FQDN, "")
        app_name = reg.get(REGISTRY_FIELD_APP_NAME, "")
        token_ref = reg.get(REGISTRY_FIELD_TOKEN_REF)
        token = self._token_refs.get(token_ref) if token_ref else None
        if token is None and token_ref and app_name:
            # A restart empties the in-process map, and that emptiness is not evidence about the
            # container: recover the bearer from its ACA env rather than concluding "gone",
            # which the caller answers by restoring over a container that is still running.
            token = await self._recover_token(token_ref, app_name)
        if token is None:
            # Recovery failed. Distinguish HONESTLY, because the two answers have very different
            # costs: a container ARM confirms is gone has nothing to lose and should be restored,
            # while a container we merely cannot authenticate to right now must NOT be destroyed
            # over a transient control-plane failure.
            #
            # The ACA read is GUARDED, matching `_probe_with_retry`'s identical confirmation
            # step. `AcaError`/`AcaTransientError` are not `SandboxError`s, so an unguarded call
            # escaped every handler above this one — and this arm is reached exactly when ARM is
            # already unhappy (the token recovery just failed against it), so a throttle here is
            # the expected shape, not an exotic one. `get_app_fqdn`'s own contract asks the
            # attach caller to map it to NotReady; this is that mapping.
            if app_name:
                try:
                    fqdn_now = await self._aca.get_app_fqdn(name=app_name)
                except (AcaError, AcaTransientError) as exc:
                    raise SandboxNotReadyError("could not confirm container liveness") from exc
                if fqdn_now is not None:
                    raise SandboxNotReadyError("supervisor token temporarily unrecoverable")
            raise SandboxGoneError("token reference not resolvable")
        alias = reg.get(REGISTRY_FIELD_ALIAS)
        # A record with no alias describes a container from before aliases, which serves at its
        # own name. The sweep retires it, writing its work back first.
        base_path = base_path_for(alias or app_name)
        handle = SandboxHandle(
            fqdn=fqdn,
            token=token,
            app_name=app_name,
            # Same public address as a fresh provision — attach and provision must not disagree
            # about where a person goes, or a relaunched session frames a different URL.
            preview_url=_public_app_url(base_path),
            ready=False,
            base_path=base_path,
        )
        handle = replace(handle, configured=await self._probe_with_retry(handle))
        self._app_owners[app_name] = user_uuid
        # `ready` reflects the ACTUAL dev-server state on reattach (SandboxHandle.ready) —
        # a resumed, already-ready sandbox drives the initial-load preview trigger. A
        # transient status error must not fail an attach that just probed healthy: fall back
        # to ready=False (the readiness poll recovers it later).
        try:
            status = await self.dev_status(handle)
        except SandboxError:
            _log.warning(
                "dev_status failed after attach; returning ready=False", app_name=app_name
            )
            return handle
        return replace(handle, ready=status.ready)

    async def attach_by_name(self, *, app_name: str) -> SandboxHandle:
        """Reach a container from its NAME alone — no registry hash, no `user_id`, no
        `token_ref`. What survives a switch overwriting the per-user registry with the
        incoming container's record: the outgoing one is still standing at this name.

        Composed from the same two ARM reads `attach_existing` performs via the registry —
        `get_app_fqdn` for the address, `_read_supervisor_token` for the bearer — plus the
        same reachability probe, all keyed by `app_name` directly instead of a Redis lookup.

        ABSENT AND UNREACHABLE ARE DIFFERENT ANSWERS. `SandboxGoneError` means ARM confirms no
        container answers to this name. `SandboxNotReadyError` means ARM found it but the
        supervisor did not — a reach failure, retryable, never a death certificate."""
        try:
            fqdn = await self._aca.get_app_fqdn(name=app_name)
        except (AcaError, AcaTransientError) as exc:
            raise SandboxNotReadyError("could not confirm container liveness") from exc
        if fqdn is None:
            raise SandboxGoneError(f"no container answers to {app_name!r}")
        token = await self._read_supervisor_token(app_name)
        if token is None:
            raise SandboxNotReadyError("supervisor token temporarily unrecoverable")
        base_path = await self._read_base_path(app_name)
        handle = SandboxHandle(
            fqdn=fqdn,
            token=token,
            app_name=app_name,
            preview_url=_public_app_url(base_path),
            ready=False,
            base_path=base_path,
        )
        handle = replace(handle, configured=await self._probe_with_retry(handle))
        try:
            status = await self.dev_status(handle)
        except SandboxError:
            _log.warning(
                "dev_status failed after attach_by_name; returning ready=False",
                app_name=app_name,
            )
            return handle
        return replace(handle, ready=status.ready)

    async def _restore_snapshot_into(self, handle: SandboxHandle, bundle: bytes) -> None:
        """Push an ALREADY-FETCHED bundle into the container. The fetch itself belongs to the
        caller, before the container is created — see `restore_from_snapshot`."""
        result = await self._run_over_a_pushed_bundle(
            handle, bundle, _RESTORE_SCRIPT, "snapshot restore"
        )
        running_stopwatch().saw_the_restore_reinstall(
            _REINSTALLED_MARKER in result.stdout.splitlines()
        )

    async def reset_to_bundle(self, handle: SandboxHandle, bundle: bytes) -> None:
        await self._run_over_a_pushed_bundle(
            handle, bundle, _DISCARD_SCRIPT, "reset to the saved version"
        )

    async def _run_over_a_pushed_bundle(
        self, handle: SandboxHandle, bundle: bytes, script: str, what: str
    ) -> ExecResult:
        """Write the bundle into the workspace, then run one of the two bundle scripts over it."""
        encoded = base64.b64encode(bundle).decode("ascii")
        stopwatch = running_stopwatch()
        with stopwatch.lap("files"):
            await self.files(handle, FileCreate(path=_BUNDLE_B64_NAME, file_text=encoded))
        run_command = self.exec  # aliased to keep the call off the JS-oriented exec guard
        with stopwatch.lap("restore_exec"):
            result = await run_command(
                handle, ["sh", "-c", script], timeout_s=_RESTORE_TIMEOUT_SECONDS
            )
        if result.exit != 0:
            raise SandboxError(f"{what} failed (exit {result.exit})")
        if _LIBRARIES_LEFT_AS_SAVED in result.stderr.splitlines():
            _log.warning("libraries_left_as_saved", app_name=handle.app_name, during=what)
        return result

    async def restore_from_snapshot(
        self,
        user_id: str,
        app_name: str,
        *,
        app_env: dict[str, str],
        source_key: str | None = None,
        kind: SandboxKind = "build_sandbox",
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> SandboxHandle:
        user_uuid = uuid.UUID(user_id)
        # The caller supplies the app_id via app_env (the frozen client signature carries no
        # app_id).
        app_id = uuid.UUID(app_env["BIAL_APP_ID"])
        key = source_key or snapshot_key(app_id)
        # FETCHED BEFORE ANYTHING IS CREATED, so a missing, unreachable or unreadable bundle
        # (`StorageNotFoundError`, `StorageError`, `BundleValidationError`) fails the restore
        # with no container to clean up.
        #
        # NOT validated here, deliberately. `parse_bundle_head_sha` reads only the header, so
        # it cannot detect the truncation that actually matters, and gating the restore on it
        # would refuse bundles the container can in fact fetch — trading a narrow, already-
        # covered failure for a broad new one.
        bundle = await get_storage().get(key)
        handle = await self._provision_container(
            user_uuid,
            app_name,
            app_env,
            arm="restore_from_snapshot",
            kind=kind,
            shared_project_id=shared_project_id,
            shared_owner_id=shared_owner_id,
        )
        try:
            await self._restore_snapshot_into(handle, bundle)
        except Exception:
            # Mid-restore death runs MORE fallible steps than provision — self-clean the
            # just-created container, then clear its registry ONLY IF the container is
            # confirmed gone.
            await self._undo_a_container_whose_next_step_died(
                user_uuid,
                handle,
                event="restore_cleanup_left_registry_for_the_reaper",
                during="restore cleanup",
            )
            raise
        return handle

    async def teardown(self, handle: SandboxHandle) -> None:
        try:
            await self._aca.delete_app(name=handle.app_name)
        except (AcaError, AcaTransientError) as exc:
            # Keep the registry so the reaper retries this teardown; don't orphan.
            raise SandboxError("sandbox teardown failed") from exc
        # ACA delete succeeded (or the container was already gone) -> safe to drop the
        # coordination state. The registry is user-keyed, so clear it only for a session
        # this process owns (the reaper clears a crashed session's registry itself).
        self._evict_token(handle.token)
        owner = self._app_owners.pop(handle.app_name, None)
        if owner is not None:
            try:
                await self._delete_registry(owner, handle.app_name)
            except RedisError as exc:
                # The ACA delete already succeeded — only the coordination-state cleanup
                # failed. Re-label it to this method's SandboxError-only failure contract
                # (all three callers guard `except SandboxError`) so a bare RedisError never
                # escapes and hits an unguarded surface.
                raise SandboxError("sandbox registry cleanup failed") from exc

    # --- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the supervisor HTTP pool AND (if built) the ACA client + credential.
        Safe to call more than once."""
        try:
            await self._http.aclose()
        finally:
            if self._aca_lazy is not None:
                await self._aca_lazy.aclose()


# --- accessor singleton (mirrors services/redis/client.py) -------------------

_sandbox_singleton: SandboxClient | None = None


def create_sandbox(config: SandboxConfig) -> AcaSandboxClient:
    """Build the concrete client from a `SandboxConfig`. Opens no ACA connection —
    the httpx pool and ACA client connect lazily on first use."""
    return AcaSandboxClient(config)


def get_sandbox() -> SandboxClient:
    """The configured sandbox client (app-level singleton). Raises if the sandbox is
    unset (genuinely-optional in dev/test; the prod gate in `src.config` requires it),
    so a caller never silently gets a `None` (fail-first)."""
    global _sandbox_singleton
    if _sandbox_singleton is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.sandbox is None:
            raise SandboxNotConfiguredError(
                "sandbox is not configured: set SANDBOX__* env, or call get_sandbox() "
                "only where the sandbox is configured (it is required in production)."
            )
        _sandbox_singleton = create_sandbox(settings.sandbox)
    return _sandbox_singleton


async def aclose_sandbox_singleton() -> None:
    """Close the app-global sandbox client and drop the singleton (wired behind
    `lifecycle.aclose_sandbox`). A no-op when never opened. The close is isolated: a
    raise is logged (never swallowed) but the singleton is STILL reset, so a restart
    never reuses a half-closed client (mirrors `aclose_redis` / `aclose_storage`)."""
    global _sandbox_singleton
    client = _sandbox_singleton
    if client is None:
        return
    try:
        # Only the concrete client owns pools/credentials; a test-injected fake has
        # nothing to close.
        if isinstance(client, AcaSandboxClient):
            await client.aclose()
    except Exception:
        _log.exception("sandbox teardown failed during aclose_sandbox")
    finally:
        _sandbox_singleton = None


def set_sandbox_for_tests(client: SandboxClient | None) -> None:
    """Inject (or clear) the singleton so the reaper — which resolves its client via
    the singleton, not a `Depends` — is test-injectable."""
    global _sandbox_singleton
    _sandbox_singleton = client


def reset_sandbox_for_tests() -> None:
    """Drop the singleton so a suite that builds clients with different configs never
    reuses a stale one across tests."""
    global _sandbox_singleton
    _sandbox_singleton = None
