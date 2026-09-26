"""The administrator's classification configuration: the read, the policy, adding and editing a
class, the rules each write holds, and the audit row every write leaves."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.user import User
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.classification.config import load_live_config
from tests.factories import UserFactory

_URL = "/v1/admin/classification"
_POLICY = f"{_URL}/policy"
_CLASSES = f"{_URL}/classes"

_LAUNCH_SET: list[dict[str, Any]] = [
    {
        "key": "pii",
        "title": "PII",
        "kind": "hard_block",
        "weight": None,
        "description": (
            "Yes if the app collects, stores or shows government identity data about a person: "
            "Aadhaar, PAN, passport, driving licence or voter ID numbers, or uploaded copies of "
            "them; travel documents tied to a named person, such as passport or visa details, "
            "PNR or boarding-pass data; or biometric data, such as fingerprints or face images "
            "used to identify someone. Names, email addresses, phone numbers and home addresses "
            "on their own are not PII. Yes: a visitor pass app that stores a photo of each "
            "visitor's ID card. No: a feedback form that asks for name, email and phone."
        ),
    },
    {
        "key": "financial_data",
        "title": "Financial data",
        "kind": "hard_block",
        "weight": None,
        "description": (
            "Yes if the app collects, stores or shows payment or bank data: card numbers, bank "
            "account details, UPI IDs, or the salaries of named people. Budgets, expense totals, "
            "invoices, prices and cost reports are not financial data. Yes: a reimbursement app "
            "that stores each employee's bank account number. No: a department budget tracker "
            "with monthly totals."
        ),
    },
    {
        "key": "credentials_keys",
        "title": "Credentials & keys",
        "kind": "scored",
        "weight": 20,
        "description": (
            "Yes if the code contains an actual secret value: a hardcoded password, API key, "
            "token, private key, or a connection string or URL that carries a credential. A form "
            'field or database column named "password" is not a secret, and reading a value from '
            "the environment is not hardcoding one. Yes: an API key typed into a source file. "
            "No: a login form with a password field."
        ),
    },
    {
        "key": "confidential_business_data",
        "title": "Confidential business data",
        "kind": "scored",
        "weight": 20,
        "description": (
            "Yes if the app handles internal business information that is not meant for the "
            "public: internal metrics or reports, contracts, vendor terms, pricing, strategy or "
            "organisational data. Yes: a board that shows vendor contract rates. No: a unit "
            "converter."
        ),
    },
    {
        "key": "ai_usage",
        "title": "AI usage",
        "kind": "scored",
        "weight": 20,
        "description": (
            "Yes if the app calls an AI model or AI service, runs an AI agent, or includes an AI "
            "SDK, whatever key it uses, including one the owner brings or writes into the code. "
            "Ordinary rules, formulas and search are not AI. Yes: an app that summarises "
            "comments with a language model. No: a form that sorts requests by fixed rules."
        ),
    },
    {
        "key": "integrations",
        "title": "Integrations",
        "kind": "scored",
        "weight": 20,
        "description": (
            "Yes if the app connects to a system outside this platform, such as SAP, an ERP, CRM "
            "or HRMS, Zoho, an email or messaging service, or any third-party API, usually with "
            "a key or login the owner supplies. The platform's own data connections and plain "
            "links to other websites do not count. Yes: an app that reads accounts from Zoho "
            "CRM. No: an app that shows data from the platform's own data connection."
        ),
    },
    {
        "key": "public_data",
        "title": "Public data",
        "kind": "scored",
        "weight": 20,
        "description": (
            "Yes only if the app shows or publishes information that is already public to other "
            "people, such as published flight schedules or public reference lists. An app that "
            "handles no data answers No. Yes: a page of the airport's published shop opening "
            "hours. No: a calculator."
        ),
    },
]

_DESCRIPTION = "Yes if the app accepts file uploads. No: a calculator."


def _headers(user: User) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, settings.auth.access_ttl_seconds)
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await UserFactory.create(db_session, email="admin@bial.com", display_name="Asha Rao")


@pytest.fixture
async def admin_headers(admin: User) -> dict[str, str]:
    return _headers(admin)


async def _audit_rows(db: AsyncSession) -> list[AuditLog]:
    return list(
        (
            await db.scalars(
                sa.select(AuditLog)
                .where(AuditLog.action == "classification:config")
                .order_by(AuditLog.id)
            )
        ).all()
    )


def _class(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(row for row in body["classes"] if row["key"] == key)


async def test_a_citizen_is_refused_every_read_and_write(client, db_session) -> None:
    citizen = await UserFactory.create(db_session, email="plain@rvaiglobal.com")
    headers = _headers(citizen)
    answers = [
        await client.get(_URL, headers=headers),
        await client.patch(_POLICY, json={"threshold": 50}, headers=headers),
        await client.post(
            _CLASSES,
            json={"title": "Uploads", "description": _DESCRIPTION, "kind": "hard_block"},
            headers=headers,
        ),
        await client.patch(f"{_CLASSES}/pii", json={"active": False}, headers=headers),
    ]
    assert [answer.status_code for answer in answers] == [403, 403, 403, 403]
    assert await _audit_rows(db_session) == []


async def test_a_write_without_the_csrf_header_is_refused(client, admin) -> None:
    jwt = mint_session_jwt(admin.id, admin.token_version, settings.auth.access_ttl_seconds)
    answer = await client.patch(
        _POLICY, json={"threshold": 50}, headers={"Cookie": f"session={jwt}"}
    )
    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == "csrf_failed"


async def test_the_read_returns_the_launch_set_exactly(client, admin_headers) -> None:
    answer = await client.get(_URL, headers=admin_headers)
    assert answer.status_code == 200
    body = answer.json()
    assert body["policy"] == {"threshold": 100, "ownersCanChangeAnswers": True}
    assert [
        {name: row[name] for name in ("key", "title", "kind", "weight", "description")}
        for row in body["classes"]
    ] == _LAUNCH_SET
    assert all(row["active"] for row in body["classes"])
    assert all(row["updatedByName"] is None for row in body["classes"])
    assert all(row["updatedAt"] for row in body["classes"])


async def test_the_read_puts_hard_blocks_first_then_scored_by_weight_descending(
    client, admin_headers
) -> None:
    for title, kind, weight in (
        ("Uploads", "scored", 50),
        ("Location", "hard_block", None),
        ("Cookies", "scored", 5),
    ):
        created = await client.post(
            _CLASSES,
            json={"title": title, "description": _DESCRIPTION, "kind": kind, "weight": weight},
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text

    body = (await client.get(_URL, headers=admin_headers)).json()
    assert [(row["key"], row["weight"]) for row in body["classes"]] == [
        ("pii", None),
        ("financial_data", None),
        ("location", None),
        ("uploads", 50),
        ("credentials_keys", 20),
        ("confidential_business_data", 20),
        ("ai_usage", 20),
        ("integrations", 20),
        ("public_data", 20),
        ("cookies", 5),
    ]


async def test_updating_the_policy_saves_it_and_audits_before_and_after(
    client, db_session, admin, admin_headers
) -> None:
    answer = await client.patch(_POLICY, json={"threshold": 50}, headers=admin_headers)
    assert answer.status_code == 200
    assert answer.json()["policy"] == {"threshold": 50, "ownersCanChangeAnswers": True}

    answer = await client.patch(
        _POLICY, json={"ownersCanChangeAnswers": False}, headers=admin_headers
    )
    assert answer.json()["policy"] == {"threshold": 50, "ownersCanChangeAnswers": False}

    rows = await _audit_rows(db_session)
    assert [(row.actor_id, row.resource_type, row.detail) for row in rows] == [
        (
            admin.id,
            "classification_policy",
            {
                "before": {"threshold": 100, "ownersCanChangeAnswers": True},
                "after": {"threshold": 50, "ownersCanChangeAnswers": True},
            },
        ),
        (
            admin.id,
            "classification_policy",
            {
                "before": {"threshold": 50, "ownersCanChangeAnswers": True},
                "after": {"threshold": 50, "ownersCanChangeAnswers": False},
            },
        ),
    ]


@pytest.mark.parametrize("threshold", [101, -1, 2.5, "50", None])
async def test_a_threshold_outside_zero_through_one_hundred_is_refused(
    client, db_session, admin_headers, threshold
) -> None:
    answer = await client.patch(_POLICY, json={"threshold": threshold}, headers=admin_headers)
    assert answer.status_code == 422
    assert await _audit_rows(db_session) == []


async def test_an_empty_policy_update_is_refused(client, admin_headers) -> None:
    answer = await client.patch(_POLICY, json={}, headers=admin_headers)
    assert answer.status_code == 422


async def test_adding_a_class_stores_it_and_audits_it(
    client, db_session, admin, admin_headers
) -> None:
    answer = await client.post(
        _CLASSES,
        json={
            "title": "File uploads",
            "description": _DESCRIPTION,
            "kind": "scored",
            "weight": 20,
        },
        headers=admin_headers,
    )
    assert answer.status_code == 201
    added = _class(answer.json(), "file_uploads")
    assert added["title"] == "File uploads"
    assert added["kind"] == "scored"
    assert added["weight"] == 20
    assert added["active"] is True
    assert added["updatedByName"] == "Asha Rao"

    (row,) = await _audit_rows(db_session)
    assert row.actor_id == admin.id
    assert row.resource_type == "classification_class"
    assert row.resource_id == "file_uploads"
    assert row.detail == {
        "before": None,
        "after": {
            "title": "File uploads",
            "description": _DESCRIPTION,
            "kind": "scored",
            "weight": 20,
            "active": True,
        },
    }


async def test_a_title_differing_only_in_case_is_refused_naming_the_title(
    client, db_session, admin_headers
) -> None:
    answer = await client.post(
        _CLASSES,
        json={"title": "pii", "description": _DESCRIPTION, "kind": "hard_block"},
        headers=admin_headers,
    )
    assert answer.status_code == 409
    error = answer.json()["error"]
    assert error["code"] == "duplicate_title"
    assert "pii" in error["message"]
    assert await _audit_rows(db_session) == []


async def test_renaming_a_class_to_another_class_title_is_refused(client, admin_headers) -> None:
    answer = await client.patch(
        f"{_CLASSES}/ai_usage", json={"title": "INTEGRATIONS"}, headers=admin_headers
    )
    assert answer.status_code == 409
    assert answer.json()["error"]["code"] == "duplicate_title"
    assert "INTEGRATIONS" in answer.json()["error"]["message"]


async def test_a_class_can_keep_its_own_title_in_another_case(client, admin_headers) -> None:
    answer = await client.patch(
        f"{_CLASSES}/ai_usage", json={"title": "AI Usage"}, headers=admin_headers
    )
    assert answer.status_code == 200
    assert _class(answer.json(), "ai_usage")["title"] == "AI Usage"


async def test_a_title_whose_slug_is_taken_gets_a_numbered_key(client, admin_headers) -> None:
    """The key comes from the slug, the uniqueness check from the lower-cased title, so the two
    refuse different things."""
    answer = await client.post(
        _CLASSES,
        json={"title": "AI-Usage!", "description": _DESCRIPTION, "kind": "scored", "weight": 10},
        headers=admin_headers,
    )
    assert answer.status_code == 201
    assert _class(answer.json(), "ai_usage_2")["title"] == "AI-Usage!"
    assert _class(answer.json(), "ai_usage")["title"] == "AI usage"

    again = await client.post(
        _CLASSES,
        json={"title": "ai-usage!", "description": _DESCRIPTION, "kind": "scored", "weight": 10},
        headers=admin_headers,
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "duplicate_title"

    third = await client.post(
        _CLASSES,
        json={"title": "AI usage?", "description": _DESCRIPTION, "kind": "scored", "weight": 10},
        headers=admin_headers,
    )
    assert third.status_code == 201
    assert _class(third.json(), "ai_usage_3")["title"] == "AI usage?"


async def test_a_title_with_no_ascii_letters_still_gets_a_key(client, admin_headers) -> None:
    answer = await client.post(
        _CLASSES,
        json={"title": "डेटा", "description": _DESCRIPTION, "kind": "hard_block"},
        headers=admin_headers,
    )
    assert answer.status_code == 201
    assert _class(answer.json(), "class")["title"] == "डेटा"


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "", "description": _DESCRIPTION},
        {"title": "   ", "description": _DESCRIPTION},
        {"title": "x" * 61, "description": _DESCRIPTION},
        {"title": "Uploads", "description": ""},
        {"title": "Uploads", "description": "x" * 1001},
    ],
)
async def test_a_missing_or_over_long_title_or_description_is_refused(
    client, db_session, admin_headers, fields
) -> None:
    answer = await client.post(
        _CLASSES, json={**fields, "kind": "hard_block"}, headers=admin_headers
    )
    assert answer.status_code == 422
    assert await _audit_rows(db_session) == []


async def test_the_longest_title_and_description_are_accepted(client, admin_headers) -> None:
    answer = await client.post(
        _CLASSES,
        json={"title": "T" * 60, "description": "d" * 1000, "kind": "hard_block"},
        headers=admin_headers,
    )
    assert answer.status_code == 201


@pytest.mark.parametrize("weight", [101, -1, 2.5, "20"])
async def test_a_weight_outside_whole_numbers_zero_through_one_hundred_is_refused(
    client, db_session, admin_headers, weight
) -> None:
    created = await client.post(
        _CLASSES,
        json={"title": "Uploads", "description": _DESCRIPTION, "kind": "scored", "weight": weight},
        headers=admin_headers,
    )
    edited = await client.patch(
        f"{_CLASSES}/ai_usage", json={"weight": weight}, headers=admin_headers
    )
    assert (created.status_code, edited.status_code) == (422, 422)
    assert await _audit_rows(db_session) == []


async def test_a_scored_class_needs_a_weight(client, admin_headers) -> None:
    answer = await client.post(
        _CLASSES,
        json={"title": "Uploads", "description": _DESCRIPTION, "kind": "scored"},
        headers=admin_headers,
    )
    assert answer.status_code == 422
    assert answer.json()["error"]["code"] == "weight_required"


async def test_a_hard_block_created_with_a_weight_stores_none(client, admin_headers) -> None:
    answer = await client.post(
        _CLASSES,
        json={"title": "Uploads", "description": _DESCRIPTION, "kind": "hard_block", "weight": 30},
        headers=admin_headers,
    )
    assert answer.status_code == 201
    assert _class(answer.json(), "uploads")["weight"] is None


async def test_switching_a_scored_class_to_a_hard_block_clears_its_weight(
    client, admin_headers
) -> None:
    answer = await client.patch(
        f"{_CLASSES}/ai_usage", json={"kind": "hard_block"}, headers=admin_headers
    )
    assert answer.status_code == 200
    edited = _class(answer.json(), "ai_usage")
    assert (edited["kind"], edited["weight"]) == ("hard_block", None)


async def test_switching_a_hard_block_to_scored_without_a_weight_is_refused(
    client, db_session, admin_headers
) -> None:
    answer = await client.patch(f"{_CLASSES}/pii", json={"kind": "scored"}, headers=admin_headers)
    assert answer.status_code == 422
    assert answer.json()["error"]["code"] == "weight_required"
    assert await _audit_rows(db_session) == []

    answer = await client.patch(
        f"{_CLASSES}/pii", json={"kind": "scored", "weight": 40}, headers=admin_headers
    )
    assert answer.status_code == 200
    edited = _class(answer.json(), "pii")
    assert (edited["kind"], edited["weight"]) == ("scored", 40)


async def test_clearing_the_weight_of_a_scored_class_is_refused(client, admin_headers) -> None:
    answer = await client.patch(
        f"{_CLASSES}/ai_usage", json={"weight": None}, headers=admin_headers
    )
    assert answer.status_code == 422
    assert answer.json()["error"]["code"] == "weight_required"


async def test_editing_a_class_saves_every_field_but_the_key_and_audits_it(
    client, db_session, admin, admin_headers
) -> None:
    answer = await client.patch(
        f"{_CLASSES}/integrations",
        json={
            "key": "renamed",
            "title": "Outside systems",
            "description": _DESCRIPTION,
            "weight": 35,
            "active": False,
        },
        headers=admin_headers,
    )
    assert answer.status_code == 200
    edited = _class(answer.json(), "integrations")
    assert edited["title"] == "Outside systems"
    assert edited["description"] == _DESCRIPTION
    assert edited["weight"] == 35
    assert edited["active"] is False
    assert edited["updatedByName"] == "Asha Rao"
    assert all(row["key"] != "renamed" for row in answer.json()["classes"])

    (row,) = await _audit_rows(db_session)
    assert row.actor_id == admin.id
    assert row.resource_type == "classification_class"
    assert row.resource_id == "integrations"
    assert row.detail is not None
    assert row.detail["before"] == {
        "title": "Integrations",
        "description": _LAUNCH_SET[5]["description"],
        "kind": "scored",
        "weight": 20,
        "active": True,
    }
    assert row.detail["after"] == {
        "title": "Outside systems",
        "description": _DESCRIPTION,
        "kind": "scored",
        "weight": 35,
        "active": False,
    }


async def test_turning_a_class_off_takes_it_out_of_the_live_configuration(
    client, db_session, admin_headers
) -> None:
    answer = await client.patch(f"{_CLASSES}/pii", json={"active": False}, headers=admin_headers)
    assert answer.status_code == 200
    assert _class(answer.json(), "pii")["active"] is False

    live = await load_live_config(db_session)
    assert "pii" not in [entry.key for entry in live.classes]
    assert "financial_data" in [entry.key for entry in live.classes]


async def test_editing_a_class_that_does_not_exist_is_a_404(client, admin_headers) -> None:
    answer = await client.patch(
        f"{_CLASSES}/no_such_class", json={"active": False}, headers=admin_headers
    )
    assert answer.status_code == 404
    assert answer.json()["error"]["code"] == "class_not_found"


async def test_an_empty_class_edit_is_refused(client, admin_headers) -> None:
    answer = await client.patch(f"{_CLASSES}/pii", json={}, headers=admin_headers)
    assert answer.status_code == 422


async def test_there_is_no_route_that_deletes_a_class(client, admin_headers) -> None:
    answer = await client.delete(f"{_CLASSES}/pii", headers=admin_headers)
    assert answer.status_code == 405
    body = (await client.get(_URL, headers=admin_headers)).json()
    assert "pii" in [row["key"] for row in body["classes"]]
