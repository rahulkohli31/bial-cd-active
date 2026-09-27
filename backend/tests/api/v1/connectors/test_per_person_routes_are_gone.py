"""Connector access is the project's switch and nothing else, so there is no per-person surface.

Every route that once asked for, withdrew, listed or decided a person's access answers 404, for a
citizen and a super-admin alike. Paired with a live read of the project route, so a broken app
that 404s everything cannot pass for a removed surface.
"""

from __future__ import annotations

import uuid

import pytest

from tests.api.v1.connectors.conftest import KEY, auth_headers
from tests.factories import ProjectFactory, UserFactory

_GONE = [
    ("GET", "/v1/connectors"),
    ("POST", f"/v1/connectors/{KEY}/request"),
    ("POST", f"/v1/connectors/{KEY}/cancel"),
    ("GET", "/v1/admin/connector-requests"),
    ("GET", "/v1/admin/connector-requests/counts"),
    ("POST", f"/v1/admin/connector-requests/{uuid.uuid4()}/approve"),
    ("POST", f"/v1/admin/connector-requests/{uuid.uuid4()}/decline"),
]


@pytest.mark.parametrize("email", ["citizen@rvaiglobal.com", "admin@bial.com"])
async def test_every_per_person_access_route_answers_404(client, db_session, email: str) -> None:
    user = await UserFactory.create(db_session, email=email)
    project = await ProjectFactory.create(db_session, user.id)
    headers = auth_headers(user)

    live = await client.get(f"/v1/projects/{project.id}/connectors", headers=headers)
    assert live.status_code == 200, live.text

    answers = {
        (method, path): (
            await client.request(method, path, headers=headers, json={"remarks": "a b c d e"})
        ).status_code
        for method, path in _GONE
    }

    assert answers == dict.fromkeys(_GONE, 404)
