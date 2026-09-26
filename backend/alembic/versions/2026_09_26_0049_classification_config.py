"""The admin-configured publish classification: classes, one policy row, a review fingerprint

Revision ID: 0049_classification_config
Revises: 0047_drop_connector_access
Create Date: 2026-09-26

The classes the reviewer answers and the policy the gate scores with move into two tables, seeded
here with the launch set so a first deploy starts configured. `classification_reviews` gains a
nullable `definitions_fingerprint`; every existing row keeps NULL.

The labels, the rules and the seed text are literals, never imported — a migration is a historical
record (ADR-0008) — and the enum type's lifecycle is explicit because dropping a table does not
drop its type. `downgrade` drops everything this revision created, administrators' edits included.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0049_classification_config"
down_revision: str | None = "0047_drop_connector_access"
branch_labels: str | None = None
depends_on: str | None = None

classification_kind = postgresql.ENUM(
    "hard_block", "scored", name="classification_kind", create_type=False
)

_WEIGHT_FOR_KIND = (
    "(kind = 'hard_block' AND weight IS NULL) OR "
    "(kind = 'scored' AND weight IS NOT NULL AND weight BETWEEN 0 AND 100)"
)

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


def upgrade() -> None:
    classification_kind.create(op.get_bind(), checkfirst=True)
    classes = op.create_table(
        "classification_classes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=60), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("kind", classification_kind, nullable=False),
        sa.Column("weight", sa.SmallInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_WEIGHT_FOR_KIND, name="ck_classification_classes_weight_for_kind"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_classification_classes_key"),
    )
    op.create_index(
        "uq_classification_classes_title",
        "classification_classes",
        [sa.text("lower(title)")],
        unique=True,
    )

    policy = op.create_table(
        "classification_policy",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("threshold", sa.SmallInteger(), nullable=False),
        sa.Column("owners_can_change_answers", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "threshold BETWEEN 0 AND 100", name="ck_classification_policy_threshold"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_classification_policy_one_row",
        "classification_policy",
        [sa.text("(true)")],
        unique=True,
    )

    # `uuidv7()` is monotonic within a session, so the ids keep this list's order, and the admin
    # read breaks weight ties by id.
    op.bulk_insert(classes, _LAUNCH_SET)
    op.bulk_insert(policy, [{"threshold": 100, "owners_can_change_answers": True}])

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "classification_reviews",
        sa.Column("definitions_fingerprint", sa.String(length=64), nullable=True),
    )
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("classification_reviews", "definitions_fingerprint")
    op.execute("SET LOCAL lock_timeout = DEFAULT")
    # Each table's own indexes go with it.
    op.drop_table("classification_policy")
    op.drop_table("classification_classes")
    classification_kind.drop(op.get_bind(), checkfirst=True)
