"""Azure names for a published app — and the regression test that keeps the sandbox reaper
away from live deployments.

The reaper cannot select a published app for a structural reason (it reads the Redis sandbox
registry, which the deploy path never writes). The naming is the SECOND, independent belt,
and it is the one a future refactor is most likely to erode by accident — so it gets a
property test over a thousand ids rather than one hand-picked example.

If `published_app_name` ever collided with `app_name_for`, a deploy would create a container
app under the sandbox's name; the reaper would then tear down a citizen's live application on
its next 300-second sweep, and the sandbox restore path would tear it down again. That is why
this file exists.
"""

from __future__ import annotations

import re
import uuid

from src.services.build_sessions import app_name_for
from src.services.deploy.names import (
    image_reference,
    image_tag,
    published_app_name,
    revision_name,
    revision_suffix,
)

# ACA container-app names: 2–32 chars, lowercase alphanumeric with internal hyphens, must
# start with a letter and end alphanumeric.
_ACA_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_ACA_NAME_MAX = 32


def test_published_names_are_aca_legal_and_never_collide_with_a_sandbox() -> None:
    for _ in range(1000):
        app_id = uuid.uuid4()
        published = published_app_name(app_id)

        assert published != app_name_for(app_id)
        assert not published.startswith("sbx-")
        assert published.startswith("pub-")
        assert len(published) <= _ACA_NAME_MAX
        assert _ACA_NAME_RE.fullmatch(published), published


def test_the_published_name_is_exactly_at_the_aca_ceiling() -> None:
    """`pub-` + 28 hex = 32. Pinned because the next person to widen the slug would produce
    names Azure rejects with a terminal 400 — no retry, straight to a failed deploy."""
    assert len(published_app_name(uuid.uuid4())) == _ACA_NAME_MAX


def test_the_name_is_stable_for_an_app_forever() -> None:
    """This is what makes the published URL stable across every redeploy. Derived from the
    immutable app_id, never from anything a user can rename."""
    app_id = uuid.uuid4()
    assert published_app_name(app_id) == published_app_name(app_id)


def test_two_apps_never_share_a_name() -> None:
    names = {published_app_name(uuid.uuid4()) for _ in range(2000)}
    assert len(names) == 2000


# --- revisions -------------------------------------------------------------------


def test_every_deploy_gets_a_distinct_revision() -> None:
    """Without a per-deploy suffix, redeploying an unchanged tree produces an identical
    template, ACA creates no revision at all, and the pipeline polls forever for something
    that never appears."""
    app_id = uuid.uuid4()
    first = revision_name(app_id, uuid.uuid4())
    second = revision_name(app_id, uuid.uuid4())
    assert first != second
    assert first.startswith(f"{published_app_name(app_id)}--")


def test_the_revision_suffix_starts_with_a_letter() -> None:
    """ACA revision suffixes follow the same alphanumeric-with-hyphens rule; a bare hex slug
    beginning with a digit is rejected, which is why the `d` prefix is there."""
    for _ in range(200):
        suffix = revision_suffix(uuid.uuid4())
        assert _ACA_NAME_RE.fullmatch(suffix), suffix


def test_the_revision_name_fits_the_column() -> None:
    from src.db.models.deployment import MAX_REVISION_NAME

    assert len(revision_name(uuid.uuid4(), uuid.uuid4())) <= MAX_REVISION_NAME


# --- image references -------------------------------------------------------------


def test_the_container_image_is_digest_pinned_never_tagged() -> None:
    """ACA resolves a TAG once, at revision creation, and never notices a later push. A
    tag-referenced app looks deployed while serving whatever the tag meant back then."""
    app_id = uuid.uuid4()
    digest = "sha256:" + "ab" * 32
    ref = image_reference(
        acr_server="bialgenaicr.azurecr.io",
        repository_prefix="citizen-apps",
        app_id=app_id,
        digest=digest,
    )
    assert ref == f"bialgenaicr.azurecr.io/citizen-apps/{app_id}@{digest}"
    assert "@sha256:" in ref
    # A colon tag separator after the repository would mean the reference is NOT pinned.
    assert not re.search(r"/citizen-apps/[^@]+:", ref)


def test_the_build_tag_carries_the_deployment_id_for_attribution() -> None:
    """So an operator can trace any image in the registry back to a row in `deployments`."""
    app_id, deployment_id = uuid.uuid4(), uuid.uuid4()
    tag = image_tag(repository_prefix="citizen-apps", app_id=app_id, deployment_id=deployment_id)
    assert tag == f"citizen-apps/{app_id}:{deployment_id.hex[:12]}"
