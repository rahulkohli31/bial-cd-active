"""The model-free credential sweep (U6): the read-tools jail applied to the scan's own
walk, per-file truncation surfacing as an INCOMPLETE sweep, and prompt-ready hits that
structurally cannot carry a value."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from src.core.redaction import SCAN_INPUT_MAX_CHARS, Tier
from src.services.classification.scan import CredentialSweep, scan_snapshot

# A value-shaped Tier A finding (Stripe live key) and a name-shaped Tier B lead.
_TIER_A_LINE = 'const stripeKey = "sk_live_' + "a1b2c3d4e5" * 3 + '"\n'
_TIER_B_LINE = 'const password = "hunter2-fixture"\n'


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "extract"
    (root / "app").mkdir(parents=True)
    (root / "app" / "page.tsx").write_text("export default () => <div>ok</div>\n")
    return root


async def test_a_hit_carries_path_family_tier_and_line_never_the_value(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "app" / "db.ts").write_text("// setup\n" + _TIER_A_LINE)

    sweep = await scan_snapshot(root)

    assert len(sweep.hits) == 1
    located = sweep.hits[0]
    assert located.path == "app/db.ts"
    assert located.hit.family == "stripe-live-key"
    assert located.hit.tier is Tier.A
    assert located.hit.line == 2
    # Structural: neither the located wrapper nor the hit has anywhere to put a value.
    assert {field.name for field in dataclasses.fields(located)} == {"path", "hit"}
    assert {field.name for field in dataclasses.fields(located.hit)} == {"family", "tier", "line"}
    assert sweep.incomplete is False


async def test_a_clean_tree_scans_clean_and_complete(tmp_path: Path) -> None:
    sweep = await scan_snapshot(_tree(tmp_path))
    assert sweep == CredentialSweep(hits=(), incomplete=False)


async def test_a_real_lockfile_with_a_credential_line_produces_no_hit(tmp_path: Path) -> None:
    # The scan walks under the SAME exclusions as the read tools (U1's file-level set
    # included) — it does not go through the model's tools, so without this the
    # lockfile would sit in its path. A REAL lockfile is one beside the manifest it locks,
    # at the root or in a monorepo package alike.
    root = _tree(tmp_path)
    (root / "package.json").write_text('{"name": "app"}\n')
    (root / "package-lock.json").write_text(_TIER_A_LINE)
    (root / "packages" / "ui").mkdir(parents=True)
    (root / "packages" / "ui" / "package.json").write_text('{"name": "ui"}\n')
    (root / "packages" / "ui" / "pnpm-lock.yaml").write_text(_TIER_B_LINE)

    sweep = await scan_snapshot(root)

    assert sweep.hits == ()
    assert sweep.incomplete is False


async def test_a_credential_hidden_in_a_file_merely_named_like_a_lockfile_is_found(
    tmp_path: Path,
) -> None:
    """THE HOLE THE ANCHORING CLOSES. Matching the bare name at any depth meant a citizen
    could park a live credential in `app/config/yarn.lock` and have it skipped by the
    deterministic sweep AND refused to the model — while still shipping in the published
    image, because the deploy packaging step does not exclude lockfiles.

    Nothing there is a lockfile: no manifest sits beside it. It is ordinary source and the
    sweep reads it."""
    root = _tree(tmp_path)
    (root / "app" / "config").mkdir(parents=True)
    (root / "app" / "config" / "yarn.lock").write_text(_TIER_A_LINE)

    sweep = await scan_snapshot(root)

    assert len(sweep.hits) == 1
    assert sweep.hits[0].path == "app/config/yarn.lock"


async def test_ignored_directories_are_never_descended_into(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "node_modules" / "evil").mkdir(parents=True)
    (root / "node_modules" / "evil" / "index.js").write_text(_TIER_A_LINE)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text(_TIER_B_LINE)

    sweep = await scan_snapshot(root)

    assert sweep.hits == ()


async def test_symlinks_are_never_listed_or_followed(tmp_path: Path) -> None:
    # A link planted in the untrusted bundle must not lead the scan out of the
    # extraction — same jail as the read tools' walk.
    outside = tmp_path / "outside.ts"
    outside.write_text(_TIER_A_LINE)
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "secrets.ts").write_text(_TIER_A_LINE)

    root = _tree(tmp_path)
    (root / "app" / "link.ts").symlink_to(outside)
    (root / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

    sweep = await scan_snapshot(root)

    assert sweep.hits == ()


async def test_a_credential_line_past_the_per_file_ceiling_is_incomplete_not_clean(
    tmp_path: Path,
) -> None:
    # The one un-appealable answer must never silently degrade: a truncated file means
    # the sweep saw a prefix, and that is INCOMPLETE, never a clean no-hit.
    root = _tree(tmp_path)
    padding = "// filler\n" * (SCAN_INPUT_MAX_CHARS // 10 + 1)
    (root / "app" / "big.ts").write_text(padding + _TIER_A_LINE)

    sweep = await scan_snapshot(root)

    assert sweep.hits == ()
    assert sweep.incomplete is True


async def test_one_truncated_file_marks_the_whole_sweep_incomplete(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "app" / "huge.ts").write_text("x" * (SCAN_INPUT_MAX_CHARS + 10))
    (root / "app" / "db.ts").write_text(_TIER_A_LINE)

    sweep = await scan_snapshot(root)

    # The hit in the small file still surfaces; the sweep is still incomplete.
    assert [located.path for located in sweep.hits] == ["app/db.ts"]
    assert sweep.incomplete is True


async def test_hits_arrive_in_deterministic_path_order(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "zz.ts").write_text(_TIER_B_LINE)
    (root / "app" / "aa.ts").write_text(_TIER_B_LINE)

    sweep = await scan_snapshot(root)

    assert [located.path for located in sweep.hits] == ["app/aa.ts", "zz.ts"]
    assert all(located.hit.tier is Tier.B for located in sweep.hits)


async def test_a_binary_file_scans_as_noise_not_a_crash(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "app" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\xff" * 64)

    sweep = await scan_snapshot(root)

    assert sweep.hits == ()
    assert sweep.incomplete is False
