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
# Clean up the transient bundle AND the caller's /files-written base64 on EVERY exit path. This is
# load-bearing: a mid-restore failure (after `git init`) that left the multi-MB base64 in the
# workspace would be swept up by a later snapshot.sh `git add -A` and committed into every future
# bundle — a real correctness + size regression, not just a leak.
trap 'rm -f "$BUNDLE" "$B64"' EXIT
base64 -d < "$B64" > "$BUNDLE"

cd "$WORKSPACE"
git init -q
# Fetch the bundle's branches into a REMOTES namespace — never directly into refs/heads/*, because
# `git init` leaves HEAD on an unborn `main` and git refuses to fetch into the checked-out branch.
git fetch -q "$BUNDLE" '+refs/heads/*:refs/remotes/snapshot/*'
# Materialize the snapshot: create/reset local `main` from the bundle's `main` and force the working
# tree onto the baked files. snapshot.sh always commits on `main` (init.defaultBranch=main, baked into
# the image), so target that ref EXPLICITLY — an unordered `for-each-ref --count=1` would pick the
# alphabetically-first branch and silently restore stale work if the workspace ever grew a 2nd branch.
# `-f` overwrites baked files the snapshot also tracks; baked-only untracked files (node_modules/.next)
# are left in place (overlay semantics — see the header).
git checkout -f -B main refs/remotes/snapshot/main
