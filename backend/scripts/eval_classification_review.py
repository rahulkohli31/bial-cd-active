"""Evaluate the classification review's budgets, its misses, and the golden scenarios.

WHY THIS EXISTS: the review ships with PROVISIONAL ceilings (wall-clock, request budget, the
per-class output cap) sized from ad-hoc runs that measured cost, not accuracy. This runs the real
review loop against the live class configuration and reports wall-clock, requests, tool calls,
token classes — the final step's output count SEPARATELY, since the cap is later re-set from it —
each class's answer, catch/miss per seeded finding, and the deployment used (ceilings don't
transfer). It also scores the model-free credential scan: Tier A/B precision-recall SEPARATELY,
gated on Tier A reaching 100% precision.

Two named figures: the FALSE-POSITIVE ROUTING RATE (known-clean bundles the gate would route,
counting a run failure as a route, deliberately cautious) and the MISS RATE (seeded findings a
completed review did not answer Yes on). `--golden` runs the built-in scenarios instead of
bundles, each with the answers the seeded class descriptions must produce.

Drives `scan_snapshot` + `agent.run_review` directly, NOT `ClassificationReviewService` — the
service's own ceilings would censor the very distributions this eval measures. Run from
`backend/` with the backend env loaded (only `--help` and argument errors are env-free): model
runs read the class configuration from the database and need `FOUNDRY__*`, which `--scan-only`
does not. OUTPUT is an operator artifact: rows carry citizen file paths (never values), so it
lives OUTSIDE the repo tree.
"""

# `--help` prints the hand-written `description=` in `_build_parser`, not this docstring.

from __future__ import annotations

import sys
from pathlib import Path

# Executed by PATH (`python scripts/eval_...py`) sys.path[0] is `scripts/`, not the
# backend root, so `src` would not resolve; `-m` mode and the test import need nothing.
# The shim makes both documented invocations work (tests/conftest.py's noqa pattern).
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Callable, Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict  # noqa: E402

if TYPE_CHECKING:  # env-poisoned import chain — type-only here, runtime import is lazy
    from src.services.classification.config import LiveConfig
    from src.services.classification.scan import CredentialSweep

from pydantic_ai.exceptions import (  # noqa: E402
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models import Model, ModelRequestParameters  # noqa: E402
from pydantic_ai.models.wrapper import WrapperModel  # noqa: E402
from pydantic_ai.settings import ModelSettings  # noqa: E402
from pydantic_ai.usage import UsageLimits  # noqa: E402

# The classification agent/scan/config and the gate are DELIBERATELY NOT imported here:
# their import chain reaches `src.services.agent` (package init) and the DB engine,
# which resolve the full Settings at import — so importing them at module level would
# make even `--help` demand a configured environment. They are imported inside the
# functions that run the sweep; everything imported below is verified env-free.
from src.core.redaction import Tier, redact_and_cap  # noqa: E402
from src.services.classification.schema import ReviewOutput  # noqa: E402
from src.services.storage.bundle import (  # noqa: E402
    BundleValidationError,
    parse_bundle_head_sha,
)
from src.services.storage.errors import StorageError  # noqa: E402
from src.services.storage.snapshot_read import (  # noqa: E402
    NoAppYet,
    SnapshotExtractionError,
    extract_snapshot,
)

ModelFactory = Callable[[], Model]

#: Bound on one local `git clone` from a bundle (mirrors the snapshot reader's bound —
#: a HEAD-only bundle extracts in well under this; a hang is a wedged git).
_CLONE_TIMEOUT_S: Final = 60.0

# Failure kinds. `extract_failed` is the pre-model edge — a bundle that never opens is still
# a report ROW, not a dropped sample; the model-phase kinds mirror the service's taxonomy in
# spirit, but the mapping to citizen-facing buckets stays the service's own business.
_EXTRACT_FAILED: Final = "extract_failed"
_NO_APP_YET: Final = "no_app_yet"
_STORAGE_UNAVAILABLE: Final = "storage_unavailable"
_OUTPUT_TRUNCATED: Final = "output_truncated"
_REQUEST_LIMIT: Final = "request_limit_exhausted"
_RUN_TIMEOUT: Final = "run_timeout"
_MODEL_ERROR: Final = "model_error"

#: How much of a failure detail is worth keeping in the report, matching the runner's own
#: ceiling. Its own constant rather than an import of the service's private one: this
#: script deliberately keeps the classification modules out of its import chain (see the
#: module docstring), and how much diagnostic to keep is each pipeline's to decide.
_DETAIL_MAX_CHARS: Final = 2_000


class SpecError(Exception):
    """The sample spec (arguments + manifests) is unusable. `main` turns this into the
    parser's usage-and-exit — never a traceback, never a partial sweep over a spec with
    a typo in it."""


class _EvalRunFailedError(Exception):
    """One bundle's run failed. Carries the report row's failure kind and detail; the
    sweep records the row and moves on."""

    def __init__(self, kind: str, detail: str | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


class _TruncatedError(Exception):
    """The model stopped at the output token cap (`finish_reason == "length"`). Raised
    from inside the model seam — the service's tripwire, minus its guided retry: for
    the eval a truncation is a failure row, and the at-cap output-token count it leaves
    in the recorder is itself a data point for the cap distribution."""

    def __init__(self, raw_finish_reason: str) -> None:
        super().__init__(f"model output truncated (finish_reason={raw_finish_reason!r})")
        self.raw_finish_reason = raw_finish_reason


class _FlightRecorder(WrapperModel):
    """The run's black box: it survives whatever happens to the flight. Counts model
    requests and non-output tool calls, accumulates the four RAW token classes (raw
    means raw — pydantic-ai's `input_tokens` already includes the cache classes, and
    re-adding them is the documented double-count regression), keeps the LAST step's
    output-token count separately, and trips on truncation."""

    def __init__(self, wrapped: Model, *, output_tool_name: str) -> None:
        super().__init__(wrapped)
        self._output_tool_name = output_tool_name
        self.requests = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.final_step_output_tokens = 0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        self.requests += 1
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        # Overwritten every step: after the run, this holds the FINAL step's count —
        # the one the output cap binds on (only the structured output step is large).
        self.final_step_output_tokens = usage.output_tokens
        self.tool_calls += sum(
            1
            for part in response.parts
            if isinstance(part, ToolCallPart) and part.tool_name != self._output_tool_name
        )
        if response.finish_reason == "length":
            details = response.provider_details or {}
            raise _TruncatedError(raw_finish_reason=str(details.get("finish_reason", "length")))
        return response


# ---------------------------------------------------------------------------------------
# The golden scenarios
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenScenario:
    """One built-in app and the answers the seeded class descriptions must produce for it:
    `expect` names classes and their answers, and `every_class_no` expects No on every active
    class besides."""

    name: str
    files: dict[str, str]
    expect: dict[str, str]
    every_class_no: bool = False


# The platform's own database client, as the generated-app template ships it (abridged).
_PLATFORM_DB: Final[dict[str, str]] = {
    "db/index.ts": (
        'import { drizzle } from "drizzle-orm/node-postgres";\n'
        'import { Pool } from "pg";\n'
        'import * as schema from "./schema";\n\n'
        "// The app's own database, provided by the platform.\n"
        "let pool: Pool | undefined;\n"
        "export function getDb() {\n"
        '  if (typeof window !== "undefined") throw new Error("server only");\n'
        "  pool ??= new Pool({ connectionString: process.env.BIAL_DATABASE_URL, max: 3 });\n"
        "  return drizzle(pool, { schema });\n"
        "}\n"
    ),
}

# The platform's own file storage, reached the way the template's environment manifest says.
_PLATFORM_STORAGE: Final[dict[str, str]] = {
    "lib/storage.ts": (
        "// The app's own file storage, provided by the platform.\n"
        "export async function saveFile(name: string, body: Blob): Promise<string> {\n"
        "  const base = process.env.BIAL_BLOB_CONTAINER_URL;\n"
        "  const sas = process.env.BIAL_BLOB_SAS;\n"
        '  if (!base || !sas) throw new Error("File storage is not configured.");\n'
        "  const res = await fetch(`${base}/${encodeURIComponent(name)}${sas}`, {\n"
        '    method: "PUT",\n'
        '    headers: { "x-ms-blob-type": "BlockBlob" },\n'
        "    body,\n"
        "  });\n"
        "  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);\n"
        "  return name;\n"
        "}\n"
    ),
}


def _form_page(title: str, endpoint: str, fields: Sequence[tuple[str, str, bool]]) -> str:
    """A client form page posting JSON to `endpoint`; fields are (name, label, required)."""
    inputs = "\n".join(
        f'        <label>{label}<input name="{name}"{" required" if required else ""} /></label>'
        for name, label, required in fields
    )
    return (
        '"use client";\n\n'
        "export default function Page() {\n"
        "  async function submit(form: FormData) {\n"
        f'    await fetch("{endpoint}", {{\n'
        '      method: "POST",\n'
        '      headers: { "content-type": "application/json" },\n'
        "      body: JSON.stringify(Object.fromEntries(form)),\n"
        "    });\n"
        "  }\n"
        "  return (\n"
        '    <main className="p-6">\n'
        f"      <h1>{title}</h1>\n"
        "      <form action={submit}>\n"
        f"{inputs}\n"
        '        <button type="submit">Send</button>\n'
        "      </form>\n"
        "    </main>\n"
        "  );\n"
        "}\n"
    )


def _insert_route(table: str, columns: Sequence[str]) -> str:
    values = ", ".join(f"{column}: body.{column}" for column in columns)
    return (
        'import { getDb } from "@/db";\n'
        f'import {{ {table} }} from "@/db/schema";\n\n'
        "export async function POST(req: Request) {\n"
        "  const body = await req.json();\n"
        f"  await getDb().insert({table}).values({{ {values} }});\n"
        "  return Response.json({ ok: true });\n"
        "}\n"
    )


def _table(table: str, columns: Sequence[tuple[str, bool]]) -> str:
    lines = "\n".join(
        f'  {name}: text("{name}"){".notNull()" if required else ""},'
        for name, required in columns
    )
    return (
        'import { pgTable, serial, text, timestamp } from "drizzle-orm/pg-core";\n\n'
        f'export const {table} = pgTable("{table}", {{\n'
        '  id: serial("id").primaryKey(),\n'
        f"{lines}\n"
        '  createdAt: timestamp("created_at").defaultNow(),\n'
        "});\n"
    )


GOLDEN_SCENARIOS: Final[tuple[GoldenScenario, ...]] = (
    GoldenScenario(
        name="calculator",
        files={
            "app/page.tsx": (
                '"use client";\nimport { useState } from "react";\n\n'
                "export default function Calculator() {\n"
                '  const [a, setA] = useState("");\n'
                '  const [b, setB] = useState("");\n'
                '  const [op, setOp] = useState("+");\n'
                "  const x = Number(a);\n"
                "  const y = Number(b);\n"
                '  const result = op === "+" ? x + y : op === "-" ? x - y : '
                'op === "*" ? x * y : x / y;\n'
                "  return (\n"
                '    <main className="p-6">\n'
                "      <h1>Calculator</h1>\n"
                "      <input value={a} onChange={(e) => setA(e.target.value)} />\n"
                "      <select value={op} onChange={(e) => setOp(e.target.value)}>\n"
                "        <option>+</option><option>-</option>\n"
                "        <option>*</option><option>/</option>\n"
                "      </select>\n"
                "      <input value={b} onChange={(e) => setB(e.target.value)} />\n"
                '      <p>Result: {Number.isFinite(result) ? result : "-"}</p>\n'
                "    </main>\n"
                "  );\n"
                "}\n"
            ),
        },
        expect={},
        every_class_no=True,
    ),
    GoldenScenario(
        name="feedback form asking for name, email and phone",
        files={
            **_PLATFORM_DB,
            "app/page.tsx": _form_page(
                "Passenger feedback",
                "/api/feedback",
                [
                    ("name", "Name", True),
                    ("email", "Email", True),
                    ("phone", "Phone", False),
                    ("comments", "Comments", True),
                ],
            ),
            "app/api/feedback/route.ts": _insert_route(
                "feedback", ["name", "email", "phone", "comments"]
            ),
            "db/schema.ts": _table(
                "feedback",
                [("name", True), ("email", True), ("phone", False), ("comments", True)],
            ),
        },
        expect={"pii": "no"},
    ),
    GoldenScenario(
        name="event sign-up form collecting name, email and phone",
        files={
            **_PLATFORM_DB,
            "app/page.tsx": _form_page(
                "Staff sports day sign-up",
                "/api/signups",
                [
                    ("name", "Name", True),
                    ("email", "Email", True),
                    ("phone", "Phone", True),
                    ("event", "Event (100m, relay, tug of war)", True),
                ],
            ),
            "app/api/signups/route.ts": _insert_route(
                "signups", ["name", "email", "phone", "event"]
            ),
            "db/schema.ts": _table(
                "signups", [("name", True), ("email", True), ("phone", True), ("event", True)]
            ),
        },
        expect={"pii": "no"},
    ),
    GoldenScenario(
        name="feedback form that also asks for an Aadhaar number",
        files={
            **_PLATFORM_DB,
            "app/page.tsx": _form_page(
                "Passenger feedback",
                "/api/feedback",
                [
                    ("name", "Name", True),
                    ("email", "Email", True),
                    ("phone", "Phone", False),
                    ("aadhaar", "Aadhaar number (optional)", False),
                    ("comments", "Comments", True),
                ],
            ),
            "app/api/feedback/route.ts": _insert_route(
                "feedback", ["name", "email", "phone", "aadhaar", "comments"]
            ),
            "db/schema.ts": _table(
                "feedback",
                [
                    ("name", True),
                    ("email", True),
                    ("phone", False),
                    ("aadhaar", False),
                    ("comments", True),
                ],
            ),
        },
        expect={"pii": "yes"},
    ),
    GoldenScenario(
        name="visitor pass app that uploads a copy of each Aadhaar card",
        files={
            **_PLATFORM_DB,
            **_PLATFORM_STORAGE,
            "app/page.tsx": (
                "export default function Page() {\n"
                "  return (\n"
                '    <form action="/api/visitors" method="post" encType="multipart/form-data">\n'
                '      <label>Visitor name<input name="name" required /></label>\n'
                '      <label>Company<input name="company" /></label>\n'
                '      <label>Upload Aadhaar card<input name="aadhaarCard" '
                'type="file" required /></label>\n'
                '      <button type="submit">Issue pass</button>\n'
                "    </form>\n"
                "  );\n"
                "}\n"
            ),
            "app/api/visitors/route.ts": (
                'import { getDb } from "@/db";\n'
                'import { visitors } from "@/db/schema";\n'
                'import { saveFile } from "@/lib/storage";\n\n'
                "export async function POST(req: Request) {\n"
                "  const form = await req.formData();\n"
                '  const card = form.get("aadhaarCard") as File;\n'
                "  const path = await "
                "saveFile(`aadhaar/${crypto.randomUUID()}-${card.name}`, card);\n"
                "  await getDb().insert(visitors).values({\n"
                '    name: String(form.get("name")),\n'
                '    company: String(form.get("company") ?? ""),\n'
                "    aadhaarCardPath: path,\n"
                "  });\n"
                '  return Response.redirect(new URL("/", req.url));\n'
                "}\n"
            ),
            "db/schema.ts": _table(
                "visitors", [("name", True), ("company", False), ("aadhaarCardPath", True)]
            ),
        },
        expect={"pii": "yes"},
    ),
    GoldenScenario(
        name="Zoho CRM account reader",
        files={
            "lib/zoho.ts": (
                'const ZOHO_API = "https://www.zohoapis.in/crm/v2";\n\n'
                "export async function listAccounts() {\n"
                "  const res = await "
                "fetch(`${ZOHO_API}/Accounts?fields=Account_Name,Industry`, {\n"
                "    headers: { Authorization: `Zoho-oauthtoken "
                "${process.env.ZOHO_ACCESS_TOKEN}` },\n"
                '    cache: "no-store",\n'
                "  });\n"
                "  if (!res.ok) throw new Error(`Zoho returned ${res.status}`);\n"
                "  const body = await res.json();\n"
                "  return body.data as { id: string; Account_Name: string; "
                "Industry: string | null }[];\n"
                "}\n"
            ),
            "app/page.tsx": (
                'import { listAccounts } from "@/lib/zoho";\n\n'
                "export default async function Page() {\n"
                "  const accounts = await listAccounts();\n"
                "  return (\n"
                "    <ul>\n"
                "      {accounts.map((a) => <li key={a.id}>{a.Account_Name} "
                "· {a.Industry}</li>)}\n"
                "    </ul>\n"
                "  );\n"
                "}\n"
            ),
        },
        expect={"integrations": "yes"},
    ),
    GoldenScenario(
        name="comment summariser that calls an outside language model",
        files={
            "package.json": (
                '{\n  "name": "comment-summary",\n  "dependencies": {\n'
                '    "next": "16.0.0",\n    "openai": "^5.0.0",\n    "react": "19.0.0"\n  }\n}\n'
            ),
            "app/api/summarise/route.ts": (
                'import OpenAI from "openai";\n\n'
                "const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n\n"
                "export async function POST(req: Request) {\n"
                "  const { comments } = (await req.json()) as { comments: string[] };\n"
                "  const completion = await client.chat.completions.create({\n"
                '    model: "gpt-4o-mini",\n'
                "    messages: [\n"
                '      { role: "system", content: "Summarise these passenger '
                'comments in three bullets." },\n'
                '      { role: "user", content: comments.join("\\n") },\n'
                "    ],\n"
                "  });\n"
                "  return Response.json({ summary: completion.choices[0].message.content });\n"
                "}\n"
            ),
            "app/page.tsx": (
                '"use client";\nimport { useState } from "react";\n\n'
                "export default function Page() {\n"
                '  const [text, setText] = useState("");\n'
                '  const [summary, setSummary] = useState("");\n'
                "  async function run() {\n"
                '    const res = await fetch("/api/summarise", {\n'
                '      method: "POST",\n'
                '      body: JSON.stringify({ comments: text.split("\\n") }),\n'
                "    });\n"
                "    setSummary((await res.json()).summary);\n"
                "  }\n"
                "  return (\n"
                "    <main>\n"
                "      <textarea value={text} onChange={(e) => setText(e.target.value)} />\n"
                "      <button onClick={run}>Summarise</button>\n"
                "      <pre>{summary}</pre>\n"
                "    </main>\n"
                "  );\n"
                "}\n"
            ),
        },
        expect={"ai_usage": "yes", "integrations": "yes"},
    ),
    GoldenScenario(
        name="flight board fed by the platform's flight data connection",
        files={
            "lib/flight-data.ts": (
                "// The platform's flight data connection, injected when it is switched on.\n"
                'import { BlobServiceClient } from "@azure/storage-blob";\n'
                'import { ManagedIdentityCredential } from "@azure/identity";\n\n'
                "export async function departureFiles(): Promise<string[]> {\n"
                "  const url = process.env.BIAL_FLIGHT_DATA_URL;\n"
                "  const clientId = process.env.BIAL_FLIGHT_DATA_CLIENT_ID;\n"
                "  if (!url || !clientId) {\n"
                '    throw new Error("The flight data connection is switched '
                'off for this project.");\n'
                "  }\n"
                "  const parsed = new URL(url);\n"
                '  const [container, ...rest] = parsed.pathname.slice(1).split("/");\n'
                "  const service = new BlobServiceClient(\n"
                "    parsed.origin,\n"
                "    new ManagedIdentityCredential({ clientId }),\n"
                "  );\n"
                "  const names: string[] = [];\n"
                "  const blobs = service.getContainerClient(container).listBlobsFlat({\n"
                '    prefix: rest.join("/"),\n'
                "  });\n"
                "  for await (const blob of blobs) names.push(blob.name);\n"
                "  return names;\n"
                "}\n"
            ),
            "app/page.tsx": (
                'import { departureFiles } from "@/lib/flight-data";\n\n'
                "export default async function Page() {\n"
                "  const files = await departureFiles();\n"
                "  return (\n"
                "    <main>\n"
                "      <h1>Today's departures</h1>\n"
                "      <ul>{files.map((f) => <li key={f}>{f}</li>)}</ul>\n"
                "    </main>\n"
                "  );\n"
                "}\n"
            ),
        },
        expect={"integrations": "no"},
    ),
    GoldenScenario(
        name="document library kept in the platform's file storage",
        files={
            **_PLATFORM_STORAGE,
            "app/api/documents/route.ts": (
                'import { saveFile } from "@/lib/storage";\n\n'
                "export async function POST(req: Request) {\n"
                "  const form = await req.formData();\n"
                '  const file = form.get("file") as File;\n'
                "  const name = await saveFile(`manuals/${file.name}`, file);\n"
                "  return Response.json({ name });\n"
                "}\n"
            ),
            "app/page.tsx": (
                "export default function Page() {\n"
                "  return (\n"
                '    <form action="/api/documents" method="post" encType="multipart/form-data">\n'
                "      <h1>Equipment manuals</h1>\n"
                '      <input name="file" type="file" accept="application/pdf" required />\n'
                '      <button type="submit">Upload</button>\n'
                "    </form>\n"
                "  );\n"
                "}\n"
            ),
        },
        expect={"integrations": "no"},
    ),
)


# ---------------------------------------------------------------------------------------
# The sample spec
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Source:
    """One app to evaluate: a local `.bundle` file, an app id to pull from object storage, or a
    golden scenario."""

    bundle_id: str
    kind: Literal["local", "storage", "golden"]
    path: Path | None = None
    app_id: uuid.UUID | None = None
    golden: GoldenScenario | None = None

    @property
    def origin(self) -> str:
        if self.kind == "golden":
            return "golden"
        return str(self.path) if self.kind == "local" else str(self.app_id)


@dataclass(frozen=True)
class _ScanLabels:
    """One labeled bundle: paths holding genuine secrets, and paths holding
    credential-shaped non-secrets (the login-form population)."""

    secrets: frozenset[str]
    credential_shaped: frozenset[str]


@dataclass(frozen=True)
class _EvalSpec:
    sources: tuple[_Source, ...]
    seeded: dict[str, tuple[str, ...]]
    known_clean: frozenset[str]
    scan_labels: dict[str, _ScanLabels]


def _load_json(path: Path, what: str) -> Any:
    if not path.is_file():
        raise SpecError(f"{what} manifest not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"{what} manifest {path} is not readable JSON: {exc}") from exc


def _load_seeded(path: Path) -> dict[str, tuple[str, ...]]:
    raw = _load_json(path, "--seeded")
    if not isinstance(raw, dict):
        raise SpecError(f"--seeded {path}: expected an object of bundle-id -> [category, ...]")
    seeded: dict[str, tuple[str, ...]] = {}
    for bundle_id, categories in raw.items():
        if not isinstance(categories, list) or not all(
            isinstance(category, str) for category in categories
        ):
            raise SpecError(f"--seeded {path}: {bundle_id!r} must map to a list of class keys")
        seeded[str(bundle_id)] = tuple(categories)
    return seeded


def _check_seeded(seeded: dict[str, tuple[str, ...]], keys: Sequence[str]) -> None:
    """Every seeded class key must be an active class, or its catch/miss would measure nothing."""
    for bundle_id, categories in seeded.items():
        unknown = sorted(set(categories) - set(keys))
        if unknown:
            raise SpecError(
                f"--seeded: unknown class key(s) {', '.join(unknown)} on {bundle_id!r}; "
                f"the active classes are {', '.join(keys)}"
            )


def _load_known_clean(path: Path) -> frozenset[str]:
    raw = _load_json(path, "--known-clean")
    if not isinstance(raw, list):
        raise SpecError(f"--known-clean {path}: expected a JSON list of bundle ids")
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise SpecError(f"--known-clean {path}: expected a JSON list of bundle ids")
        ids.append(item)
    return frozenset(ids)


def _load_scan_labels(path: Path) -> dict[str, _ScanLabels]:
    raw = _load_json(path, "--scan-labels")
    if not isinstance(raw, dict):
        raise SpecError(f"--scan-labels {path}: expected an object of bundle-id -> labels")
    labels: dict[str, _ScanLabels] = {}
    for bundle_id, entry in raw.items():
        if not isinstance(entry, dict):
            raise SpecError(f"--scan-labels {path}: {bundle_id!r} must map to an object")
        allowed = {"secrets", "credential_shaped"}
        unknown_keys = sorted(set(entry) - allowed)
        if unknown_keys:
            raise SpecError(
                f"--scan-labels {path}: {bundle_id!r} has unknown key(s) "
                f"{', '.join(unknown_keys)}; allowed: secrets, credential_shaped"
            )
        for key in allowed:
            paths = entry.get(key, [])
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise SpecError(f"--scan-labels {path}: {bundle_id!r}.{key} must be a path list")
        labels[str(bundle_id)] = _ScanLabels(
            secrets=frozenset(entry.get("secrets", [])),
            credential_shaped=frozenset(entry.get("credential_shaped", [])),
        )
    return labels


def _collect_sources(
    bundles: Sequence[Path], bundle_dirs: Sequence[Path], app_ids: Sequence[uuid.UUID]
) -> tuple[_Source, ...]:
    files: list[Path] = []
    for bundle in bundles:
        if not bundle.is_file():
            raise SpecError(f"--bundle {bundle}: no such file")
        files.append(bundle)
    for directory in bundle_dirs:
        if not directory.is_dir():
            raise SpecError(f"--bundle-dir {directory}: no such directory")
        found = sorted(directory.glob("*.bundle"))
        if not found:
            raise SpecError(f"--bundle-dir {directory}: contains no *.bundle files")
        files.extend(found)

    sources: list[_Source] = [
        _Source(bundle_id=path.stem, kind="local", path=path) for path in files
    ]
    sources.extend(
        _Source(bundle_id=str(app_id), kind="storage", app_id=app_id) for app_id in app_ids
    )
    if not sources:
        raise SpecError("no bundles to evaluate: pass --bundle, --bundle-dir, or --app-id")
    seen: set[str] = set()
    for source in sources:
        if source.bundle_id in seen:
            raise SpecError(
                f"duplicate bundle id {source.bundle_id!r} — manifests key on the id, "
                "so two sources sharing one would be indistinguishable"
            )
        seen.add(source.bundle_id)
    return tuple(sources)


def _golden_sources() -> tuple[_Source, ...]:
    return tuple(
        _Source(bundle_id=scenario.name, kind="golden", golden=scenario)
        for scenario in GOLDEN_SCENARIOS
    )


def _build_spec(args: argparse.Namespace) -> _EvalSpec:
    if args.golden:
        if args.bundle or args.bundle_dir or args.app_id:
            raise SpecError("--golden runs the built-in scenarios; it takes no bundles")
        if args.seeded or args.known_clean or args.scan_labels:
            raise SpecError("--golden carries its own expectations; it takes no manifests")
        return _EvalSpec(
            sources=_golden_sources(), seeded={}, known_clean=frozenset(), scan_labels={}
        )
    sources = _collect_sources(args.bundle, args.bundle_dir, args.app_id)
    seeded = _load_seeded(args.seeded) if args.seeded else {}
    known_clean = _load_known_clean(args.known_clean) if args.known_clean else frozenset()
    scan_labels = _load_scan_labels(args.scan_labels) if args.scan_labels else {}

    ids = {source.bundle_id for source in sources}
    for name, referenced in (
        ("--seeded", set(seeded)),
        ("--known-clean", set(known_clean)),
        ("--scan-labels", set(scan_labels)),
    ):
        unmatched = sorted(referenced - ids)
        if unmatched:
            raise SpecError(
                f"{name} names bundle id(s) matching nothing in the sample: "
                f"{', '.join(unmatched)} — a typo'd id would silently measure nothing"
            )
    contradictions = sorted(set(seeded) & known_clean)
    if contradictions:
        raise SpecError(
            f"bundle(s) both seeded and known-clean: {', '.join(contradictions)} — "
            "a seeded bundle is by definition not clean"
        )
    return _EvalSpec(
        sources=sources, seeded=seeded, known_clean=known_clean, scan_labels=scan_labels
    )


# ---------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------


def _clone_local_bundle(bundle_path: Path, scratch: Path) -> tuple[str, Path]:
    """Extract a local bundle the way the platform does: validated header parse for the
    HEAD SHA, then a jailed clone — scrubbed HOME (no user gitconfig hooks/filters),
    `--template=`, and `core.symlinks=false` so a planted symlink materializes inert.
    PATH passes through: the eval host is a workstation, not the jailed server."""
    head_sha = parse_bundle_head_sha(bundle_path.read_bytes())
    clone_dir = scratch / "tree"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(scratch),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.symlinks=false",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--template=",
                str(bundle_path),
                str(clone_dir),
            ],
            cwd=scratch,
            env=env,
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _EvalRunFailedError(
            _EXTRACT_FAILED, f"git clone timed out after {_CLONE_TIMEOUT_S:.0f}s"
        ) from exc
    except FileNotFoundError as exc:
        raise _EvalRunFailedError(_EXTRACT_FAILED, "the `git` binary is not on PATH") from exc
    if completed.returncode != 0:
        raise _EvalRunFailedError(_EXTRACT_FAILED, f"git clone failed: {completed.stderr[:500]}")
    return head_sha, clone_dir


def _write_golden_tree(scenario: GoldenScenario, scratch: Path) -> Path:
    root = scratch / "tree"
    for rel_path, text in scenario.files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


async def _extract(source: _Source, scratch: Path) -> tuple[str | None, Path]:
    """One app to a tree on disk, every disappointment mapped to a failure kind. A golden
    scenario is written out and names no commit."""
    if source.kind == "golden":
        if source.golden is None:  # structurally impossible; fail loudly, not silently
            raise _EvalRunFailedError(_EXTRACT_FAILED, "golden source carries no scenario")
        return None, await asyncio.to_thread(_write_golden_tree, source.golden, scratch)
    if source.kind == "local":
        if source.path is None:  # structurally impossible; fail loudly, not silently
            raise _EvalRunFailedError(_EXTRACT_FAILED, "local source carries no path")
        try:
            return await asyncio.to_thread(_clone_local_bundle, source.path, scratch)
        except BundleValidationError as exc:
            raise _EvalRunFailedError(_EXTRACT_FAILED, str(exc)) from exc
    if source.app_id is None:
        raise _EvalRunFailedError(_EXTRACT_FAILED, "storage source carries no app id")
    try:
        extracted = await extract_snapshot(source.app_id, cache_root=scratch)
    except BundleValidationError as exc:
        raise _EvalRunFailedError(_EXTRACT_FAILED, str(exc)) from exc
    except SnapshotExtractionError as exc:
        raise _EvalRunFailedError(_EXTRACT_FAILED, str(exc)) from exc
    except StorageError as exc:
        raise _EvalRunFailedError(_STORAGE_UNAVAILABLE, str(exc)) from exc
    if isinstance(extracted, NoAppYet):
        raise _EvalRunFailedError(_NO_APP_YET, "the app has no saved bundle")
    return extracted.head_sha, extracted.root


# ---------------------------------------------------------------------------------------
# One bundle's evaluation
# ---------------------------------------------------------------------------------------


def _scan_doc(sweep: CredentialSweep) -> dict[str, Any]:
    return {
        "tier_a_paths": sorted({h.path for h in sweep.hits if h.hit.tier is Tier.A}),
        "tier_b_paths": sorted({h.path for h in sweep.hits if h.hit.tier is Tier.B}),
        "hits": [
            {"path": h.path, "family": h.hit.family, "tier": h.hit.tier.value, "line": h.hit.line}
            for h in sweep.hits
        ],
        "incomplete": sweep.incomplete,
    }


class EvalRow(TypedDict):
    """One `row_type: "run"` report row — the wire shape, named once.

    A TypedDict on purpose, not a dataclass: this IS the on-disk JSONL record, so converting
    before write would add a second shape to keep in step. The real gap was never the object,
    it was the CONTRACT — an 18-parameter builder returning `dict[str, Any]`, read back by
    string key in three summarisers, where a typo is silent at every gate. Every key is always
    present (`None` where it could not apply), so `total=True` is the honest declaration."""

    row_type: str
    bundle_id: str
    source: str
    origin: str
    timestamp: str
    deployment: str | None
    head_sha: str | None
    status: str
    failure_kind: str | None
    failure_detail: str | None
    wall_clock_s: float
    requests: int | None
    tool_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    final_step_output_tokens: int | None
    verdicts: dict[str, str] | None
    evidence: dict[str, Any] | None
    scan: dict[str, Any] | None
    seeded: list[str] | None
    caught: dict[str, bool] | None
    known_clean: bool
    would_route: bool | None
    expected: dict[str, str] | None
    expected_met: dict[str, bool] | None


def _row(
    source: _Source,
    *,
    deployment: str | None,
    status: str,
    wall_clock_s: float,
    known_clean: bool,
    head_sha: str | None = None,
    failure_kind: str | None = None,
    failure_detail: str | None = None,
    recorder: _FlightRecorder | None = None,
    verdicts: dict[str, str] | None = None,
    evidence: dict[str, Any] | None = None,
    scan: dict[str, Any] | None = None,
    seeded: tuple[str, ...] | None = None,
    caught: dict[str, bool] | None = None,
    would_route: bool | None = None,
    expected: dict[str, str] | None = None,
    expected_met: dict[str, bool] | None = None,
) -> EvalRow:
    """One report row. EVERY key is always present — a consumer greps a field name and
    gets every run, with null where a field could not apply."""
    return {
        "row_type": "run",
        "bundle_id": source.bundle_id,
        "source": source.kind,
        "origin": source.origin,
        "timestamp": datetime.now(UTC).isoformat(),
        "deployment": deployment,
        "head_sha": head_sha,
        "status": status,
        "failure_kind": failure_kind,
        # The ONE place a detail reaches the report file, so the redact-then-cap rule is
        # applied here rather than at each producer — same rule the production runner
        # applies before storing a detail (`service.py`), same ceiling. A model error can
        # quote the source it was reading, and this file outlives the run.
        "failure_detail": redact_and_cap(failure_detail, _DETAIL_MAX_CHARS),
        "wall_clock_s": round(wall_clock_s, 3),
        "requests": recorder.requests if recorder else None,
        "tool_calls": recorder.tool_calls if recorder else None,
        "input_tokens": recorder.input_tokens if recorder else None,
        "output_tokens": recorder.output_tokens if recorder else None,
        "cache_read_tokens": recorder.cache_read_tokens if recorder else None,
        "cache_write_tokens": recorder.cache_write_tokens if recorder else None,
        "final_step_output_tokens": (
            recorder.final_step_output_tokens if recorder and recorder.requests else None
        ),
        "verdicts": verdicts,
        "evidence": evidence,
        "scan": scan,
        "seeded": list(seeded) if seeded is not None else None,
        "caught": caught,
        "known_clean": known_clean,
        "would_route": would_route,
        "expected": expected,
        "expected_met": expected_met,
    }


def _would_route(verdicts: dict[str, str], config: LiveConfig) -> bool:
    """Whether the gate would route a current review with these answers and no owner changes:
    the gate's own decision, so the routing rate follows the live policy."""
    from src.services.deploy.gate import ReviewAtHead, decide

    review = ReviewAtHead(
        current=True,
        status="complete",
        failure_code=None,
        answers={key: verdict == "yes" for key, verdict in verdicts.items()},
        reasons={},
        checked_at=None,
    )
    decision = decide(config=config, review=review, owner_answers={}, rejection_standing=False)
    return decision.reason is not None


def _expected(source: _Source, config: LiveConfig) -> dict[str, str] | None:
    scenario = source.golden
    if scenario is None:
        return None
    expected = {entry.key: "no" for entry in config.classes} if scenario.every_class_no else {}
    return {**expected, **scenario.expect}


async def _evaluate_one(
    source: _Source,
    spec: _EvalSpec,
    *,
    config: LiveConfig | None,
    model_factory: ModelFactory | None,
    deployment: str | None,
    scan_only: bool,
    request_limit: int,
    run_timeout: float,
    sweep_root: Path,
) -> EvalRow:
    """Run one app end to end. NEVER raises for a per-app problem — an app that fails to
    extract (or a run that fails in the model) is a failure ROW, and the sweep moves on."""
    # Lazy on purpose — these modules' import chain resolves the full Settings (see
    # the import-block note at the top of the file).
    from src.services.classification.agent import OUTPUT_TOOL_NAME, run_review
    from src.services.classification.scan import scan_snapshot

    known_clean = source.bundle_id in spec.known_clean
    seeded = spec.seeded.get(source.bundle_id)
    scratch = sweep_root / f"run-{uuid.uuid4().hex[:12]}"
    await asyncio.to_thread(scratch.mkdir, parents=True)
    started = time.monotonic()
    try:
        try:
            head_sha, root = await _extract(source, scratch)
        except _EvalRunFailedError as failure:
            return _row(
                source,
                deployment=deployment,
                status="failed",
                wall_clock_s=time.monotonic() - started,
                known_clean=known_clean,
                failure_kind=failure.kind,
                failure_detail=failure.detail,
                seeded=seeded,
                would_route=None if scan_only else True,
            )

        try:
            sweep = await scan_snapshot(root)
        except Exception as exc:  # noqa: BLE001 — an unreadable tree is this bundle's
            # failure row, never the sweep's abort (BaseException still propagates).
            return _row(
                source,
                deployment=deployment,
                status="failed",
                wall_clock_s=time.monotonic() - started,
                known_clean=known_clean,
                head_sha=head_sha,
                failure_kind=f"unexpected:{type(exc).__name__}",
                failure_detail=str(exc),
                seeded=seeded,
                would_route=None if scan_only else True,
            )
        scan = _scan_doc(sweep)
        if scan_only:
            return _row(
                source,
                deployment=deployment,
                status="scan_only",
                wall_clock_s=time.monotonic() - started,
                known_clean=known_clean,
                head_sha=head_sha,
                scan=scan,
                seeded=seeded,
            )

        if model_factory is None or config is None:  # argument validation already prevents this
            raise SpecError("model runs requested but no model or configuration resolved")
        expected = _expected(source, config)
        recorder = _FlightRecorder(model_factory(), output_tool_name=OUTPUT_TOOL_NAME)
        failure_kind: str | None = None
        failure_detail: str | None = None
        output: ReviewOutput | None = None
        try:
            async with asyncio.timeout(run_timeout):
                result = await run_review(
                    model=recorder,
                    user_id=uuid.uuid4(),  # attribution-only; nothing persists it here
                    snapshot_root=root,
                    classes=config.classes,
                    scan_hits=sweep.hits,
                    usage_limits=UsageLimits(request_limit=request_limit),
                )
            output = result.output
        except TimeoutError:
            failure_kind = _RUN_TIMEOUT
            failure_detail = f"over the eval's --run-timeout ({run_timeout:.0f}s)"
        except _TruncatedError as exc:
            failure_kind = _OUTPUT_TRUNCATED
            failure_detail = str(exc)
        except UsageLimitExceeded as exc:
            failure_kind = _REQUEST_LIMIT
            failure_detail = str(exc)
        except UnexpectedModelBehavior as exc:
            failure_kind = _MODEL_ERROR
            failure_detail = str(exc)
        except ModelAPIError as exc:
            failure_kind = _MODEL_ERROR
            failure_detail = str(exc)
        except Exception as exc:  # noqa: BLE001 — a paid 30-bundle sweep must not abort
            # on run 29; anything unforeseen is a failure ROW with its type named, and
            # BaseException (Ctrl-C, cancellation) still propagates.
            failure_kind = f"unexpected:{type(exc).__name__}"
            failure_detail = str(exc)

        if output is None:
            return _row(
                source,
                deployment=deployment,
                status="failed",
                wall_clock_s=time.monotonic() - started,
                known_clean=known_clean,
                head_sha=head_sha,
                failure_kind=failure_kind,
                failure_detail=failure_detail,
                recorder=recorder,
                scan=scan,
                seeded=seeded,
                would_route=True,  # the gate routes every unfinished review
                expected=expected,
                expected_met=(None if expected is None else dict.fromkeys(expected, False)),
            )

        verdicts = {answer.key: answer.verdict.value for answer in output.answers}
        evidence = {
            answer.key: [{"path": ref.path, "kind": ref.kind} for ref in answer.evidence]
            for answer in output.answers
        }
        caught: dict[str, bool] | None = None
        if seeded is not None:
            caught = {category: verdicts.get(category) == "yes" for category in seeded}
        return _row(
            source,
            deployment=deployment,
            status="complete",
            wall_clock_s=time.monotonic() - started,
            known_clean=known_clean,
            head_sha=head_sha,
            recorder=recorder,
            verdicts=verdicts,
            evidence=evidence,
            scan=scan,
            seeded=seeded,
            caught=caught,
            would_route=_would_route(verdicts, config),
            expected=expected,
            expected_met=(
                None
                if expected is None
                else {key: verdicts.get(key) == answer for key, answer in expected.items()}
            ),
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)


# ---------------------------------------------------------------------------------------
# The summary — the named figures, and the distributions the ceilings are re-set from
# ---------------------------------------------------------------------------------------


def _dist(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), 1),
        "median": round(statistics.median(values), 1),
        "max": round(max(values), 1),
    }


def _summarize(rows: list[EvalRow], spec: _EvalSpec, deployment: str | None) -> dict[str, Any]:
    """The machine-readable summary row. The distributions here (wall-clock,
    requests, final-step output tokens) are the inputs that later re-set
    `REVIEW_WALL_CLOCK_CEILING_S`, `REVIEW_REQUEST_BUDGET` and the per-class output cap
    (`max_output_tokens`) in `src/services/classification/constants.py`. This script NEVER
    modifies those ceilings itself: bumping them in `service.py` happens after a real
    measured run against live Foundry, as its own reviewed change, and the ceilings
    belong to the deployment recorded here."""
    complete = [row for row in rows if row["status"] == "complete"]
    failed = [row for row in rows if row["status"] == "failed"]

    # The false-positive routing rate — the figure the ceilings are tuned
    # against: known-clean bundles the gate would route, run failures included.
    clean_rows = [row for row in rows if row["known_clean"]]
    clean_routed = [row for row in clean_rows if row["would_route"]]

    # The miss rate, over seeded findings a COMPLETED review judged. Seeded findings on
    # failed runs are not misses (a failed run routes to a human anyway) — they are
    # counted separately so nobody reads "0 missed" off a sweep where every run died.
    evaluated = 0
    missed = 0
    on_failed_runs = 0
    for row in rows:
        seeded = row["seeded"] or []
        if row["caught"] is not None:
            evaluated += len(seeded)
            missed += sum(1 for category in seeded if not row["caught"][category])
        elif seeded:
            on_failed_runs += len(seeded)

    # Tier A / Tier B precision-recall over the labeled corpus, micro-averaged on file
    # paths. Reported SEPARATELY: Tier A stands in when the model is down, Tier B is
    # only a lead, and blending them would hide the one number that gates the design.
    tier_stats = {tier: {"tp": 0, "fp": 0, "secrets_hit": 0} for tier in ("A", "B")}
    secrets_total = 0
    tier_a_false_paths: list[str] = []
    labeled_unscanned: list[str] = []
    for row in rows:
        labels = spec.scan_labels.get(row["bundle_id"])
        if labels is None:
            continue
        if row["scan"] is None:
            labeled_unscanned.append(row["bundle_id"])
            continue
        secrets_total += len(labels.secrets)
        for tier, paths_key in (("A", "tier_a_paths"), ("B", "tier_b_paths")):
            hit_paths = set(row["scan"][paths_key])
            tier_stats[tier]["tp"] += len(hit_paths & labels.secrets)
            tier_stats[tier]["fp"] += len(hit_paths - labels.secrets)
            tier_stats[tier]["secrets_hit"] += len(labels.secrets & hit_paths)
        for path in sorted(set(row["scan"]["tier_a_paths"]) - labels.secrets):
            tier_a_false_paths.append(f"{row['bundle_id']}:{path}")

    def _precision_recall(tier: str) -> dict[str, Any]:
        stats = tier_stats[tier]
        hits = stats["tp"] + stats["fp"]
        return {
            "hits": hits,
            "true_positives": stats["tp"],
            "false_positives": stats["fp"],
            "precision": round(stats["tp"] / hits, 4) if hits else None,
            "recall": round(stats["secrets_hit"] / secrets_total, 4) if secrets_total else None,
        }

    golden = [row for row in rows if row["expected_met"] is not None]
    golden_failures = [
        {
            "scenario": row["bundle_id"],
            "class": key,
            "expected": (row["expected"] or {})[key],
            "answered": (row["verdicts"] or {}).get(key),
        }
        for row in golden
        for key, met in (row["expected_met"] or {}).items()
        if not met
    ]

    tier_a = _precision_recall("A")
    if not spec.scan_labels:
        gate = "no-labeled-corpus"
    else:
        # 100% precision required: a Tier A false positive becomes a verdict nobody
        # reviewed. Zero hits passes vacuously — the hit count above keeps that visible.
        gate = "pass" if tier_a["false_positives"] == 0 else "fail"

    return {
        "row_type": "summary",
        "timestamp": datetime.now(UTC).isoformat(),
        "deployment": deployment,
        "runs": len(rows),
        "complete": len(complete),
        "failed": len(failed),
        "scan_only": sum(1 for row in rows if row["status"] == "scan_only"),
        "known_clean_total": len(clean_rows),
        "known_clean_routed": len(clean_routed),
        "false_positive_routing_rate": (
            round(len(clean_routed) / len(clean_rows), 4) if clean_rows else None
        ),
        "seeded_findings_evaluated": evaluated,
        "seeded_findings_missed": missed,
        "seeded_findings_on_failed_runs": on_failed_runs,
        "miss_rate": round(missed / evaluated, 4) if evaluated else None,
        "tier_a": tier_a,
        "tier_b": _precision_recall("B"),
        "tier_a_precision_gate": gate,
        "tier_a_false_positive_paths": tier_a_false_paths,
        "labeled_bundles_unscanned": labeled_unscanned,
        "golden_total": len(golden),
        "golden_passed": sum(1 for row in golden if all((row["expected_met"] or {}).values())),
        "golden_failures": golden_failures,
        "wall_clock_s": _dist([row["wall_clock_s"] for row in complete]),
        "requests": _dist([float(row["requests"]) for row in complete if row["requests"]]),
        "tool_calls": _dist(
            [float(row["tool_calls"]) for row in complete if row["tool_calls"] is not None]
        ),
        "final_step_output_tokens": _dist(
            [
                float(row["final_step_output_tokens"])
                for row in complete
                if row["final_step_output_tokens"] is not None
            ]
        ),
    }


def _human_summary(summary: dict[str, Any]) -> str:
    def _rate(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    def _spread(entry: dict[str, float] | None) -> str:
        if entry is None:
            return "n/a"
        return f"min {entry['min']} / median {entry['median']} / max {entry['max']}"

    def _tier_line(name: str, tier: dict[str, Any]) -> str:
        precision = "n/a" if tier["precision"] is None else f"{tier['precision']:.1%}"
        recall = "n/a" if tier["recall"] is None else f"{tier['recall']:.1%}"
        return (
            f"  Tier {name}: precision {precision}, recall {recall} "
            f"({tier['hits']} hit path(s), {tier['false_positives']} false)"
        )

    lines = [
        "=== classification-review evaluation ===",
        f"deployment: {summary['deployment'] or 'n/a (scan-only)'}",
        f"runs: {summary['runs']} ({summary['complete']} complete, "
        f"{summary['failed']} failed, {summary['scan_only']} scan-only)",
        "",
        "-- the two named figures --",
        f"false-positive routing rate: {_rate(summary['false_positive_routing_rate'])} "
        f"({summary['known_clean_routed']}/{summary['known_clean_total']} known-clean "
        "bundles the gate would route, run failures included)",
        f"miss rate: {_rate(summary['miss_rate'])} "
        f"({summary['seeded_findings_missed']}/{summary['seeded_findings_evaluated']} seeded "
        f"findings missed; {summary['seeded_findings_on_failed_runs']} on failed runs, "
        "routed regardless)",
        "",
        "-- scan precision/recall (labeled corpus) --",
        _tier_line("A", summary["tier_a"]),
        _tier_line("B", summary["tier_b"]),
        f"  TIER A PRECISION GATE (must be 100%): {summary['tier_a_precision_gate'].upper()}",
    ]
    if summary["tier_a_false_positive_paths"]:
        lines.append(
            "  Tier A false positives (narrow Tier A — a later decision, not this run's):"
        )
        lines.extend(f"    {path}" for path in summary["tier_a_false_positive_paths"])
    if summary["labeled_bundles_unscanned"]:
        lines.append(
            "  labeled but never scanned (extraction failed): "
            + ", ".join(summary["labeled_bundles_unscanned"])
        )
    if summary["golden_total"]:
        lines += [
            "",
            f"-- golden scenarios: {summary['golden_passed']}/{summary['golden_total']} pass --",
        ]
        lines.extend(
            f"  MISS {failure['scenario']}: {failure['class']} expected "
            f"{failure['expected']}, answered {failure['answered']}"
            for failure in summary["golden_failures"]
        )
    lines += [
        "",
        "-- distributions the ceilings are re-set from (this script changes NO ceilings;",
        "   constants.py is modified separately, after a real measured run) --",
        f"wall-clock (s):            {_spread(summary['wall_clock_s'])}",
        f"model requests:            {_spread(summary['requests'])}",
        f"tool calls:                {_spread(summary['tool_calls'])}",
        f"final-step output tokens:  {_spread(summary['final_step_output_tokens'])}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------------------


def _default_model_factory() -> ModelFactory:
    """The real thing: the Foundry deployment the platform is configured with, resolved
    through the app's own settings (so `ENV_FILE=.env` behaves exactly as it does for
    the server). Fails BEFORE any extraction or spend when Foundry is unconfigured."""
    from src.config import settings
    from src.services.agent.model import build_foundry_model

    foundry = settings.foundry
    if foundry is None:
        raise SpecError(
            "no Foundry deployment is configured (FOUNDRY__* env) — model runs need "
            "one; use --scan-only for a model-free sweep"
        )
    return lambda: build_foundry_model(foundry)


async def _default_config() -> LiveConfig:
    """The live class configuration, read from the database the backend env points at."""
    from src.db.base import async_session_factory
    from src.services.classification.config import load_live_config

    async with async_session_factory() as db:
        return await load_live_config(db)


async def _run_sweep(
    spec: _EvalSpec,
    *,
    model_factory: ModelFactory | None,
    config: LiveConfig | None,
    out_path: Path,
    scan_only: bool,
    request_limit: int,
    run_timeout: float,
) -> list[EvalRow]:
    deployment: str | None = None
    if not scan_only:
        if config is None:
            config = await _default_config()
        _check_seeded(spec.seeded, [entry.key for entry in config.classes])
        if model_factory is None:
            model_factory = _default_model_factory()
        # Resolve the deployment label once, up front — every row records the
        # deployment it ran on, because ceilings measured on one model do not
        # transfer to another (the "Build on Opus now" decision's re-run clause).
        deployment = model_factory().model_name

    sweep_root = Path(tempfile.mkdtemp(prefix="bial-review-eval-"))
    rows: list[EvalRow] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_path.open("w", encoding="utf-8") as out:
            for source in spec.sources:
                row = await _evaluate_one(
                    source,
                    spec,
                    config=config,
                    model_factory=None if scan_only else model_factory,
                    deployment=deployment,
                    scan_only=scan_only,
                    request_limit=request_limit,
                    run_timeout=run_timeout,
                    sweep_root=sweep_root,
                )
                rows.append(row)
                out.write(json.dumps(row) + "\n")
                out.flush()  # a crash mid-sweep keeps every paid row already written
                print(
                    f"[{len(rows)}/{len(spec.sources)}] {source.bundle_id}: {row['status']}"
                    + (f" ({row['failure_kind']})" if row["failure_kind"] else ""),
                    file=sys.stderr,
                )
            summary = _summarize(rows, spec, deployment)
            out.write(json.dumps(summary) + "\n")
        print(_human_summary(summary))
    finally:
        await asyncio.to_thread(shutil.rmtree, sweep_root, ignore_errors=True)
    return rows


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_classification_review",
        description=(
            "Measure the classification review against the live class configuration, over "
            "a corpus of saved app bundles or the built-in golden scenarios: budgets "
            "(wall-clock, requests, tokens, the final step's output tokens), answer accuracy "
            "against seeded/known-clean manifests or the golden expectations, and the "
            "credential scan's Tier A/B precision-recall against a labeled corpus."
        ),
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        action="append",
        default=[],
        help="a local .bundle file (repeatable)",
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        action="append",
        default=[],
        help="a directory; every *.bundle inside is evaluated (repeatable)",
    )
    parser.add_argument(
        "--app-id",
        type=uuid.UUID,
        action="append",
        default=[],
        help="an app id whose bundle is pulled from object storage (repeatable; "
        "needs the backend env loaded)",
    )
    parser.add_argument(
        "--golden",
        action="store_true",
        help="run the built-in golden scenarios instead of bundles, each checked against "
        "the answers the seeded class descriptions must produce",
    )
    parser.add_argument(
        "--seeded",
        type=Path,
        default=None,
        help="JSON manifest: {bundle-id: [class key, ...]} — catch/miss is reported per "
        "seeded finding",
    )
    parser.add_argument(
        "--known-clean",
        type=Path,
        default=None,
        help="JSON list of bundle ids the gate should publish — the false-positive routing "
        "rate is measured over these",
    )
    parser.add_argument(
        "--scan-labels",
        type=Path,
        default=None,
        help="JSON manifest: {bundle-id: {secrets: [path...], credential_shaped: "
        "[path...]}} — Tier A/B precision-recall is measured against it",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="JSONL report path (run rows + a summary row)"
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="run only the credential scan — no model, no Foundry config, no spend",
    )
    parser.add_argument(
        "--request-limit",
        type=int,
        default=50,
        help="the eval's per-run model-request bound (default 50 — deliberately above "
        "the service's provisional budget, so the measurement is not censored by "
        "the ceiling it exists to set)",
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=600.0,
        help="per-run wall-clock bound in seconds (default 600); exceeding it is a "
        "failure row, never a hung sweep",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    model_factory: ModelFactory | None = None,
    config: LiveConfig | None = None,
) -> int:
    """Entry point. `model_factory` and `config` are the test seams: tests inject a scripted
    (FunctionModel) factory and a configuration, so no real Foundry or database is used; the
    defaults resolve the platform's Foundry deployment and the live configuration."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.request_limit < 1:
        parser.error("--request-limit must be at least 1")
    if args.run_timeout <= 0:
        parser.error("--run-timeout must be positive")
    try:
        spec = _build_spec(args)
        asyncio.run(
            _run_sweep(
                spec,
                model_factory=model_factory,
                config=config,
                out_path=args.out,
                scan_only=args.scan_only,
                request_limit=args.request_limit,
                run_timeout=args.run_timeout,
            )
        )
    except SpecError as exc:
        parser.error(str(exc))  # exits 2, with usage
    return 0


if __name__ == "__main__":
    sys.exit(main())
