#!/bin/sh
# Snapshot mechanics — runs INSIDE the sandbox via the frozen `/exec` (as the demoted appuser).
#
# git-commit the workspace, bundle its FULL history to one file, and emit that bundle as base64 to
# stdout. base64 is TRANSPORT ONLY: the reference client base64-DECODES stdout to raw bytes
# before `storage.put`, so the object at snapshots/{app_id}/app.bundle is a RAW git bundle, not
# base64 text. node_modules/.next (+ any .env) are excluded by the template .gitignore, keeping the
# bundle small.
#
# Cross-platform: LF-only, POSIX sh, and `base64` read from STDIN (a positional file arg is a
# GNU-only spelling BSD/macOS rejects — stdin works on both, so the offline test can run this literal
# script). base64 wraps at 76 cols; both the Python client's `b64decode` and `base64 -d` discard the
# newlines, so it round-trips cleanly. Built on the Windows VM via `az acr build`. No
# shell-injection surface: the only input is the workspace path, quoted; no eval.
set -eu

WORKSPACE="${1:-/workspace/app}"
cd "$WORKSPACE"

# NEVER CREATE A REPOSITORY HERE. The repo is seeded at provision, so a workspace that reaches this
# script without one has LOST it — and a root commit written here would hold the finished app, which
# makes "is this still the starter page?" compare the app against itself forever. Exit 64 says "no
# repository" and nothing else; a full disk or a locked index keeps its own non-zero exit.
git rev-parse --git-dir >/dev/null 2>&1 || exit 64

git add -A

# Empty-diff guard: `git commit` exits NON-ZERO on a clean tree. Treated naively that looks like a
# snapshot FAILURE and, under the abort-before-teardown rule, wedges the lock forever. So commit
# ONLY when something is staged; a no-op re-snapshot still bundles HEAD below.
if ! git diff --cached --quiet; then
    git commit -q -m "bial workspace snapshot"
fi

# Serialize the whole history to a temp bundle (a real file so `set -e` catches a bundle failure —
# dash has no `pipefail`), then base64-encode it to stdout (transport only; the client decodes).
# `trap ... EXIT` removes the temp file on EVERY path — a bundle failure on a long-lived container is
# abort-before-teardown (the container keeps running), so a manual-only `rm` would leak on retry.
BUNDLE="$(mktemp)"
trap 'rm -f "$BUNDLE"' EXIT
git bundle create "$BUNDLE" --all
base64 < "$BUNDLE"
