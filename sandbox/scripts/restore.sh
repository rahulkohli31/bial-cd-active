#!/bin/sh
# C4 restore mechanics — runs INSIDE a FRESH sandbox via the frozen `/exec` (as appuser).
#
# The reference client base64-ENCODES the raw bundle it fetched from storage and writes it to a
# text file via `/files create`; this script DECODES it and overlays the snapshot onto the
# pre-baked (non-empty, NON-repo) workspace: git init → fetch the bundle's refs → checkout -f.
#
# Overlay semantics (C4 / U16): `checkout -f` forces the snapshot's tracked files over the baked
# tree but does NOT delete untracked files — so the baked node_modules/.next (excluded from the
# bundle, and REQUIRED for `next dev`) survive. `git clean` is deliberately NOT used: it would nuke
# that baked node_modules. Snapshot files win; a baked source file the user deleted reappears (an
# accepted POC bound).
#
# Cross-platform: LF-only, POSIX sh, `base64 -d` read from STDIN (a positional file arg is a GNU-only
# spelling BSD/macOS rejects). No shell-injection surface (quoted paths).
set -eu

WORKSPACE="${1:-/workspace/app}"
B64="${2:?usage: restore.sh <workspace> <base64-bundle-file>}"

BUNDLE="$(mktemp)"
base64 -d < "$B64" > "$BUNDLE"

cd "$WORKSPACE"
git init -q
# Fetch the bundle's branches into a REMOTES namespace — never directly into refs/heads/*, because
# `git init` leaves HEAD on an unborn `main` and git refuses to fetch into the checked-out branch.
git fetch -q "$BUNDLE" '+refs/heads/*:refs/remotes/snapshot/*'
# Materialize the snapshot: create/reset local `main` from its ref and force the working tree onto
# the baked files. `-f` overwrites baked files that the snapshot also tracks; baked-only untracked
# files (node_modules/.next) are left in place (overlay semantics — see the header).
SNAP="$(git for-each-ref --count=1 --format='%(refname)' refs/remotes/snapshot)"
git checkout -f -B main "$SNAP"

# The transient bundle + the /files-written base64 (root-owned; rm needs only workspace-dir write).
rm -f "$BUNDLE" "$B64"
