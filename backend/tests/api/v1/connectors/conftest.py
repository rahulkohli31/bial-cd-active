"""Fixtures and seeds shared by the two connector route suites.

Both modules need the same three things — a signed-in citizen with a CSRF token, a ledger row
in a chosen state, and the registry's one key — so they live here rather than being imported
across sibling test modules.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from src.config import settings
from src.core.connectors import CONNECTORS
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.user import User
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt

CONNECTORS_URL = "/v1/connectors"

# The registry's only key, taken from the catalogue rather than written down. R18 keeps the
# connector's own name out of every identifier in this tree, and a literal here would be a second
# place the key is spelled — which is how a test starts passing against a key nothing serves.
KEY = next(iter(CONNECTORS))
UNKNOWN_KEY = "no-such-system"

REMARKS = (
    "I build the operational boards for the T2 duty managers and they need the live "
    "flight schedule rather than a spreadsheet."
)
DECLINE_REMARKS = (
    "Nothing you have built needs operational flight data yet. Ask again when something does."
)


def auth_headers(user: User, *, with_csrf: bool = True) -> dict[str, str]:
    """Cookie session + the signed double-submit CSRF pair. `with_csrf=False` is the shape a
    cross-site form post would arrive in — the cookie rides along, the header does not."""
    jwt = mint_session_jwt(user.id, user.token_version, settings.auth.access_ttl_seconds)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


async def seed_request(
    db,
    user_id: uuid.UUID,
    status: ConnectorRequestStatus,
    **overrides: Any,
) -> ConnectorAccessRequest:
    """One `connector_access_requests` row, flushed — the ledger states the routes read but do
    not all write (an administrator's approval and decline land in U5)."""
    data: dict[str, Any] = {
        "user_id": user_id,
        "connector_key": KEY,
        "status": status,
        "requester_remarks": REMARKS,
    }
    data.update(overrides)
    row = ConnectorAccessRequest(**data)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def seed_decision(
    db,
    user_id: uuid.UUID,
    status: ConnectorRequestStatus,
    decider: User | None,
    **overrides: Any,
) -> ConnectorAccessRequest:
    return await seed_request(
        db,
        user_id,
        status,
        decided_by_id=None if decider is None else decider.id,
        decided_at=datetime.now(UTC),
        **overrides,
    )
