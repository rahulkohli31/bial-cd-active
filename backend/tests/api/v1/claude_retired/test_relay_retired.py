"""GUARDS: the legacy chat relay `POST /v1/claude` is GONE (R72, R17).

The relay was the SECOND way of running a turn: its own history load, its own stream reader,
its own send path, its own copy of the build-in-flight gate. Retiring it — rather than gating
it, or leaving it mounted and uncalled — is what makes "one turn engine" true, because a
mounted second engine is a way around every bound the first one enforces. #170 removed the
last caller from the portal; this removes the door.

Per the repo's retire-a-behaviour convention
(`docs/solutions/conventions/cleanly-removing-dead-ui-controls-2026-06-23.md`, the same flip
`tests/api/v1/apps/test_submit_retired.py` carries for `POST /apps/{id}/submit`), the route's
tests become guards that it stays gone. If any of these fails, someone reinstated the relay.

The BEHAVIOUR the relay carried is not gone — it is the turn engine
(`POST /v1/conversations/{id}/turns`), and its coverage lives under
`tests/api/v1/conversations/` and `tests/journeys/`. Two of the relay's own suites were moved
rather than deleted: the multi-turn/attachment journeys, and the project-description grounding
tests (`tests/api/v1/conversations/test_project_grounding.py`).

`tests/api/v1/claude/test_chat_stream.py` used to assert the OPPOSITE of the OpenAPI check
below — that `/v1/claude` WAS documented. It died with its directory; this is its inverse.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import Message
from src.db.models.token_usage import TokenUsage
from src.main import create_app
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _headers(user) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


async def test_the_relay_is_gone_even_with_every_precondition_staged(client, db_session) -> None:
    # The strongest reinstatement probe: everything a successful relay call needed is in
    # place — a valid session, a real owned conversation, a well-formed body — and the answer
    # is still "no such route", never a turn.
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )

    resp = await client.post(
        "/v1/claude",
        headers=_headers(user),
        json={"conversationId": str(conv.id), "message": {"text": "hi"}},
    )

    assert resp.status_code in (404, 405)
    # And it did NOTHING as a side effect: a half-removed handler would persist the user turn
    # before streaming, and bill the run afterwards.
    messages = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conv.id)
    )
    assert messages == 0
    usage = await db_session.scalar(
        select(func.count()).select_from(TokenUsage).where(TokenUsage.user_id == user.id)
    )
    assert usage == 0


async def test_the_relay_is_gone_unauthenticated_too(client) -> None:
    # No auth-shaped answer either: a 401 would mean a handler is still mounted and merely
    # gated, which is a relay you can reach the moment the gate moves.
    resp = await client.post("/v1/claude", json={"conversationId": str(uuid.uuid4())})
    assert resp.status_code in (404, 405)


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
async def test_no_verb_on_the_relay_path_answers(client, method: str) -> None:
    # Retiring one verb and leaving the path mounted is the half-removal this catches.
    resp = await getattr(client, method)("/v1/claude")
    assert resp.status_code in (404, 405)


def test_the_relay_is_gone_from_the_openapi_schema() -> None:
    # Retired means UNDOCUMENTED: no client is invited to call one.
    paths = create_app().openapi()["paths"]
    assert "/v1/claude" not in paths
    relay_shaped = [path for path in paths if path.startswith("/v1/claude")]
    assert relay_shaped == []


def test_there_is_exactly_one_send_path_and_one_stream_path() -> None:
    """STRUCTURAL: no second send path or conversation stream is mounted under `/v1`. This is
    the assertion that would have gone red at any point in the U10→R72 window, and the one
    that stops a third engine arriving the way the second did.

    `/v1/build-sessions/{session_id}/events` IS a second stream, and it is named here rather
    than filtered away: it is the older build harness, still mounted and unreachable from the
    portal, and retiring it is its own tracked piece of work. Pinning it means this test goes
    red when it goes — which is the right way round. What must never grow is the list."""
    paths = create_app().openapi()["paths"]

    senders = sorted(p for p, ops in paths.items() if "post" in ops and p.endswith("/turns"))
    assert senders == ["/v1/conversations/{conversation_id}/turns"]

    streamers = sorted(p for p, ops in paths.items() if "get" in ops and p.endswith("/events"))
    assert streamers == [
        "/v1/build-sessions/{session_id}/events",  # the known second harness; see above
        "/v1/conversations/{conversation_id}/events",
    ]


def test_the_relay_module_is_gone_from_the_tree() -> None:
    # An unmounted-but-present module is how a retired surface comes back: the next reader
    # sees working code and one missing `include_router`.
    import importlib.util

    assert importlib.util.find_spec("src.api.v1.claude") is None
