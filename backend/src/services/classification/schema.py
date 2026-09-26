"""The review's structured output: one Yes or No per class, evidence first.

Field order is load-bearing and pinned by test. Per class the schema is evidence → reason →
verdict, in the order pydantic declares it and the JSON schema preserves: the model cites what it
found, explains it, and only then concludes. Verdict-first would yield a justification written
after the fact.

The model is fixed across every configuration, so the output tool's definition stays
byte-identical and cached. Which keys a run must answer is the agent's output validator's job,
against the classes that run was given. Evidence is internal only; `reason` is the only text a
person reads.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Verdict(StrEnum):
    """One class's answer. There is no third value: a class the code gives no sign of is a No."""

    YES = "yes"
    NO = "no"


class EvidenceRef(BaseModel):
    """One machine-checkable location backing a verdict. Deliberately nowhere to carry a found
    value: evidence that quoted its secret would leak it into stored records."""

    path: str = Field(
        max_length=500,
        description=(
            "Workspace-relative path of the file the finding is in, exactly as listed "
            "by the tools (e.g. `app/lib/db.ts`)."
        ),
    )
    kind: str = Field(
        max_length=100,
        description=(
            "Short machine label for WHAT is at that path — e.g. `hardcoded-value`, "
            "`form-field`, `api-route`, `schema-column`. Never the content itself."
        ),
    )


class ClassAnswer(BaseModel):
    """One class's finding — evidence first, then the reason, then the verdict."""

    key: str = Field(
        max_length=64,
        description="The key of the class this answers, exactly as its definition gives it.",
    )
    evidence: list[EvidenceRef] = Field(
        description=(
            "The locations backing this answer, cited BEFORE reasoning. A Yes cites at least "
            "one real location; a No may cite none."
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "The explanation, written AFTER the evidence and BEFORE the verdict, for a "
            "non-technical reader: no file names, no paths, no code, no identifiers, "
            "and never the value of anything found."
        ),
    )
    verdict: Verdict = Field(
        description="The conclusion, stated LAST: `yes` or `no`.",
    )


class ReviewOutput(BaseModel):
    """The whole review: one answer per class definition."""

    answers: list[ClassAnswer] = Field(
        description="One answer per class definition — every class, exactly once.",
    )
