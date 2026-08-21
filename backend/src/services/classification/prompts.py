"""Prompt assembly for the classification review (U5, R3/R5, P8).

THE ORDERING IS THE COST MODEL. `REVIEW_INSTRUCTIONS` — the six-question rubric, the
output discipline and the scan-verification protocol — is BYTE-IDENTICAL across every
review of every app, composed once at import from static sources. It rides the agent's
`instructions`, which sit behind the `anthropic_cache_instructions` breakpoint (with the
tool definitions behind their own), so the static prefix is a shared cache hit
platform-wide. Everything app-specific — the file listing, the scan's hits — is VOLATILE
and goes in the per-run user prompt via `build_review_prompt`. Anything app-specific
placed above the breakpoints would destroy the platform-wide hit; a test pins the split.

THE SCAN'S HITS ARE DIRECTED EVIDENCE, NEVER VALUES (P8). Each hit is a location and a
pattern family — `CredentialHit` structurally cannot carry the matched value, because
putting the secret in the prompt is the exact leak the plain-language rule exists to
prevent. The instruction is to VERIFY each hit and to look for what the scan would have
missed: handing the model a named location to check is harder to talk it out of than
letting it hunt unaided, and confirmation is cheaper than discovery (the measured
54-57s runs were an agent discovering an empty tree by itself).

Explanations are kept plain by PROMPTING, not by a validator — the evidence fields carry
the integrity load, and U6 additionally passes every reason through the shared redactor
before it is stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from src.core.redaction import CredentialHit, Tier
from src.services.classification.constants import LISTING_MAX_FILES
from src.services.deploy.classification import DATA_CLASSIFICATION_QUESTIONS

# What counts as a Yes, per question — the rubric's substance. Indexed (not `.get`) so a
# questionnaire key added in `deploy/classification.py` without guidance here fails at
# IMPORT, in every environment, rather than shipping a rubric with a silent hole.
_QUESTION_GUIDANCE: Final[dict[str, str]] = {
    "credentials_secrets": (
        "the code contains an actual secret VALUE — a hardcoded password, API key, "
        "token, private key, or a connection string / URL carrying a credential. A "
        "form field, validation schema, or database column merely NAMED `password` is "
        "not a secret, and reading a value from the environment is not hardcoding one."
    ),
    "health_data": (
        "the app collects, stores, or displays medical or health information about "
        "people — symptoms, diagnoses, medications, treatments, appointments, fitness "
        "or medical records."
    ),
    "personal_information": (
        "the app handles personally identifiable information — names paired with "
        "contact details, email addresses, phone numbers, home addresses, government "
        "or employee identifiers of real people."
    ),
    "financial_data": (
        "the app handles money-related records — payment details, bank information, "
        "salaries, invoices, budgets, expenses, or transaction histories."
    ),
    "confidential_business_data": (
        "the app handles internal business information not meant for the public — "
        "internal metrics or reports, contracts, vendor terms, pricing, strategy, or "
        "organisational data."
    ),
    "public_data": (
        "the app handles only information that is already public or carries no "
        "sensitivity — reference lists, public schedules, published content."
    ),
}


def _rubric() -> str:
    """The six questions, one line each, keyed and labelled from the single source."""
    return "\n".join(
        f"- `{key}` — {label}. Answer yes when {_QUESTION_GUIDANCE[key]}"
        for key, label, _weight in DATA_CLASSIFICATION_QUESTIONS
    )


# Static, byte-identical on every run of every app — see the module docstring before
# adding ANYTHING app-specific here.
REVIEW_INSTRUCTIONS: Final[str] = f"""\
You are the pre-publish data-classification reviewer for an internal app platform. An
employee described an app in plain language and the platform built it; before it can be
published you review its SAVED SOURCE CODE — reachable only through your read-only tools
— and answer six questions about what data the app handles: what it collects, stores,
displays, or has written into its code. You review code only; you cannot see any records
the app may have stored, and you never modify anything.

THE SIX QUESTIONS:
{_rubric()}

HOW TO WORK:
- Ground every verdict in code you actually read. Read the files that matter — pages,
  API routes, schemas, configuration — before concluding.
- The task message lists the findings of a deterministic credential scan as directed
  evidence for `credentials_secrets`: VERIFY each cited location by reading it, decide
  whether it is a real hardcoded secret or a false positive, and then look for what a
  pattern scan would have missed. Your verdict is the answer, including when it
  disagrees with the scan.
- Answer only where you have evidence. A question the code gives you no basis to answer
  is `unanswered` — never a guess in either direction. An unanswered question is put to
  a person instead, which is the correct outcome.

THE OUTPUT DISCIPLINE — for each question, produce its fields strictly in order:
1. `evidence` first: the locations that ground the verdict, as workspace-relative paths
   with a short `kind` label. Internal only — no person ever reads these. A `yes` must
   cite at least one real location; a location that does not exist invalidates it.
2. `reason` second: one or two sentences for a NON-TECHNICAL reader. No file names, no
   paths, no code, no identifiers, and NEVER the value of anything you found — describe
   what kind of data is involved and where it comes from in plain terms (for example:
   "The app's sign-in page stores a fixed password inside the code itself.").
3. `verdict` last: `yes`, `no`, or `unanswered` — the conclusion your evidence and
   reason already support.
Set `agreed_with_scan` only on questions the scan findings addressed: true when your
verdict is consistent with them, false when you overrule them. Set `completeness` to
`complete` when every question was examined as far as the evidence allows (honest
`unanswered` verdicts still count); `partial` only when you know the review is cut
short. When you are done, record the review with the output tool — exactly once."""


@dataclass(frozen=True)
class LocatedHit:
    """One scan hit tied to the file it was found in — the shape U6 hands the prompt
    builder. Pairs the path with the `CredentialHit` (family / tier / line), which
    structurally carries no matched value."""

    path: str
    hit: CredentialHit


_TIER_NOTES: Final[dict[Tier, str]] = {
    Tier.A: "high-confidence match",
    Tier.B: "possible lead — this family is often a false positive",
}


def format_scan_hits(scan_hits: Sequence[LocatedHit]) -> str:
    """The scan-findings section of the volatile prompt: location + pattern family per
    hit, NEVER a value (the hit shape has nowhere to carry one). No hits is stated as a
    signal, not an answer — the model still checks for what the families do not cover."""
    if not scan_hits:
        return (
            "The credential scan found no hits. That is a signal, not an answer — "
            "still check for secrets its pattern families would not cover."
        )
    lines = "\n".join(
        f"- `{located.path}` line {located.hit.line}: pattern family "
        f"`{located.hit.family}` ({_TIER_NOTES[located.hit.tier]})"
        for located in scan_hits
    )
    return f"The credential scan flagged these locations:\n{lines}"


def build_review_prompt(*, files: Sequence[str], scan_hits: Sequence[LocatedHit]) -> str:
    """The volatile, per-run user prompt: the app's file listing and the scan's hits —
    everything app-specific, kept BELOW the cache breakpoints (see the module
    docstring). The listing is capped with an explicit marker; the model holds
    `list_files` for the remainder."""
    shown = list(files[:LISTING_MAX_FILES])
    listing = "\n".join(shown) if shown else "(the app has no files)"
    if len(files) > len(shown):
        listing += f"\n[... {len(files) - len(shown)} more files — use list_files ...]"
    return (
        "Review the app whose files are listed below.\n\n"
        f"FILES:\n{listing}\n\n"
        f"SCAN FINDINGS:\n{format_scan_hits(scan_hits)}\n\n"
        "Verify each scan finding at its cited location, look for what the scan would "
        "have missed, examine the app for the other five questions, and record the "
        "structured review."
    )
