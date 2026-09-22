"""Revision 0044: `chat_kind` gains a third label, and a conversation may have no project.

TWO LANES, deliberately split — every up/down round-trip permanently burns `pg_attribute` slots
on the shared `citizen_one_test` database, so round-trip coverage is budgeted rather than added
reflexively. The **default lane** asserts the SHAPE the fresh upgrade left behind, against the
real migrated schema inside the per-test transaction, and drives the CHECK constraint at the
database rather than through the validator above it. The **destructive lane**
(`uv run pytest -m destructive_migration`) walks the chain for real, because the round trip and
the per-revision transaction cannot be proved from a fresh schema.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings
from src.db.models.conversation import ChatKind, Conversation
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_REVISION = "0044_chat_kind_generic"
_PRE_REVISION = "0043_pending_teardown"

_TYPE_LABELS_SQL = (
    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
    "WHERE t.typname = :name ORDER BY e.enumsortorder"
)
_COLUMN_SQL = (
    "SELECT is_nullable, udt_name FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = :column"
)


# --- the shape the fresh upgrade left -------------------------------------------------


async def test_chat_kind_carries_exactly_three_labels(db_session) -> None:
    labels = (
        (await db_session.execute(sa.text(_TYPE_LABELS_SQL), {"name": "chat_kind"}))
        .scalars()
        .all()
    )
    assert list(labels) == ["plan", "build", "generic"]
    # …and the Python enum agrees, so a value the database accepts is one the code can name.
    assert [member.value for member in ChatKind] == list(labels)


@pytest.mark.parametrize("table", ["conversations", "messages"])
async def test_both_kind_columns_still_carry_the_native_type_not_null(
    db_session, table: str
) -> None:
    """The rebuild swaps the type under both columns; neither loses its NOT NULL along the way."""
    row = (
        await db_session.execute(sa.text(_COLUMN_SQL), {"table": table, "column": "kind"})
    ).one()
    assert row.udt_name == "chat_kind"
    assert row.is_nullable == "NO"


async def test_the_project_column_is_nullable_now(db_session) -> None:
    row = (
        await db_session.execute(
            sa.text(_COLUMN_SQL), {"table": "conversations", "column": "project_id"}
        )
    ).one()
    assert row.is_nullable == "YES"


async def test_a_generic_conversation_persists_and_reloads_with_no_project(db_session) -> None:
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.GENERIC, project_id=None
    )

    db_session.expunge(conversation)
    reloaded = await db_session.get(Conversation, conversation.id)

    assert reloaded is not None
    assert reloaded.kind is ChatKind.GENERIC
    assert reloaded.project_id is None


# --- the constraint, driven at the database -------------------------------------------
#
# Through the ORM but past the request schema: the validator above it is a different guard with
# a different failure, and a test that only drives the validator proves nothing about a row
# inserted by anything else.


async def test_the_database_refuses_a_generic_conversation_that_names_a_project(
    db_session,
) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(
                ConversationFactory.build(user.id, kind=ChatKind.GENERIC, project_id=project.id)
            )
            await db_session.flush()
    # ON THE CONSTRAINT NAME, never on a bare `IntegrityError`: a NOT NULL violation and a CHECK
    # violation raise the same class, so "it raised" is not evidence this constraint fired.
    assert "ck_conversations_parentage" in str(caught.value)


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.BUILD])
async def test_the_database_refuses_a_project_bearing_kind_with_no_project(
    db_session, kind: ChatKind
) -> None:
    user = await UserFactory.create(db_session)

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(ConversationFactory.build(user.id, kind=kind, project_id=None))
            await db_session.flush()
    assert "ck_conversations_parentage" in str(caught.value)


async def test_deleting_a_project_leaves_the_owners_generic_chats_alone(db_session) -> None:
    """The FK cascade takes the project's own sessions and nothing else. A generic chat belongs
    to the citizen, so it has no parent to be taken down with."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    build_chat = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    generic_chat = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.GENERIC, project_id=None
    )

    await db_session.execute(sa.text("DELETE FROM projects WHERE id = :id"), {"id": project.id})
    await db_session.flush()
    db_session.expunge_all()

    assert await db_session.get(Conversation, build_chat.id) is None
    assert await db_session.get(Conversation, generic_chat.id) is not None


def test_the_revision_id_fits_the_version_column() -> None:
    """`alembic_version.version_num` is `varchar(32)` and this repo has no headroom left. The
    length is compared against a LITERAL, never against the same string that produced it — the
    latter passes at any length."""
    assert _REVISION == "0044_chat_kind_generic"
    assert len(_REVISION) <= 32


# --- the chain walk (destructive lane) ------------------------------------------------


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _labels(url: URL | None = None) -> list[str]:
    """Read the enum's labels between alembic commands on a fresh engine (alembic's env.py owns
    the loop during the commands, so this runs outside the suite's per-test transaction)."""

    async def _read() -> list[str]:
        engine = create_async_engine(
            url or make_url(settings.DATABASE_URL.get_secret_value()), poolclass=NullPool
        )
        try:
            async with engine.connect() as conn:
                result = await conn.execute(sa.text(_TYPE_LABELS_SQL), {"name": "chat_kind"})
                return list(result.scalars())
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _stamped_revision() -> str | None:
    async def _read() -> str | None:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
        finally:
            await engine.dispose()

    return asyncio.run(_read())


@pytest.mark.destructive_migration
def test_the_round_trip_leaves_the_type_with_three_labels() -> None:
    """★ Down to the prior revision and back up — the half a fresh schema cannot show.

    The downgrade is the reason the type is rebuilt rather than extended: PostgreSQL has no
    inverse for `ALTER TYPE … ADD VALUE`, so an added label would still be sitting there."""
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize the start state (a no-op when at head)

    command.downgrade(config, _PRE_REVISION)
    try:
        assert _labels() == ["plan", "build"]
    finally:
        command.upgrade(config, "head")
    assert _labels() == ["plan", "build", "generic"]


_FAILING_REVISION = """\
from __future__ import annotations

revision = "0044_tpm_probe"
down_revision = "0044_chat_kind_generic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raise RuntimeError("the probe revision fails on purpose")


def downgrade() -> None:
    pass
"""


@pytest.mark.destructive_migration
def test_a_failing_revision_leaves_the_stamp_at_the_last_one_that_succeeded() -> None:
    """★ `transaction_per_migration`, proved rather than read off the configuration.

    A throwaway revision chained after 0044 raises, and the run is started from 0043 so two
    revisions are in it. Under ONE transaction for the whole run the stamp rolls back to where
    the run began and an operator cannot tell which revisions actually applied; under one per
    revision, 0044 is committed and named. The probe does no DDL, so it costs no
    `pg_attribute` slot."""
    probe = _BACKEND_ROOT / "alembic" / "versions" / "_tpm_probe.py"
    probe.write_text(_FAILING_REVISION)
    try:
        config = _alembic_config()  # built AFTER the file exists, so alembic discovers it
        command.upgrade(config, _REVISION)
        command.downgrade(config, _PRE_REVISION)
        assert _stamped_revision() == _PRE_REVISION

        with pytest.raises(RuntimeError, match="fails on purpose"):
            command.upgrade(config, "heads")

        assert _stamped_revision() == _REVISION
    finally:
        probe.unlink(missing_ok=True)
        command.upgrade(_alembic_config(), "head")


@pytest.mark.destructive_migration
def test_a_brand_new_database_walks_the_whole_chain_to_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The other direction a deploy can take: a database with nothing in it at all.

    ON A THROWAWAY DATABASE, not the shared one. Walking `citizen_one_test` down to base is
    destructive to every other test in the run and impossible here anyway — `0001` drops the
    `vector` extension, which the test role does not own. The scratch database also costs the
    shared one no `pg_attribute` slots.

    `settings.DATABASE_URL` is what has to move, not the alembic config: `alembic/env.py` sets
    the url from settings on every command, so a url set on the `Config` here is overwritten
    before the first revision runs."""
    scratch = f"citizen_one_test_fresh_{uuid.uuid4().hex[:8]}"
    url = make_url(settings.DATABASE_URL.get_secret_value())

    def _admin(statement: str) -> None:
        async def _run() -> None:
            engine = create_async_engine(
                url.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
            )
            try:
                async with engine.connect() as conn:
                    await conn.execute(sa.text(statement))
            finally:
                await engine.dispose()

        asyncio.run(_run())

    try:
        _admin(f'CREATE DATABASE "{scratch}"')
    except ProgrammingError as exc:  # no CREATEDB on this role — a local limit, not a defect
        pytest.skip(f"cannot create a scratch database here: {exc}")

    scratch_url = url.set(database=scratch)
    monkeypatch.setattr(settings, "DATABASE_URL", SecretStr(scratch_url.render_as_string(False)))
    try:
        try:
            command.upgrade(_alembic_config(), "head")
        except ProgrammingError as exc:
            # `0001` installs pgvector, which PostgreSQL reserves to a superuser. The shared test
            # database was created by one; a database this role creates cannot have it. A local
            # privilege limit, named rather than worked around — this test is meaningful
            # wherever the role can install the extension.
            if "permission denied to create extension" not in str(exc):
                raise
            pytest.skip(f"the test role cannot install pgvector on a new database: {exc}")
        assert _labels(scratch_url) == ["plan", "build", "generic"]
    finally:
        monkeypatch.undo()
        _admin(f'DROP DATABASE "{scratch}" WITH (FORCE)')


@pytest.mark.destructive_migration
def test_the_downgrade_removes_generic_conversations_rather_than_inventing_parentage() -> None:
    """★ The one row shape the downgrade cannot preserve, and it says so by deleting it.

    A generic conversation has no project, and `project_id` goes back to NOT NULL. Guessing a
    project would put a citizen's chat under someone's application; the revision deletes it and
    the docstring calls the downgrade a data-loss operation."""
    config = _alembic_config()
    command.upgrade(config, "head")

    user_id, conversation_id = uuid.uuid4(), uuid.uuid4()
    tag = conversation_id.hex[:8]

    async def _seed() -> None:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO users (id, azure_oid, email, token_version) "
                        "VALUES (:id, :oid, :email, 0)"
                    ),
                    {"id": user_id, "oid": f"b44-{tag}", "email": f"b44-{tag}@rvaiglobal.com"},
                )
                await conn.execute(
                    sa.text(
                        "INSERT INTO conversations (id, user_id, project_id, kind) "
                        "VALUES (:id, :user_id, NULL, 'generic')"
                    ),
                    {"id": conversation_id, "user_id": user_id},
                )
        finally:
            await engine.dispose()

    async def _survivors() -> int:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return (
                    await conn.scalar(
                        sa.text("SELECT count(*) FROM conversations WHERE id = :id"),
                        {"id": conversation_id},
                    )
                    or 0
                )
        finally:
            await engine.dispose()

    async def _cleanup() -> None:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    try:
        assert asyncio.run(_survivors()) == 1
        command.downgrade(config, _PRE_REVISION)
        command.upgrade(config, "head")
        assert asyncio.run(_survivors()) == 0
    finally:
        asyncio.run(_cleanup())
