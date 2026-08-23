"""U1 — does this container still hold this app's work? (R1, R2.)

WHY THIS FILE IS SEPARATE FROM `test_integrity.py`. That one covers the two probes the health
verdict asks (baseline identity, the agent watermark), which answer "is this app finished". This
one covers the verdict that answers "is this app still HERE" — the only question in the system
whose answer can authorise replacing a live workspace. They share a module because they are both
facts the container holds about its own git repository; they share nothing else.

THE TWO TESTS THAT MATTER MOST, and they pull in opposite directions:

* `test_a_factory_reset_container_with_no_durable_copy_at_all_is_still_a_reversion` is the
  2026-08-18 P0. Every check the platform had came back green on that container because each one
  asked "is an app running here". This one asks whether the app's own repository is still there.
* `test_a_lineage_that_moved_over_a_tree_that_still_holds_content_is_never_a_reversion` is the
  guard against the cure being worse than the disease. `git reset --hard`, `--amend` and `rebase`
  all break the lineage over a perfectly good workspace, and the live Write prompt still teaches
  `git checkout` for undo. Restoring over that tree would be a NEW data-loss path, invented by
  the guard meant to close one.
"""

from __future__ import annotations

import uuid

import pytest

from src.services.build_sessions import integrity
from src.services.build_sessions.integrity import (
    Ancestry,
    ContainerState,
    IntegrityVerdict,
    WorkspaceState,
    container_state,
    parse_state,
    reset_integrity_streaks_for_tests,
    state_script,
    workspace_integrity,
)
from src.services.sandbox import SandboxError
from src.services.sandbox.base import ExecResult, SandboxHandle
from src.services.storage import recovery_key, snapshot_key
from src.services.storage.errors import StorageError, StorageUnconfiguredError
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

APP = uuid.UUID("0198f2c0-1111-7000-8000-00000000ca11")
REFERENCE = "a" * 40
DESCENDANT = "b" * 40

_HANDLE = SandboxHandle(
    fqdn="app-x.westeurope.azurecontainerapps.io",
    token="t",
    app_name="app-x",
    preview_url="https://app-x.westeurope.azurecontainerapps.io/",
    ready=True,
)


@pytest.fixture(autouse=True)
def _no_leaked_streaks() -> None:
    """The unreadable streak is process-local, so a partial streak from one test would change
    the verdict the next one gets."""
    reset_integrity_streaks_for_tests()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr(integrity, "get_storage", lambda: fake)
    return fake


def _stdout(
    head: str | None = DESCENDANT,
    porcelain: str = "",
    commits: int = 4,
    ancestry: str = "0 0",
) -> str:
    """The four `@@`-separated fields `state_script` produces."""
    return f"{head or ''}@@{porcelain}@@{commits}@@{ancestry}"


def _client(stdout: str) -> FakeSandboxClient:
    client = FakeSandboxClient()
    client.exec_handler = lambda cmd: ExecResult(stdout=stdout, stderr="", exit=0)
    return client


async def _seed_recovery(store: FakeStorage, sha: str | None = REFERENCE) -> None:
    await store.put(
        recovery_key(APP),
        a_git_bundle(sha or REFERENCE),
        metadata={"head_sha": sha} if sha else {},
    )


async def _seed_saved(store: FakeStorage, sha: str | None = REFERENCE) -> None:
    await store.put(
        snapshot_key(APP),
        a_git_bundle(sha or REFERENCE),
        metadata={"head_sha": sha} if sha else {},
    )


# =============================================================================
# parse_state — the pure half
# =============================================================================


def test_the_four_field_form_parses_every_field() -> None:
    state = parse_state(_stdout(head="abc123", porcelain="M  app/page.tsx", commits=7))

    assert state.head == "abc123"
    assert state.uncommitted is True
    assert state.changed_paths == ("app/page.tsx",)
    assert state.commits == 7
    assert state.ancestry is Ancestry.DESCENDANT


def test_a_truncated_three_field_body_still_parses_and_reads_as_unasked() -> None:
    """★ The shape a container answers with when nobody supplied a reference sha — and the shape
    every pre-U1 caller of this script produced. It must parse, and the ancestry must be
    `NOT_ASKED` rather than any judgement about the lineage."""
    state = parse_state("abc123@@@@3")

    assert state.head == "abc123"
    assert state.commits == 3
    assert state.ancestry is Ancestry.NOT_ASKED


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("", Ancestry.NOT_ASKED),
        ("0 0", Ancestry.DESCENDANT),
        ("0 1", Ancestry.NOT_DESCENDANT),
        ("1 128", Ancestry.REFERENCE_ABSENT),
        ("0 128", Ancestry.UNREADABLE),
        ("garbage", Ancestry.UNREADABLE),
        ("0", Ancestry.UNREADABLE),
        ("x y", Ancestry.UNREADABLE),
    ],
)
def test_the_ancestry_field_maps_exit_codes_to_answers(field: str, expected: Ancestry) -> None:
    """`git cat-file -e`'s exit first, then `git merge-base --is-ancestor`'s.

    `0 128` is the one worth staring at: the reference exists, so `--is-ancestor` had every
    input it needed, and a 128 means git itself failed. Reading that as "not a descendant" would
    turn a broken repository into a lineage judgement."""
    assert parse_state(f"abc@@@@2@@{field}").ancestry is expected


def test_the_script_asks_nothing_about_ancestry_when_no_reference_is_given() -> None:
    script = state_script(None)

    assert "merge-base" not in script
    assert "cat-file" not in script
    assert script.count('echo "@@"') == 3  # four fields, three separators


def test_the_script_asks_cat_file_before_merge_base() -> None:
    """Order matters to the reader, not just to the shell: `--is-ancestor` against an object the
    repository does not contain fails for a reason that has nothing to do with the lineage."""
    script = state_script(REFERENCE)

    assert script.index("cat-file") < script.index("merge-base")


# =============================================================================
# The four states
# =============================================================================


async def test_a_brand_new_project_is_intact_and_authorises_nothing(store: FakeStorage) -> None:
    """One commit (the seeded baseline), a clean tree, nothing saved, no recovery copy — the
    four conditions `_nothing_to_lose` already uses. A brand-new project is SUPPOSED to hold
    nothing, and calling that a reversion would quarantine every first message anyone sends."""
    client = _client(_stdout(head="seed", commits=1, ancestry=""))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=None)

    assert verdict.state is WorkspaceState.INTACT
    assert verdict.may_restore is False


async def test_framework_churn_alone_still_reads_as_a_brand_new_project(
    store: FakeStorage,
) -> None:
    """`next dev` rewrites `next-env.d.ts` and normalises `tsconfig.json` on every boot, so a
    container that has merely STARTED reports a dirty tree. Treating that as work is what let an
    empty template lock a user out of the project holding their real app."""
    client = _client(
        _stdout(
            head="seed", porcelain="M  next-env.d.ts\nM  tsconfig.json", commits=1, ancestry=""
        )
    )

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=None)

    assert verdict.state is WorkspaceState.INTACT


async def test_a_factory_reset_container_with_a_recovery_copy_is_a_reversion(
    store: FakeStorage,
) -> None:
    """★ AE2. No repository at all, on an app whose work was copied at a turn boundary."""
    await _seed_recovery(store)
    client = _client(_stdout(head=None, commits=0, ancestry=""))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.REVERTED
    assert verdict.may_restore is True
    assert verdict.content_empty is True
    assert verdict.durable_copy_exists is True
    assert verdict.reference_key == recovery_key(APP)


async def test_a_factory_reset_container_with_no_durable_copy_at_all_is_still_a_reversion(
    store: FakeStorage,
) -> None:
    """★ AE2(b)/AE3 — THE P0, and the arm that is easiest to get backwards.

    A container whose turn-end autosave silently failed (ASM30 says that is a live state) and
    which then factory-resets has NO durable copy. Reading that as "nothing to compare against,
    carry on" is exactly how the agent came to build on a wiped tree and stamp the empty tree in
    as the newest copy of somebody's finished app.

    Mutation check: move the `not facts.any_copy` arm above the `head is None` arm and this goes
    red while every other test here stays green."""
    client = _client(_stdout(head=None, commits=0, ancestry=""))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=None)

    assert verdict.state is WorkspaceState.REVERTED
    assert verdict.may_restore is True
    assert verdict.durable_copy_exists is False  # U2 takes the no-source arm on this


async def test_a_repository_with_work_and_no_durable_copy_is_intact(store: FakeStorage) -> None:
    """A repository that holds real history and has simply never been copied. Nothing to report,
    nothing to restore from — U3 writes this app's first recovery copy at the end of the turn."""
    client = _client(_stdout(head="abc", commits=6, ancestry=""))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=None)

    assert verdict.state is WorkspaceState.INTACT
    assert verdict.may_restore is False


async def test_a_head_built_on_top_of_the_reference_is_intact(store: FakeStorage) -> None:
    """The normal shape of every healthy turn: work has happened since the last copy."""
    await _seed_recovery(store)
    client = _client(_stdout(head=DESCENDANT, commits=9, ancestry="0 0"))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.INTACT


async def test_a_lineage_that_moved_over_a_template_clean_tree_is_a_reversion(
    store: FakeStorage,
) -> None:
    """The repository was re-seeded rather than deleted: one commit, a clean tree, and a HEAD
    the durable copy is not below."""
    await _seed_recovery(store)
    client = _client(_stdout(head="reseeded", commits=1, ancestry="0 1"))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.REVERTED
    assert verdict.content_empty is True


async def test_a_lineage_that_moved_over_a_tree_that_still_holds_content_is_never_a_reversion(
    store: FakeStorage,
) -> None:
    """★ THE GUARD AGAINST THE CURE BEING WORSE THAN THE DISEASE.

    `git reset --hard`, `--amend` and `rebase` all produce a HEAD the durable copy is not below,
    over a workspace that still holds every file the user cares about — and the live Write prompt
    still teaches `git checkout` / `git revert` for undo. Restoring over that tree would destroy
    the newest copy of their work in the name of recovering it.

    Mutation check: drop `content_empty` from the NOT_DESCENDANT arm and this goes red."""
    await _seed_recovery(store)
    client = _client(_stdout(head="rewound", commits=12, ancestry="0 1"))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNVERIFIABLE
    assert verdict.may_restore is False


async def test_a_dirty_tree_under_a_moved_lineage_is_never_a_reversion(
    store: FakeStorage,
) -> None:
    """One commit and a MOVED lineage, but files written since — uncommitted work is still work,
    and it is the half `_nothing_to_lose` exists to catch."""
    await _seed_recovery(store)
    client = _client(
        _stdout(head="reseeded", porcelain="A  app/invoices/page.tsx", commits=1, ancestry="0 1")
    )

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNVERIFIABLE


async def test_a_truncated_porcelain_is_evidence_of_work_not_of_emptiness(
    store: FakeStorage,
) -> None:
    """A listing long enough to hit the cap has already answered the only question the cap could
    interfere with: this tree is real work."""
    await _seed_recovery(store)
    client = _client(
        _stdout(
            head="reseeded",
            porcelain="M  " + "a" * integrity.PORCELAIN_CAP_BYTES,
            commits=1,
            ancestry="0 1",
        )
    )

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNVERIFIABLE


async def test_a_reference_the_repository_does_not_contain_is_unverifiable(
    store: FakeStorage,
) -> None:
    """`--is-ancestor` never ran, so the lineage question was not answered by git — it was
    answered by an object being missing, which has innocent explanations. The conservative arm
    still protects the user: no restore, and U3 refuses the recovery write, so the good bundle
    survives for an operator to promote."""
    await _seed_recovery(store)
    client = _client(_stdout(head="abc", commits=1, ancestry="1 128"))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNVERIFIABLE
    assert verdict.may_restore is False


async def test_a_durable_copy_with_no_head_sha_is_unverifiable_never_reverted(
    store: FakeStorage,
) -> None:
    """ "No claim" is what `head_sha_from_metadata` documents for an object written before the
    stamp existed — a documented live state that no retry heals. Accusing a workspace of
    reversion on the strength of a missing metadata key is the false positive that destroys
    work."""
    await _seed_recovery(store, sha=None)
    client = _client(_stdout(head="abc", commits=1, ancestry=""))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNVERIFIABLE
    assert verdict.may_restore is False


# =============================================================================
# The reference sha never reaches the shell unvalidated
# =============================================================================


async def test_a_malformed_head_sha_is_unverifiable_and_never_reaches_the_shell(
    store: FakeStorage,
) -> None:
    """★ A sha read back from blob metadata is a value the STORE returned, and the composed
    string goes to `sh -c`. Seven to forty lowercase hex characters, or it does not go."""
    hostile = "a" * 39 + "; rm -rf /"
    await _seed_recovery(store, sha=hostile)
    seen: list[list[str]] = []

    client = FakeSandboxClient()

    def record(cmd: list[str]) -> ExecResult:
        seen.append(cmd)
        return ExecResult(stdout=_stdout(head="abc", commits=4, ancestry=""), stderr="", exit=0)

    client.exec_handler = record

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNVERIFIABLE
    assert seen, "the probe must still run, so the alarm payload carries the container's state"
    assert all("rm -rf" not in part for cmd in seen for part in cmd)
    assert all("merge-base" not in part for cmd in seen for part in cmd)


@pytest.mark.parametrize("bad", ["ABCDEF1", "abc", "z" * 40, "a" * 41, "a" * 20 + "-"])
async def test_every_non_sha_shape_is_refused(store: FakeStorage, bad: str) -> None:
    await _seed_recovery(store, sha=bad)
    client = _client(_stdout(head="abc", commits=4, ancestry=""))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNVERIFIABLE


# =============================================================================
# The unanswerable states, and the cap that stops one becoming a lockout
# =============================================================================


async def test_an_exec_that_raises_is_unreadable(store: FakeStorage) -> None:
    await _seed_recovery(store)
    client = FakeSandboxClient()

    def boom(cmd: list[str]) -> ExecResult:
        raise SandboxError("the supervisor did not answer")

    client.exec_handler = boom

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNREADABLE
    assert verdict.may_restore is False


async def test_a_non_zero_exit_is_unreadable(store: FakeStorage) -> None:
    await _seed_recovery(store)
    client = FakeSandboxClient()
    client.exec_handler = lambda cmd: ExecResult(stdout="", stderr="sh: not found", exit=127)

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNREADABLE


async def test_an_unparseable_body_is_unreadable_not_a_reversion(store: FakeStorage) -> None:
    """It must not crash, and it must not read as loss. The reference sha WAS supplied, so an
    ancestry field that is not there means the container answered in a shape we do not
    understand — which is a retry, not a judgement."""
    await _seed_recovery(store)
    client = _client("this is not the output of anything")

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert verdict.state is WorkspaceState.UNREADABLE
    assert verdict.may_restore is False


async def test_a_third_consecutive_unreadable_answer_stops_locking_the_user_out(
    store: FakeStorage,
) -> None:
    """★ `UNREADABLE` fails the turn as retryable, which is right once and wrong forever. A
    container whose exec endpoint has genuinely stopped answering would otherwise refuse the user
    their own project on every message, with a retry prompt that can never succeed.

    Mutation check: raise `_UNREADABLE_STREAK_CAP` and the third assertion goes red."""
    await _seed_recovery(store)
    client = FakeSandboxClient()
    client.exec_handler = lambda cmd: ExecResult(stdout="", stderr="", exit=127)

    first, second, third = [
        await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))
        for _ in range(3)
    ]

    assert first.state is WorkspaceState.UNREADABLE
    assert second.state is WorkspaceState.UNREADABLE
    assert third.state is WorkspaceState.UNVERIFIABLE
    assert third.may_restore is False


async def test_one_good_answer_clears_the_streak(store: FakeStorage) -> None:
    """Two bad answers then a good one must not leave the app one blip away from structural."""
    await _seed_recovery(store)
    client = FakeSandboxClient()
    client.exec_handler = lambda cmd: ExecResult(stdout="", stderr="", exit=127)
    for _ in range(2):
        await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    client.exec_handler = lambda cmd: ExecResult(
        stdout=_stdout(head=DESCENDANT, commits=9, ancestry="0 0"), stderr="", exit=0
    )
    good = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))
    client.exec_handler = lambda cmd: ExecResult(stdout="", stderr="", exit=127)
    after = await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))

    assert good.state is WorkspaceState.INTACT
    assert after.state is WorkspaceState.UNREADABLE  # a fresh streak, not the spent one


async def test_the_streak_is_counted_per_app(store: FakeStorage) -> None:
    """One app's bad luck must not spend another app's patience."""
    other = uuid.uuid4()
    await _seed_recovery(store)
    await store.put(recovery_key(other), a_git_bundle(REFERENCE), metadata={"head_sha": REFERENCE})
    client = FakeSandboxClient()
    client.exec_handler = lambda cmd: ExecResult(stdout="", stderr="", exit=127)

    for _ in range(3):
        await workspace_integrity(client, _HANDLE, APP, restore_source_key=recovery_key(APP))
    theirs = await workspace_integrity(
        client, _HANDLE, other, restore_source_key=recovery_key(other)
    )

    assert theirs.state is WorkspaceState.UNREADABLE


# =============================================================================
# What the object store's own failures mean
# =============================================================================


async def test_a_storage_off_deployment_proceeds_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ KTD-2, and the guard that keeps local development working. With no store there can be
    no durable copy for anyone, so there is nothing to compare against and nothing to restore
    from — a fact about the DEPLOYMENT, not about anybody's work.

    It fails silently in the worst direction without this test: an `UNREADABLE` here would make
    every Write turn on a storage-off deployment fail as retryable, forever."""

    def unconfigured() -> FakeStorage:
        raise StorageUnconfiguredError("no store")

    monkeypatch.setattr(integrity, "get_storage", unconfigured)
    client = _client(_stdout(head=None, commits=0, ancestry=""))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=None)

    assert verdict.state is WorkspaceState.INTACT
    assert verdict.may_restore is False


async def test_an_unreadable_store_is_unreadable_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, store: FakeStorage
) -> None:
    async def blows_up(key: str) -> None:
        raise StorageError("the store did not answer")

    monkeypatch.setattr(store, "head", blows_up)
    client = _client(_stdout(head=None, commits=0, ancestry=""))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=None)

    assert verdict.state is WorkspaceState.UNREADABLE
    assert verdict.may_restore is False


async def test_a_saved_bundle_alone_satisfies_the_durable_copy_union(store: FakeStorage) -> None:
    """A user who clicked Save but lost their recovery copy must not be told their app is
    unrecoverable while the saved bundle sits in Blob. `_restore_or_provision` already reads the
    union; the verdict has to read the same one."""
    await _seed_saved(store)
    client = _client(_stdout(head=DESCENDANT, commits=9, ancestry="0 0"))

    verdict = await workspace_integrity(client, _HANDLE, APP, restore_source_key=None)

    assert verdict.state is WorkspaceState.INTACT
    assert verdict.durable_copy_exists is True
    assert verdict.reference_key == snapshot_key(APP)


async def test_the_reference_is_the_bundle_the_caller_would_actually_restore(
    store: FakeStorage,
) -> None:
    """The saved bundle and the recovery bundle can hold different trees, and then the answer
    depends on which one the caller would hand back. Same container, two references, two
    verdicts — which is why this is the caller's choice rather than a rule buried in the probe."""
    await _seed_saved(store, sha=REFERENCE)
    await _seed_recovery(store, sha=DESCENDANT)
    # The container is a descendant of the SAVED tree and not of the recovery one.
    client = FakeSandboxClient()
    client.exec_handler = lambda cmd: ExecResult(
        stdout=_stdout(head="c" * 40, commits=9, ancestry="0 0" if REFERENCE in cmd[2] else "0 1"),
        stderr="",
        exit=0,
    )

    against_saved = await workspace_integrity(client, _HANDLE, APP, restore_source_key=None)
    against_recovery = await workspace_integrity(
        client, _HANDLE, APP, restore_source_key=recovery_key(APP)
    )

    assert against_saved.state is WorkspaceState.INTACT
    assert against_recovery.state is WorkspaceState.UNVERIFIABLE


# =============================================================================
# The property the whole unit rests on
# =============================================================================


def test_exactly_one_state_may_restore() -> None:
    """★ Asserted over the WHOLE enum rather than at the four call sites, for the reason
    `CopyVerdict.may_destroy` documents: `state is REVERTED` spelled out four times is four
    chances to write `is not INTACT` and quietly authorise the two states that mean "we could not
    tell"."""
    permitted = [state for state in WorkspaceState if IntegrityVerdict(state, "x").may_restore]

    assert permitted == [WorkspaceState.REVERTED]


async def test_the_probe_never_raises_on_a_container_that_cannot_answer() -> None:
    """`container_state` is the layer below the verdict, and it has the same contract."""
    client = FakeSandboxClient()

    def boom(cmd: list[str]) -> ExecResult:
        raise SandboxError("gone")

    client.exec_handler = boom

    assert await container_state(client, _HANDLE, reference_sha=REFERENCE) is None


def test_a_container_state_defaults_to_not_asked() -> None:
    """Every pre-U1 construction site (the reaper, the save indicator) supplies no reference, and
    must keep meaning "nobody asked" rather than any judgement."""
    assert (
        ContainerState(
            head="abc", uncommitted=False, changed_paths=(), porcelain_truncated=False, commits=2
        ).ancestry
        is Ancestry.NOT_ASKED
    )
