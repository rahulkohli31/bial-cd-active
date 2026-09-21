"""Generate the published API reference, or prove the committed one still matches this tree.

    uv run python scripts/openapi.py --write    # regenerate the committed file
    uv run python scripts/openapi.py --check    # compare, and fail naming what moved

WHY THE FILE IS COMMITTED. Production serves no schema endpoint — `create_app()` sets
`openapi_url=None` there, because an unauthenticated caller would otherwise be handed the whole
surface. So a reader holding this repository has nothing to ask, and the reference has to travel
with the code. That only works while the committed copy is true, which is what `--check` is for.

WHY ONE SCRIPT AND NOT TWO. A checker that re-implements "what the spec should look like" is a
second opinion that drifts from the first, and a drift checker that drifts is worse than none.
`--write` and `--check` share one serialiser, so they cannot disagree about formatting.

WHY IT RUNS WITHOUT A DATABASE. The schema is built from route signatures and Pydantic models,
none of which open a connection. The generator constructs the application and asks it for the
schema object; nothing in that path dials Postgres or Redis. That is what lets the check sit in
the static gate list beside the linters rather than behind a provisioned database, and it is
asserted by the suite rather than assumed.

DETERMINISM IS A REQUIREMENT, NOT A NICETY. Keys are sorted and the trailing newline is fixed, so
set iteration order cannot reach the file and produce a fresh diff on every run. The write goes
through `newline=""` so the LF survives on the Windows build host, where a checkout with line-
ending translation would otherwise rewrite every line and fail this check there while passing
here — the worst shape a guard can have. The `.gitattributes` entry covering the published
reference is the other half of that.

THE CHECK NEVER REPAIRS. A self-healing drift check hides the drift it exists to surface, so
`--check` only ever reports and exits non-zero. Repair is `--write`, run deliberately.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Executed by path (`python scripts/openapi.py`) sys.path[0] is `scripts/`, not the backend root,
# so `src` would not resolve; `-m` mode and the test import need nothing.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from collections.abc import Mapping  # noqa: E402
from typing import Any, Final  # noqa: E402

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
TARGET: Final = REPO_ROOT / "documentation" / "reference" / "openapi.json"
REGENERATE_COMMAND: Final = "uv run python scripts/openapi.py --write"
SAMPLE_ENVIRONMENT: Final = ".env.example"

_HTTP_METHODS: Final = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

# How many drifted operations to name before summarising the rest. A message that prints ninety
# lines is one nobody reads to the end of.
_NAMES_SHOWN: Final = 20


def build_spec() -> dict[str, Any]:
    """The schema object the configured application would serve.

    Imported inside the function rather than at module scope so that `--help`, and the pure
    helpers this module exposes to its tests, do not pay for constructing the application.

    Constructing it emits startup logs. This check runs in a gate sequence beside the linters,
    where two lines of unrelated chatter on every successful run is what makes people stop
    reading the output — so the noise is captured and dropped when it succeeded, and replayed in
    full when it did not. Logging is configured during the import below, which is why the
    redirect has to be in place before it.
    """
    # Pinned, not defaulted. The committed document has to be reproducible by anyone who runs
    # this, so it is generated from the sample environment whatever the developer happens to have
    # configured locally — otherwise two people regenerate two different files and the byte
    # comparison starts reporting each other's configuration as drift. The sample environment is
    # also the only one a fresh clone has.
    os.environ["ENV_FILE"] = SAMPLE_ENVIRONMENT

    noise = io.StringIO()
    try:
        with contextlib.redirect_stdout(noise), contextlib.redirect_stderr(noise):
            from src.main import create_app

            return create_app().openapi()
    except BaseException:
        sys.stderr.write(noise.getvalue())
        raise


def serialise(spec: Mapping[str, Any]) -> str:
    """The one rendering of the document that both modes use."""
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def operations(spec: Mapping[str, Any]) -> frozenset[str]:
    """Every operation as `METHOD /path`.

    Path items also carry non-operation keys such as `parameters` and `summary`, so the method
    names are matched against a closed set rather than assumed.
    """
    found: set[str] = set()
    paths = spec.get("paths") or {}
    for path, item in paths.items():
        if not isinstance(item, Mapping):
            continue
        for method, operation in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(operation, Mapping):
                found.add(f"{method.upper()} {path}")
    return frozenset(found)


def _operation_bodies(spec: Mapping[str, Any]) -> dict[str, Any]:
    bodies: dict[str, Any] = {}
    paths = spec.get("paths") or {}
    for path, item in paths.items():
        if not isinstance(item, Mapping):
            continue
        for method, operation in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(operation, Mapping):
                bodies[f"{method.upper()} {path}"] = operation
    return bodies


def _listed(label: str, names: list[str]) -> list[str]:
    if not names:
        return []
    shown = sorted(names)[:_NAMES_SHOWN]
    lines = [f"  {label} ({len(names)}):"]
    lines.extend(f"    {name}" for name in shown)
    if len(names) > len(shown):
        lines.append(f"    ... and {len(names) - len(shown)} more")
    return lines


def describe_drift(committed: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    """What moved between the committed document and this tree, named so it can be acted on.

    Returns an empty string when the two agree. A message that said only "files differ" would
    teach nothing and train people to regenerate blindly, which is the opposite of noticing, so
    this names the operations and ends with the command that repairs them.
    """
    committed_ops = operations(committed)
    current_ops = operations(current)

    added = sorted(current_ops - committed_ops)
    removed = sorted(committed_ops - current_ops)

    committed_bodies = _operation_bodies(committed)
    current_bodies = _operation_bodies(current)
    changed = sorted(
        name
        for name in committed_ops & current_ops
        if committed_bodies[name] != current_bodies[name]
    )

    elsewhere = sorted(
        key
        for key in set(committed) | set(current)
        if key != "paths" and committed.get(key) != current.get(key)
    )

    if not (added or removed or changed or elsewhere):
        return ""

    lines = ["The committed API reference no longer matches this tree."]
    lines += _listed("operations added", added)
    lines += _listed("operations removed", removed)
    lines += _listed("operations changed", changed)
    if elsewhere:
        # Model docstrings publish as schema descriptions, so a comment-only edit lands here
        # rather than under any one operation.
        lines.append(f"  changed outside the paths: {', '.join(elsewhere)}")
    lines.append("")
    lines.append(f"Regenerate with:  {REGENERATE_COMMAND}")
    return "\n".join(lines)


def _write(text: str) -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # `newline=""` so the LF stays an LF on the Windows build host as well.
    with TARGET.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _read() -> str:
    # `newline=""` so a CRLF checkout is visible here rather than being decoded away — hiding it
    # is exactly how this guard would pass locally and fail on the build host.
    return TARGET.read_text(encoding="utf-8", newline="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or check the published API reference.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate the committed reference")
    mode.add_argument("--check", action="store_true", help="fail if the committed reference moved")
    args = parser.parse_args(argv)

    current = build_spec()
    rendered = serialise(current)

    if args.write:
        _write(rendered)
        operation_count = len(operations(current))
        print(
            f"wrote {TARGET.relative_to(REPO_ROOT)} "
            f"({len(rendered):,} bytes, {operation_count:,} operations)",
            file=sys.stderr,
        )
        return 0

    if not TARGET.exists():
        print(
            f"{TARGET.relative_to(REPO_ROOT)} does not exist.\n\n"
            f"Generate it with:  {REGENERATE_COMMAND}",
            file=sys.stderr,
        )
        return 1

    committed_text = _read()
    if committed_text == rendered:
        return 0

    message = describe_drift(json.loads(committed_text), current)
    if not message:
        # Same document, different bytes: someone reformatted the file by hand.
        message = (
            "The committed API reference describes the same surface but is not formatted the "
            f"way the generator writes it.\n\nRegenerate with:  {REGENERATE_COMMAND}"
        )
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
