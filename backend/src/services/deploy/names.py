"""Azure resource names for a PUBLISHED app.

One rule matters more than the rest: a published container app must be impossible for the
sandbox reaper to select. That guarantee is **structural, not a naming convention** — the
reaper (`build_sessions/reaper.py::sweep_all`) does not enumerate Azure at all. It scans the
Redis sandbox registry and tears down whatever `app_name` it finds in the hash, and the only
writer of that field is `AcaSandboxClient._write_registry`. The deploy path never calls it
and never writes a `bial:sandbox:*` key, so a published app is invisible to the reaper by
construction.

The distinct `pub-` prefix is the SECOND, independent belt. It means an ARM-side listing can
partition the resource group unambiguously, and it means `live_build.py`'s
`live_app_name == app_name_for(app_id)` comparison can never accidentally match a published
app — a check that would otherwise treat a live deployment as the user's build sandbox.

Deliberately the same 28-hex slug length as `app_name_for` (`build_sessions/manager.py`), so
the two names stay derivable from each other. Do not "improve" the truncation: any uniqueness
reasoning belongs on `app_id`, never on the name.
"""

from __future__ import annotations

import uuid
from typing import Final

# ACA container-app names: 2–32 chars, lowercase alphanumeric and hyphens, must start with a
# letter and end alphanumeric. `pub-` (4) + 28 hex = exactly 32 — at the ceiling, like the
# sandbox's `sbx-` sibling. `str(app_id)` would be invalid (dots, and 36 chars).
_PUBLISHED_PREFIX: Final = "pub-"
_SLUG_HEX_CHARS: Final = 28

# The revision suffix ACA appends as `{app_name}--{suffix}`. Derived from the DEPLOYMENT id,
# not the app id, so every deploy mints a genuinely new revision — without it, redeploying an
# unchanged tree would produce an identical template and ACA would create no revision at all,
# leaving the pipeline polling for something that never appears.
_REVISION_PREFIX: Final = "d"
_REVISION_HEX_CHARS: Final = 10


def published_app_name(app_id: uuid.UUID) -> str:
    """The ACA container-app name for this app's published deployment.

    Stable for the life of the app, which is what makes the URL stable across every
    redeploy — the hostname is `{name}.{envDefaultDomain}`, so deriving the name from the
    immutable `app_id` (never from a project name a user can rename) is what stops a rename
    from stealing another app's address."""
    return f"{_PUBLISHED_PREFIX}{app_id.hex[:_SLUG_HEX_CHARS]}"


def revision_suffix(deployment_id: uuid.UUID) -> str:
    """The per-deploy revision suffix. ACA composes `{app_name}--{suffix}`, so knowing it
    up front is what lets the pipeline poll a revision by name instead of guessing which
    one its own `create_or_update` produced."""
    return f"{_REVISION_PREFIX}{deployment_id.hex[:_REVISION_HEX_CHARS]}"


def revision_name(app_id: uuid.UUID, deployment_id: uuid.UUID) -> str:
    """The full ACA revision name this deploy will create."""
    return f"{published_app_name(app_id)}--{revision_suffix(deployment_id)}"


def image_reference(
    *, acr_server: str, repository_prefix: str, app_id: uuid.UUID, digest: str
) -> str:
    """The DIGEST-PINNED image reference for the container spec.

    Digest-pinning is load-bearing, not hygiene: ACA resolves a TAG once, at revision
    creation, and will not notice a later push to the same tag. A tag-referenced app looks
    deployed while silently serving whatever the tag meant at revision time."""
    return f"{acr_server}/{repository_prefix}/{app_id}@{digest}"


def image_tag(*, repository_prefix: str, app_id: uuid.UUID, deployment_id: uuid.UUID) -> str:
    """The repository:tag the build pushes to. The tag carries the DEPLOYMENT id so an
    operator can attribute any image in the registry back to a row in `deployments`; the
    container spec then references the resulting digest, never this tag."""
    return f"{repository_prefix}/{app_id}:{deployment_id.hex[:12]}"
