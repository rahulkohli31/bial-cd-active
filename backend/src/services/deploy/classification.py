"""The data-classification policy table that gates one-click deploy.

Six yes/no questions about what data an app handles, each with a sensitivity weight. A
declaration whose weighted total is AT OR BELOW `AUTO_DEPLOY_MAX_SCORE` auto-publishes;
anything above needs a person. The publish request merges the citizen's declaration with
the platform's own stored review of the saved code (stricter-of per question); a weighted
Yes on the MERGED set ROUTES the app into the admin approve queue — a real queue entry, not
a refusal. This module is the table, the threshold, and the pure scoring functions both
readers read weights from.

WHY THIS EXISTS: the gate used to run the other way — `>= 50` auto-deployed, so an app that
HONESTLY declared Credentials + Confidential Business data (score 55) published live with
zero review, while a clean declaration (score 0) was refused. A since-retired
`refusal_message()` compounded it by coaching citizens toward MORE sensitive answers to get
published. The weights are sensitivity values: a safety gate must let LOW-sensitivity
through automatically and route HIGH-sensitivity to a human — never the reverse.

The gate runs inside the deploy route as a precedence ladder (`api/v1/deploy/router.py`): a
weighted merged Yes routes into the admin queue via the approvals submit service. Scoring is
SERVER-SIDE ONLY — no "score my answers" endpoint — so it cannot be skipped by a caller that
goes straight to deploy; the schema carries no review field, and the gate reads the stored
review by app+version. The table and the threshold are one policy unit, changed together in
this file — split across config and code, they could drift into a combination nobody chose.
"""

from __future__ import annotations

from collections.abc import Mapping

# The questionnaire, in the order the citizen sees it. `(key, label, weight)` — `key` is
# the field name on the request schema AND the JSONB key persisted on the deployment row,
# so the three never drift apart.
#
# The weights are sensitivity values: the more sensitive the data an app declares, the
# higher its total. `Public Data` is deliberately weighted 0 — it is a real answer that
# adds nothing, not a filler option.
DATA_CLASSIFICATION_QUESTIONS: tuple[tuple[str, str, int], ...] = (
    ("credentials_secrets", "Credentials / Secrets", 40),
    ("health_data", "Health Data", 25),
    ("personal_information", "Personal Information (PII)", 20),
    ("financial_data", "Financial Data", 20),
    ("confidential_business_data", "Confidential Business Data", 15),
    ("public_data", "Public Data", 0),
)

# AT OR BELOW this total the deploy proceeds automatically — set to 0: ANY
# weighted category answered Yes routes to a human, deliberately, not a graduated scale.
# "Nothing sensitive declared" is the one shape of answer set safe enough to publish with
# no one looking at it; every other combination needs a person, however small the total.
#
# TIED to `notes_required()`: every declaration that fails this
# gate is now ALSO obliged to explain itself — there is no longer a band that is refused
# but never asked why, nor one that must explain itself but is not refused. Before this,
# `NOTES_REQUIRED_AT` (25) sat strictly inside the refused region: an explanation could be
# compelled on a declaration that was going to be refused anyway, and the refusal path threw
# it away unread. Tying the two closes both gaps in one move rather than moving one
# threshold and leaving the other stranded.
AUTO_DEPLOY_MAX_SCORE = 0

# Every key in `DATA_CLASSIFICATION_QUESTIONS`, for callers that need to validate or
# project a persisted answer set without re-walking the tuple.
CLASSIFICATION_KEYS: tuple[str, ...] = tuple(
    key for key, _label, _weight in DATA_CLASSIFICATION_QUESTIONS
)


def total_weight(flags: Mapping[str, bool]) -> int:
    """Sum the weights of the categories answered Yes. Takes a plain mapping, not the request
    schema: this module is the policy, and a service-layer policy importing an API schema
    inverts the dependency, blocking reuse by persistence/reporting paths. Callers pass
    `answers.classification_flags()`.

    A key the mapping omits counts as No — the honest reading for a stored answer set written
    before a question existed; raising would make an old deployment row unreadable the moment
    the questionnaire grows."""
    return sum(weight for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS if flags.get(key))


def notes_required(flags: Mapping[str, bool]) -> bool:
    """Whether this answer set obliges an explanation — exactly the declarations that
    fail `qualifies_for_deploy`, so a refusal is never left unexplained and an
    explanation is never compelled and then discarded on a declaration that wasn't
    going to be refused."""
    return total_weight(flags) > AUTO_DEPLOY_MAX_SCORE


def qualifies_for_deploy(flags: Mapping[str, bool]) -> bool:
    """Whether this answer set clears the automatic-deploy threshold — safe enough to
    publish with no human review.

    Rejects an INCOMPLETE mapping outright rather than scoring it: `total_weight`'s
    per-key omission tolerance is for reading an old stored answer set, not for letting a
    partial declaration through — a mapping missing every key scores 0 and would otherwise
    silently qualify for auto-deploy, the original scoring bug's fail-open shape."""
    if any(key not in flags for key in CLASSIFICATION_KEYS):
        raise ValueError("incomplete declaration cannot be scored for auto-deploy")
    return total_weight(flags) <= AUTO_DEPLOY_MAX_SCORE


def declared_categories(flags: Mapping[str, bool]) -> tuple[str, ...]:
    """The labels of the weighted categories answered Yes, most significant first. Lets the
    citizen see which categories an answer set DID declare and check whether that's right,
    or whether they over-answered. Zero-weight categories are omitted — `Public Data` never
    moves the score, so listing it would be noise as advice.

    `refusal_message`, this projection's original consumer, was retired with the terminal
    refusal it explained (a weighted Yes now ROUTES to the admin queue)."""
    return tuple(
        label for key, label, weight in DATA_CLASSIFICATION_QUESTIONS if weight and flags.get(key)
    )
