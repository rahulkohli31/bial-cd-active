"""The admin-configured publish classification: classes, one policy row, a review fingerprint

Revision ID: 0049_classification_config
Revises: 0048_drop_manual_go_live
Create Date: 2026-09-26

The classes the reviewer answers and the policy the gate scores with move into two tables, seeded
here with the launch set so a first deploy starts configured. `classification_reviews` gains a
nullable `definitions_fingerprint`; every existing row keeps NULL. `deployments` drops its
per-attempt `classification` and `classification_score`: the declaration on the app row and in the
gate's audit row records what each decision was made under.

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
down_revision: str | None = "0048_drop_manual_go_live"
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
            "them; travel documents tied to a named person, such as passport or visa details, PNR "
            "or boarding-pass data; or biometric data, such as fingerprints or face images used "
            "to identify someone. One such field is enough, even an optional one. Names, email "
            "addresses, phone numbers, home addresses and employee IDs on their own are not PII. "
            "Yes: a feedback form that also asks for an Aadhaar number. Yes: a help desk log of "
            "passengers' PNRs. Yes: a visitor pass app that stores a photo of each ID card. No: a "
            "feedback form asking only for name, email and phone. No: a visitor log of name, "
            "company and phone."
        ),
    },
    {
        "key": "financial_data",
        "title": "Financial data",
        "kind": "hard_block",
        "weight": None,
        "description": (
            "Yes if the app collects, stores or shows payment or bank data: card numbers, bank "
            "account details, UPI IDs, or the salaries of named people. One such field is enough, "
            "whatever else the app does. Budgets, expense totals, invoices, prices, cost reports "
            "and pay scales not tied to a person are not financial data, unless they also carry "
            "one of those items. Yes: a vendor invoice log that stores each vendor's bank account "
            "number. Yes: a reimbursement form that asks for a UPI ID. Yes: a team roster that "
            "shows each member's salary. No: a department budget tracker with monthly totals. No: "
            "an expense claim form that records only amounts and receipts."
        ),
    },
    {
        "key": "credentials_keys",
        "title": "Credentials & keys",
        "kind": "scored",
        "weight": 20,
        "description": (
            "Yes if any saved file of the app contains an actual secret value: a hardcoded "
            "password, API key, token, private key, or a connection string or URL that carries a "
            "credential. Reading a value from the environment is not hardcoding one, but a real "
            "secret written in as its fallback is. A form field, variable or database column "
            'named "password", and placeholders such as "your-key-here", are not secrets. Yes: an '
            "API key typed into a source file. Yes: a login page that checks input against a "
            "fixed password in the code. No: a login form whose password field is checked against "
            "the database. No: a README that shows API_KEY=xxxx."
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
            "organisational data. A staff-only app is not confidential on that alone: it must "
            "handle one of these kinds of information. Information the airport already publishes, "
            "such as shop prices or opening hours, is not confidential. Yes: a board that shows "
            "vendor contract rates. Yes: a department budget tracker with monthly totals. Yes: an "
            "HR tool where managers write staff performance notes. No: a page of the airport's "
            "published shop prices. No: a staff canteen menu."
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
            "One small AI feature is enough. Ordinary rules, formulas, keyword search and canned "
            "replies are not AI, whatever the app or its buttons are called. Yes: an app that "
            "summarises comments with a language model. Yes: a complaint form that asks a model "
            "to choose the category. Yes: a search that ranks results with AI embeddings. No: a "
            'form that sorts requests by fixed rules. No: an "AI assistant" chat that picks '
            "canned answers by keyword."
        ),
    },
    {
        "key": "integrations",
        "title": "Integrations",
        "kind": "scored",
        "weight": 20,
        "description": (
            "Yes if the app connects to a system outside this platform, such as SAP, an ERP, CRM "
            "or HRMS, Zoho, an email or messaging service, or any third-party API, with or "
            "without a key or login. An outside AI service is a third-party API too. The "
            "platform's own parts do not count: the flight data and other data connections it "
            "provides, and the database and file storage it gives each app. Nor do plain links, "
            "or fonts and code libraries loaded from the web. Yes: an app that reads accounts "
            "from Zoho CRM. Yes: an alert posted to a Teams channel. Yes: weather fetched from a "
            "public API with no key. No: a flight board fed by the platform's flight data. No: "
            "records saved in the database the platform provides."
        ),
    },
    {
        "key": "public_data",
        "title": "Public data",
        "kind": "scored",
        "weight": 20,
        "description": (
            "Yes if the app shows or publishes information that is already public, such as "
            "published flight schedules or public reference lists. The app may hold other data as "
            "well; judge the public part on its own. Results worked out from what the user types, "
            "constants built into a tool, and pick-lists on a form do not count, so an app that "
            "handles no data answers No. Yes: a page of the airport's published shop opening "
            "hours. Yes: a list of public holidays. Yes: a dashboard showing the published flight "
            "schedule beside internal targets. No: a calculator. No: a unit converter. No: a "
            "complaint form with a drop-down of terminals."
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
    # Every decision's declaration lives on the app row and in its audit row; nothing reads
    # these two, so they go. A downgrade brings them back empty.
    op.drop_column("deployments", "classification_score")
    op.drop_column("deployments", "classification")
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "deployments",
        sa.Column("classification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("deployments", sa.Column("classification_score", sa.Integer(), nullable=True))
    op.drop_column("classification_reviews", "definitions_fingerprint")
    op.execute("SET LOCAL lock_timeout = DEFAULT")
    # Each table's own indexes go with it.
    op.drop_table("classification_policy")
    op.drop_table("classification_classes")
    classification_kind.drop(op.get_bind(), checkfirst=True)
