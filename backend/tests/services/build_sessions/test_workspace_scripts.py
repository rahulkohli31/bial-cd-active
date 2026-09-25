"""The workspace's git scripts, run exactly as a sandbox runs them.

Each test runs the LITERAL strings the control plane sends over `/exec` under `sh`, with real
`git`, and with `core.excludesFile` set to the image's `sandbox/platform-owned.gitignore`. No
test supplies a workspace `.gitignore`: `az acr build` drops every file of that name from the
build context, so the image a sandbox runs carries none.

`backend/Dockerfile.gates` deselects this module: its build context holds no `sandbox/`."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.services.build_sessions.integrity import (
    clean_but_for_churn,
    is_the_untouched_starter,
    parse_state,
    state_script,
)
from src.services.build_sessions.snapshot import _COMMIT_SCRIPT, _NO_REPOSITORY_EXIT
from src.services.sandbox.client import _BUNDLE_B64_NAME, _INIT_REPO_SCRIPT, _RESTORE_SCRIPT

_EXCLUDE_FILE = Path(__file__).resolve().parents[4] / "sandbox" / "platform-owned.gitignore"

_STARTER = {
    "app/page.tsx": "export default function Page() { return null }\n",
    "package.json": '{ "name": "app" }\n',
    "next-env.d.ts": '/// <reference types="next" />\n',
    "node_modules/next/package.json": '{ "name": "next" }\n',
}

#: What `next dev` and the baked dependencies leave tracked in a workspace whose image carried no
#: ignore rules.
_TOOLCHAIN_OUTPUT = frozenset(
    {".next/dev/trace", "node_modules/next/package.json", "next-env.d.ts"}
)

_GIT_CONFIG = """\
[user]
\tname = BIAL Snapshot Bot
\temail = snapshot-bot@bial.local
[init]
\tdefaultBranch = main
"""


def _shims(directory: Path) -> None:
    """Two stand-ins, first on PATH. `npm`: the restore's dependency reconcile is not under test,
    and must never reach the registry. `base64`: the restore passes its input file as an argument,
    which the image's GNU `base64` accepts and BSD's refuses; stdin works under both."""
    directory.mkdir()
    real_base64 = shutil.which("base64")
    assert real_base64 is not None
    (directory / "npm").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (directory / "base64").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = -d ] && [ -f "$2" ]; then exec {real_base64} -d < "$2"; fi\n'
        f'exec {real_base64} "$@"\n',
        encoding="utf-8",
    )
    for shim in directory.iterdir():
        shim.chmod(0o755)


class _Sandbox:
    """One workspace, under the git environment one image gives it.

    `excludes` is that image's `core.excludesFile`: the real exclude file by default, `None` for
    an image whose workspaces saw no ignore rules, or a path that does not exist."""

    def __init__(self, root: Path, ws: Path, excludes: Path | None = _EXCLUDE_FILE) -> None:
        self.root = root
        self.ws = ws
        ws.mkdir(parents=True, exist_ok=True)
        home = Path(tempfile.mkdtemp(prefix="home-", dir=root))
        rule = f"[core]\n\texcludesFile = {excludes}\n" if excludes is not None else ""
        (home / "gitconfig").write_text(_GIT_CONFIG + rule, encoding="utf-8")
        self.env = {
            "PATH": f"{root / 'shims'}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }

    def with_image(self, excludes: Path | None) -> _Sandbox:
        """This workspace, under another image."""
        return _Sandbox(self.root, self.ws, excludes)

    def fresh_container(self) -> _Sandbox:
        """A second workspace under this image, as a relaunch provisions it."""
        return _Sandbox(self.root, self.root / "fresh")

    def write(self, files: dict[str, str]) -> None:
        for relative, text in files.items():
            path = self.ws / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "-c", script],
            cwd=self.ws,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def ok(self, script: str) -> subprocess.CompletedProcess[str]:
        result = self.run(script)
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"
        return result

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.ws,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=check,
        )

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def commits(self) -> int:
        return int(self.git("rev-list", "--count", "HEAD").stdout)

    def tracked_at_head(self) -> set[str]:
        listing = self.git("ls-tree", "-r", "-z", "--name-only", "HEAD").stdout
        return {path for path in listing.split("\0") if path}

    def changed_by_head(self) -> set[str]:
        listing = self.git("diff-tree", "--no-commit-id", "-r", "-z", "--name-only", "HEAD").stdout
        return {path for path in listing.split("\0") if path}

    def bundle(self) -> bytes:
        """The save's bundle step: the argv `snapshot._bundle_the_tree` runs, read back."""
        path = self.root / "app.bundle"
        self.git("bundle", "create", str(path), "HEAD")
        return path.read_bytes()

    def restore(self, bundle: bytes) -> None:
        """What `SandboxClient._run_over_a_pushed_bundle` does: push the base64, run the script."""
        (self.ws / _BUNDLE_B64_NAME).write_text(
            base64.b64encode(bundle).decode("ascii"), encoding="ascii"
        )
        self.ok(_RESTORE_SCRIPT)


@pytest.fixture
def sandbox(tmp_path: Path) -> _Sandbox:
    _shims(tmp_path / "shims")
    return _Sandbox(tmp_path, tmp_path / "workspace")


def _a_workspace_tracking_toolchain_output(sandbox: _Sandbox) -> None:
    """A workspace as an image with no ignore rules left it: the baseline swept in
    `node_modules` and `next-env.d.ts`, and the first save after `next dev` ran added `.next`."""
    unruled = sandbox.with_image(None)
    unruled.write(_STARTER)
    unruled.ok(_INIT_REPO_SCRIPT)
    unruled.write({".next/dev/trace": "boot 1\n"})
    unruled.ok(_COMMIT_SCRIPT)
    assert _TOOLCHAIN_OUTPUT <= sandbox.tracked_at_head()


def test_one_save_untracks_toolchain_output_and_later_boots_mint_no_commit(
    sandbox: _Sandbox,
) -> None:
    """A boot rewrites `.next/dev` and `next-env.d.ts`. Tracked, they make every save of an
    unchanged app a new commit, and a new commit cancels the approval pinned to the old one."""
    _a_workspace_tracking_toolchain_output(sandbox)

    sandbox.ok(_COMMIT_SCRIPT)
    cleaned = sandbox.head()
    assert sandbox.tracked_at_head() == {"app/page.tsx", "package.json"}
    for path in _TOOLCHAIN_OUTPUT:
        assert (sandbox.ws / path).is_file(), f"untracking deleted {path} from the disk"

    for boot in (2, 3):
        sandbox.write(
            {".next/dev/trace": f"boot {boot}\n", "next-env.d.ts": _STARTER["next-env.d.ts"]}
        )
        sandbox.ok(_COMMIT_SCRIPT)
        assert sandbox.head() == cleaned, f"boot {boot} minted a commit with no code change"


def test_a_code_change_after_the_cleanup_is_one_commit_of_only_that_file(
    sandbox: _Sandbox,
) -> None:
    _a_workspace_tracking_toolchain_output(sandbox)
    sandbox.ok(_COMMIT_SCRIPT)
    before = sandbox.commits()

    sandbox.write({"app/page.tsx": "export const changed = true\n", ".next/dev/trace": "boot 2\n"})
    sandbox.ok(_COMMIT_SCRIPT)

    assert sandbox.commits() == before + 1
    assert sandbox.changed_by_head() == {"app/page.tsx"}


def test_next_build_rewriting_next_env_mints_no_commit(sandbox: _Sandbox) -> None:
    """`next build` writes `next-env.d.ts` in a different form from `next dev`."""
    _a_workspace_tracking_toolchain_output(sandbox)
    sandbox.ok(_COMMIT_SCRIPT)
    cleaned = sandbox.head()

    sandbox.write({"next-env.d.ts": '/// <reference path="./.next/types/routes.d.ts" />\n'})
    sandbox.ok(_COMMIT_SCRIPT)

    assert sandbox.head() == cleaned


def test_a_gitignore_the_agent_writes_never_untracks_code(sandbox: _Sandbox) -> None:
    """Only the image's list decides what leaves the index. Migrations are versioned app code, and
    an agent-written `.gitignore` that lists them must not drop them from the next save."""
    unruled = sandbox.with_image(None)
    unruled.write(
        {**_STARTER, "drizzle/0001.sql": "create table t ();\n", ".env.example": "DATABASE_URL=\n"}
    )
    unruled.ok(_INIT_REPO_SCRIPT)

    sandbox.write({".gitignore": "drizzle/\n.env*\n"})
    sandbox.ok(_COMMIT_SCRIPT)

    tracked = sandbox.tracked_at_head()
    assert "drizzle/0001.sql" in tracked
    assert ".env.example" in tracked


def test_odd_file_names_are_untracked_and_kept_on_disk(sandbox: _Sandbox) -> None:
    odd = {"node_modules/a b/c d.js", ".next/line\nbreak"}
    unruled = sandbox.with_image(None)
    unruled.write({**_STARTER, **dict.fromkeys(odd, "x\n")})
    unruled.ok(_INIT_REPO_SCRIPT)
    assert odd <= sandbox.tracked_at_head()

    sandbox.ok(_COMMIT_SCRIPT)

    assert not odd & sandbox.tracked_at_head()
    assert all((sandbox.ws / path).is_file() for path in odd)


def test_a_clean_workspace_commits_nothing(sandbox: _Sandbox) -> None:
    sandbox.write(_STARTER)
    sandbox.ok(_INIT_REPO_SCRIPT)
    baseline = sandbox.head()

    sandbox.ok(_COMMIT_SCRIPT)

    assert sandbox.head() == baseline


def test_a_stuck_index_is_untracked_and_the_save_succeeds(sandbox: _Sandbox) -> None:
    """An ignored file whose index entry matches neither HEAD nor the disk: a commit killed after
    `git add -A`, then a dev-server rewrite. `git rm --cached` refuses this file, and would fail
    every save after it."""
    _a_workspace_tracking_toolchain_output(sandbox)
    sandbox.write({".next/dev/trace": "staged\n"})
    sandbox.with_image(None).git("add", ".next/dev/trace")
    sandbox.write({".next/dev/trace": "rewritten\n"})
    refused = sandbox.git("rm", "--cached", "-q", ".next/dev/trace", check=False)
    assert refused.returncode != 0, "premise: this is the index `git rm --cached` refuses"

    sandbox.ok(_COMMIT_SCRIPT)

    assert ".next/dev/trace" not in sandbox.tracked_at_head()
    assert (sandbox.ws / ".next/dev/trace").read_text(encoding="utf-8") == "rewritten\n"


@pytest.mark.parametrize("excludes", ["unset", "missing"])
def test_without_the_exclude_file_a_save_untracks_nothing(
    sandbox: _Sandbox, excludes: str
) -> None:
    _a_workspace_tracking_toolchain_output(sandbox)
    image = sandbox.with_image(None if excludes == "unset" else sandbox.root / "no-such-file")
    before = image.commits()

    image.write({".next/dev/trace": "boot 2\n"})
    image.ok(_COMMIT_SCRIPT)

    assert image.commits() == before + 1
    assert image.changed_by_head() == {".next/dev/trace"}


def test_a_workspace_with_no_repository_is_refused(sandbox: _Sandbox) -> None:
    sandbox.write(_STARTER)

    result = sandbox.run(_COMMIT_SCRIPT)

    assert result.returncode == _NO_REPOSITORY_EXIT
    assert not (sandbox.ws / ".git").exists(), "the save forged a repository"


def test_a_workspace_that_lost_its_repository_is_refused_not_re_rooted(sandbox: _Sandbox) -> None:
    """A root commit written here would hold the finished app, and the starter-page check would
    then compare the app against itself forever."""
    sandbox.write(_STARTER)
    sandbox.ok(_INIT_REPO_SCRIPT)
    sandbox.write({"app/page.tsx": "the built app\n"})
    sandbox.ok(_COMMIT_SCRIPT)
    shutil.rmtree(sandbox.ws / ".git")

    result = sandbox.run(_COMMIT_SCRIPT)

    assert result.returncode == _NO_REPOSITORY_EXIT
    assert not (sandbox.ws / ".git").exists(), "the save re-rooted a workspace holding the app"


def test_a_git_killed_under_the_save_is_not_read_as_a_lost_repository(sandbox: _Sandbox) -> None:
    """A caller destroys the container on the no-repository exit, so a git the kernel kills for
    memory must fail the save instead, leaving the unsaved tree to a later attempt.
    Mutation check: probe with `git rev-parse --git-dir || exit 64` again and this goes red."""
    sandbox.write(_STARTER)
    sandbox.ok(_INIT_REPO_SCRIPT)
    sandbox.write({"app/page.tsx": "unsaved work\n"})
    dies = sandbox.root / "shims" / "git"
    dies.write_text("#!/bin/sh\nkill -9 $$\n", encoding="utf-8")
    dies.chmod(0o755)

    result = sandbox.run(_COMMIT_SCRIPT)

    assert result.returncode not in (0, _NO_REPOSITORY_EXIT)


def test_a_new_workspace_tracks_only_app_code(sandbox: _Sandbox) -> None:
    sandbox.write(
        {
            "app/page.tsx": _STARTER["app/page.tsx"],
            "node_modules/x/index.js": "baked\n",
            ".next/dev/trace": "boot\n",
            ".env": "SECRET=1\n",
            ".env.local": "LOCAL=1\n",
        }
    )

    sandbox.ok(_INIT_REPO_SCRIPT)

    assert sandbox.tracked_at_head() == {"app/page.tsx"}


def test_secrets_and_transport_files_never_enter_a_save(sandbox: _Sandbox) -> None:
    sandbox.write(_STARTER)
    sandbox.ok(_INIT_REPO_SCRIPT)
    kept_out = {".env", ".env.local", ".env.production.local", _BUNDLE_B64_NAME}
    sandbox.write({**dict.fromkeys(kept_out, "SECRET=hunter2\n"), "app/lib.ts": "ok\n"})

    sandbox.ok(_COMMIT_SCRIPT)

    tracked = sandbox.tracked_at_head()
    assert "app/lib.ts" in tracked
    assert not kept_out & tracked


def test_a_started_but_unbuilt_template_reads_as_the_untouched_starter(sandbox: _Sandbox) -> None:
    """The reaper and the integrity verdict read this as "nothing to lose", and they are right:
    the only changes are the dev server's."""
    sandbox.write(_STARTER)
    sandbox.ok(_INIT_REPO_SCRIPT)
    sandbox.write({".next/dev/trace": "boot\n", "next-env.d.ts": "/// rewritten on boot\n"})

    state = parse_state(sandbox.ok(state_script(None)).stdout)

    assert is_the_untouched_starter(state)
    assert clean_but_for_churn(state)


def test_a_saved_tree_restores_with_its_history(sandbox: _Sandbox) -> None:
    sandbox.write(_STARTER)
    sandbox.ok(_INIT_REPO_SCRIPT)
    sandbox.write({"feature.ts": "export const x = 1\n"})
    sandbox.ok(_COMMIT_SCRIPT)
    fresh = sandbox.fresh_container()
    fresh.write({**_STARTER, "feature.ts": "a stale baked copy\n"})

    fresh.restore(sandbox.bundle())

    assert (fresh.ws / "feature.ts").read_text(encoding="utf-8") == "export const x = 1\n"
    assert fresh.head() == sandbox.head()
    assert fresh.commits() == 2
    assert not (fresh.ws / _BUNDLE_B64_NAME).exists()


def test_a_restore_overlays_the_baked_tree(sandbox: _Sandbox) -> None:
    """The baked `node_modules` and any other untracked baked file survive a restore: the saved
    bundle carries neither, and `next dev` needs the dependencies."""
    sandbox.write(_STARTER)
    sandbox.ok(_INIT_REPO_SCRIPT)
    fresh = sandbox.fresh_container()
    fresh.write({**_STARTER, "node_modules/dep.js": "baked\n", "only-in-the-image.ts": "keep\n"})

    fresh.restore(sandbox.bundle())

    assert (fresh.ws / "node_modules/dep.js").read_text(encoding="utf-8") == "baked\n"
    assert (fresh.ws / "only-in-the-image.ts").is_file()
