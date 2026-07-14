#!/bin/sh
# C4 snapshot mechanics — runs INSIDE the sandbox via the frozen `/exec` (as the demoted appuser).
#
# git-commit the workspace, bundle its FULL history to one file, and emit that bundle as base64 to
# stdout. base64 is TRANSPORT ONLY (D3): the reference client base64-DECODES stdout to raw bytes
# before `storage.put`, so the object at snapshots/{app_id}/app.bundle is a RAW git bundle, not
# base64 text. node_modules/.next (+ any .env) are excluded by the template .gitignore, keeping the
# bundle small (C4).
#
# Cross-platform: LF-only, POSIX sh, and `base64` read from STDIN (a positional file arg is a
# GNU-only spelling BSD/macOS rejects — stdin works on both, so the offline test can run this literal
# script). base64 wraps at 76 cols; both the Python client's `b64decode` and `base64 -d` discard the
# newlines, so it round-trips cleanly. Built on the Windows VM via `az acr build` (ADR-0015). No
# shell-injection surface: the only input is the workspace path, quoted; no eval.
set -eu

WORKSPACE="${1:-/workspace/app}"
cd "$WORKSPACE"

# The baked workspace is NOT a git repo; `git init` is idempotent (a re-snapshot re-runs it safely).
git init -q

git add -A

# Empty-diff guard (C4 correctness): `git commit` exits NON-ZERO on a clean tree. Treated naively
# that looks like a snapshot FAILURE and, under the C4 abort-before-teardown rule, wedges the lock
# forever. So commit ONLY when something is staged; a no-op re-snapshot still bundles HEAD below.
if ! git diff --cached --quiet; then
    git commit -q -m "bial workspace snapshot"
fi

# Serialize the whole history to a temp bundle (a real file so `set -e` catches a bundle failure —
# dash has no `pipefail`), then base64-encode it to stdout (transport only; the client decodes).
# `trap ... EXIT` removes the temp file on EVERY path — a bundle failure on a long-lived container is
# abort-before-teardown (the container keeps running, C4), so a manual-only `rm` would leak on retry.
BUNDLE="$(mktemp)"
trap 'rm -f "$BUNDLE"' EXIT
git bundle create "$BUNDLE" --all
base64 < "$BUNDLE"
