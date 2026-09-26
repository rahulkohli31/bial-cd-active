"""The administrator's publish classification configuration: the policy, and the classes the
reviewer answers.

Every write commits together with its `classification:config` audit row, which carries the
before, the after and the actor. There is no delete: stored decisions refer to a class's key, so a
class is switched off, never removed. The writes declare `RequireCsrf`, whose header the portal
sends on every mutating call.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Annotated, Any, Self

import sqlalchemy as sa
from fastapi import APIRouter, status
from pydantic import AfterValidator, Field, model_validator
from sqlalchemy.exc import IntegrityError

from src.api.deps import DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.deps_rbac import CurrentSuperadmin
from src.core.errors import AppApiError
from src.db.models.classification_config import (
    MAX_CLASS_DESCRIPTION,
    MAX_CLASS_KEY,
    MAX_CLASS_TITLE,
    MAX_THRESHOLD,
    MAX_WEIGHT,
    ClassificationClass,
    ClassificationKind,
    ClassificationPolicy,
)
from src.db.models.user import User
from src.schemas import (
    ADMIN_AUTH,
    AUTH_401,
    CamelModel,
    DetailBody,
    ErrorEnvelope,
    error_responses,
)
from src.services.audit.log import append_audit

router = APIRouter(prefix="/admin/classification", tags=["admin"])

_AUDIT_ACTION = "classification:config"
_TITLE_INDEX = "uq_classification_classes_title"
# Room left after the stem for `_` and a numeric suffix.
_KEY_STEM = MAX_CLASS_KEY - 8

_WRITE_AUTH = (
    AUTH_401,
    (403, DetailBody, "Super-admin privileges required, or the CSRF check failed (`csrf_failed`)"),
)
_DUPLICATE_TITLE = (409, ErrorEnvelope, "Another class has this title (`duplicate_title`)")
_WEIGHT_REQUIRED = (
    422,
    ErrorEnvelope,
    "A scored class with no weight (`weight_required`), or a body that fails validation",
)


def _clean_title(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Give the class a title.")
    if len(value) > MAX_CLASS_TITLE:
        raise ValueError(f"Keep the title to {MAX_CLASS_TITLE} characters or fewer.")
    return value


def _clean_description(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Tell the agent when to answer Yes.")
    if len(value) > MAX_CLASS_DESCRIPTION:
        raise ValueError(f"Keep the description to {MAX_CLASS_DESCRIPTION:,} characters or fewer.")
    return value


Title = Annotated[str, AfterValidator(_clean_title)]
Description = Annotated[str, AfterValidator(_clean_description)]
Weight = Annotated[int, Field(strict=True, ge=0, le=MAX_WEIGHT)]
Threshold = Annotated[int, Field(strict=True, ge=0, le=MAX_THRESHOLD)]


class ClassificationPolicyOut(CamelModel):
    """The two settings every send is scored with."""

    threshold: int
    owners_can_change_answers: bool


class ClassificationClassOut(CamelModel):
    """One class, with who last changed it. `updatedByName` is null on a seeded class nobody has
    edited, and after that administrator's account is deleted."""

    key: str
    title: str
    description: str
    kind: ClassificationKind
    weight: int | None
    active: bool
    updated_at: datetime
    updated_by_name: str | None


class ClassificationConfigResponse(CamelModel):
    """The policy and every class, active or not: hard blocks first, then scored classes by
    weight, highest first."""

    policy: ClassificationPolicyOut
    classes: list[ClassificationClassOut]


class ClassificationPolicyUpdate(CamelModel):
    """Either setting, or both. A setting left out keeps its value."""

    threshold: Threshold | None = None
    owners_can_change_answers: bool | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if self.threshold is None and self.owners_can_change_answers is None:
            raise ValueError("Change the threshold, the owners' switch, or both.")
        return self


class ClassificationClassCreate(CamelModel):
    """A new class. A hard block ignores `weight`; a scored class needs one."""

    title: Title
    description: Description
    kind: ClassificationKind
    weight: Weight | None = None
    active: bool = True


class ClassificationClassUpdate(CamelModel):
    """Any of a class's fields but its key. A field left out keeps its value. Switching to hard
    block clears the weight; switching to scored needs one."""

    title: Title | None = None
    description: Description | None = None
    kind: ClassificationKind | None = None
    weight: Weight | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Change at least one field.")
        return self


def _weight_for(kind: ClassificationKind, weight: int | None) -> int | None:
    if kind is ClassificationKind.HARD_BLOCK:
        return None
    if weight is None:
        raise AppApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A scored class needs a weight from 0 to {MAX_WEIGHT}.",
            code="weight_required",
        )
    return weight


def _slug(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-z0-9]+", "_", ascii_title.lower())[:_KEY_STEM].strip("_")
    return stem or "class"


async def _free_key(db: DbSession, title: str) -> str:
    """The title's slug, or the slug with the lowest free suffix from 2 up."""
    stem = _slug(title)
    taken = set(
        await db.scalars(
            sa.select(ClassificationClass.key).where(
                ClassificationClass.key.startswith(stem, autoescape=True)
            )
        )
    )
    if stem not in taken:
        return stem
    suffix = 2
    while f"{stem}_{suffix}" in taken:
        suffix += 1
    return f"{stem}_{suffix}"


def _duplicate_title(title: str) -> AppApiError:
    return AppApiError(
        status.HTTP_409_CONFLICT,
        f"A class called “{title}” already exists.",
        code="duplicate_title",
    )


def _class_state(
    title: str, description: str, kind: ClassificationKind, weight: int | None, active: bool
) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "kind": kind.value,
        "weight": weight,
        "active": active,
    }


async def _configuration(db: DbSession) -> ClassificationConfigResponse:
    policy = (
        await db.execute(
            sa.select(
                ClassificationPolicy.threshold, ClassificationPolicy.owners_can_change_answers
            )
        )
    ).one()
    rows = await db.execute(
        sa.select(
            ClassificationClass.key,
            ClassificationClass.title,
            ClassificationClass.description,
            ClassificationClass.kind,
            ClassificationClass.weight,
            ClassificationClass.active,
            ClassificationClass.updated_at,
            User.display_name,
            User.email,
        )
        .outerjoin(User, User.id == ClassificationClass.updated_by)
        .order_by(
            ClassificationClass.kind != ClassificationKind.HARD_BLOCK,
            ClassificationClass.weight.desc(),
            ClassificationClass.id,
        )
    )
    return ClassificationConfigResponse(
        policy=ClassificationPolicyOut(
            threshold=policy.threshold,
            owners_can_change_answers=policy.owners_can_change_answers,
        ),
        classes=[
            ClassificationClassOut(
                key=row.key,
                title=row.title,
                description=row.description,
                kind=row.kind,
                weight=row.weight,
                active=row.active,
                updated_at=row.updated_at,
                updated_by_name=row.display_name or row.email,
            )
            for row in rows
        ],
    )


@router.get("", responses=error_responses(*ADMIN_AUTH))
async def read_classification_config(
    admin: CurrentSuperadmin, db: DbSession
) -> ClassificationConfigResponse:
    """The publish classification configuration: the policy, and every class with its
    description, kind, weight, whether it is active, and who changed it last and when."""
    return await _configuration(db)


@router.patch("/policy", dependencies=[RequireCsrf], responses=error_responses(*_WRITE_AUTH))
async def update_classification_policy(
    body: ClassificationPolicyUpdate, admin: CurrentSuperadmin, db: DbSession
) -> ClassificationConfigResponse:
    """Change the threshold (a whole number from 0 to 100), whether owners can change the
    reviewer's answers, or both. Applies to apps sent for publishing after it. Writes one
    `classification:config` audit row and answers with the whole configuration."""
    current = (
        await db.execute(
            sa.select(
                ClassificationPolicy.id,
                ClassificationPolicy.threshold,
                ClassificationPolicy.owners_can_change_answers,
            ).with_for_update()
        )
    ).one()
    threshold = current.threshold if body.threshold is None else body.threshold
    owners_can_change = (
        current.owners_can_change_answers
        if body.owners_can_change_answers is None
        else body.owners_can_change_answers
    )
    await db.execute(
        sa.update(ClassificationPolicy)
        .where(ClassificationPolicy.id == current.id)
        .values(threshold=threshold, owners_can_change_answers=owners_can_change)
    )
    await append_audit(
        db,
        actor_id=admin.id,
        action=_AUDIT_ACTION,
        resource_type="classification_policy",
        detail={
            "before": {
                "threshold": current.threshold,
                "ownersCanChangeAnswers": current.owners_can_change_answers,
            },
            "after": {"threshold": threshold, "ownersCanChangeAnswers": owners_can_change},
        },
    )
    await db.commit()
    return await _configuration(db)


@router.post(
    "/classes",
    status_code=status.HTTP_201_CREATED,
    dependencies=[RequireCsrf],
    responses=error_responses(*_WRITE_AUTH, _DUPLICATE_TITLE, _WEIGHT_REQUIRED),
)
async def add_classification_class(
    body: ClassificationClassCreate, admin: CurrentSuperadmin, db: DbSession
) -> ClassificationConfigResponse:
    """Add a class. Its key is made from the title, with a numeric suffix when that is taken,
    and never changes. A title another class holds, in any letter case, is `409
    duplicate_title`. Writes one `classification:config` audit row and answers with the whole
    configuration."""
    weight = _weight_for(body.kind, body.weight)
    key = await _free_key(db, body.title)
    try:
        async with db.begin_nested():
            await db.execute(
                sa.insert(ClassificationClass).values(
                    key=key,
                    title=body.title,
                    description=body.description,
                    kind=body.kind,
                    weight=weight,
                    active=body.active,
                    updated_by=admin.id,
                )
            )
    except IntegrityError as exc:
        if _TITLE_INDEX not in str(exc.orig):
            raise
        raise _duplicate_title(body.title) from None
    await append_audit(
        db,
        actor_id=admin.id,
        action=_AUDIT_ACTION,
        resource_type="classification_class",
        resource_id=key,
        detail={
            "before": None,
            "after": _class_state(body.title, body.description, body.kind, weight, body.active),
        },
    )
    await db.commit()
    return await _configuration(db)


@router.patch(
    "/classes/{key}",
    dependencies=[RequireCsrf],
    responses=error_responses(
        *_WRITE_AUTH,
        (404, ErrorEnvelope, "No class has this key (`class_not_found`)"),
        _DUPLICATE_TITLE,
        _WEIGHT_REQUIRED,
    ),
)
async def edit_classification_class(
    key: str, body: ClassificationClassUpdate, admin: CurrentSuperadmin, db: DbSession
) -> ClassificationConfigResponse:
    """Change a class's title, description, kind, weight or whether it is active. The key never
    changes. Turning a class off takes it out of every review and score from the next send.
    Writes one `classification:config` audit row and answers with the whole configuration."""
    current = (
        await db.execute(
            sa.select(
                ClassificationClass.title,
                ClassificationClass.description,
                ClassificationClass.kind,
                ClassificationClass.weight,
                ClassificationClass.active,
            )
            .where(ClassificationClass.key == key)
            .with_for_update()
        )
    ).one_or_none()
    if current is None:
        raise AppApiError(
            status.HTTP_404_NOT_FOUND, "There is no such class.", code="class_not_found"
        )

    title = current.title if body.title is None else body.title
    description = current.description if body.description is None else body.description
    kind = current.kind if body.kind is None else body.kind
    weight = _weight_for(
        kind, body.weight if "weight" in body.model_fields_set else current.weight
    )
    active = current.active if body.active is None else body.active
    try:
        async with db.begin_nested():
            await db.execute(
                sa.update(ClassificationClass)
                .where(ClassificationClass.key == key)
                .values(
                    title=title,
                    description=description,
                    kind=kind,
                    weight=weight,
                    active=active,
                    updated_by=admin.id,
                )
            )
    except IntegrityError as exc:
        if _TITLE_INDEX not in str(exc.orig):
            raise
        raise _duplicate_title(title) from None
    await append_audit(
        db,
        actor_id=admin.id,
        action=_AUDIT_ACTION,
        resource_type="classification_class",
        resource_id=key,
        detail={
            "before": _class_state(
                current.title, current.description, current.kind, current.weight, current.active
            ),
            "after": _class_state(title, description, kind, weight, active),
        },
    )
    await db.commit()
    return await _configuration(db)
