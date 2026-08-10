"""The data-classification questionnaire that gates one-click deploy.

The citizen answers six yes/no questions about what data their app handles, the answers
are scored server-side, and a total AT OR BELOW `AUTO_DEPLOY_MAX_SCORE` proceeds to
deploy with no human in the loop. Above it the deploy is refused (routed to a human
review, out of band) and the citizen is told why.

FIXED post-#104 (issue #115): the gate previously ran the other way — `>= 50` auto-
deployed, so an app that HONESTLY declared it handled Credentials + Confidential
Business data (score 55) published to a live URL with zero review, while an app
declaring nothing sensitive (score 0) was refused. `refusal_message()` compounded it by
coaching the citizen toward declaring MORE sensitive categories to get published. The
weights are sensitivity values (see below) — a safety gate must let the LOW-sensitivity
case through automatically and route the HIGH-sensitivity case to a human, not the
reverse.

WHY THE GATE LIVES IN THE DEPLOY LINEAGE AND NOT IN `app_registry`. The questionnaire is
a precondition on ONE action — publishing — so it belongs beside the thing it gates. The
admin `submit`/`approve`/`reject` surface is a separate lineage with a human in it and is
deliberately untouched; folding the score into that path instead would put an automatic
decision inside a workflow whose entire purpose is a human decision.

WHY THE SCORE IS COMPUTED HERE AND NOWHERE ELSE. There is deliberately NO "score my
answers" endpoint. A gate a client can decline to call is not a gate — it would be
advisory decoration, bypassable by any caller that skipped straight to deploy. The answers
therefore ride in the deploy request body and are scored inside the same request that
publishes, so passing the gate and being deployed are the same event. A portal may
recompute the total locally to drive its own affordances (enabling Confirm, prompting for
an explanation); that copy is a convenience and is never the decision.

THE TABLE AND THE THRESHOLD ARE ONE POLICY UNIT. `AUTO_DEPLOY_MAX_SCORE` is meaningless without
the weights it is compared against — moving one into configuration and leaving the other
in code would let them drift into a combination nobody chose (a threshold no combination
of answers can reach, or one every answer clears). They change together, in review, in
this file.
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

# At or above this total the explanation box stops being optional. Either of the top two
# categories reaches it alone, which is the intent: an app touching credentials or health
# data must say what it does with them.
NOTES_REQUIRED_AT = 25

# AT OR BELOW this total the deploy proceeds automatically — set to 0 (issue #115): ANY
# weighted category answered Yes routes to a human, deliberately, not a graduated scale.
# "Nothing sensitive declared" is the one shape of answer set safe enough to publish with
# no one looking at it; every other combination needs a person, however small the total.
# DELIBERATELY INDEPENDENT of `NOTES_REQUIRED_AT`: a declaration can cross the notes gate
# (must explain itself) while still being well above this one too — Personal Information +
# Financial Data (40) requires an explanation AND needs a human to review it. Do not
# collapse the two constants or assume one implies the other.
AUTO_DEPLOY_MAX_SCORE = 0

# Every key in `DATA_CLASSIFICATION_QUESTIONS`, for callers that need to validate or
# project a persisted answer set without re-walking the tuple.
CLASSIFICATION_KEYS: tuple[str, ...] = tuple(
    key for key, _label, _weight in DATA_CLASSIFICATION_QUESTIONS
)


def total_weight(flags: Mapping[str, bool]) -> int:
    """Sum the weights of the categories answered Yes.

    Takes a plain mapping rather than the request schema on purpose: this module is the
    policy, and a service-layer policy that imports an API schema inverts the dependency
    and cannot then be reused by the persistence or reporting paths. Callers pass
    `answers.classification_flags()`.

    A key the mapping omits counts as No. That is the honest reading for a stored answer
    set written before a question existed — the alternative, raising, would make an old
    deployment row unreadable the moment the questionnaire grows.
    """
    return sum(weight for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS if flags.get(key))


def notes_required(flags: Mapping[str, bool]) -> bool:
    """Whether this answer set obliges an explanation."""
    return total_weight(flags) >= NOTES_REQUIRED_AT


def qualifies_for_deploy(flags: Mapping[str, bool]) -> bool:
    """Whether this answer set clears the automatic-deploy threshold — i.e. is safe
    enough to publish with no human review."""
    return total_weight(flags) <= AUTO_DEPLOY_MAX_SCORE


def declared_categories(flags: Mapping[str, bool]) -> tuple[str, ...]:
    """The labels of the weighted categories answered Yes, most significant first.

    This is the actionable half of a refusal (issue #115): the citizen cannot act on a
    bare number, but they can look at the categories their app WAS declared to handle and
    tell whether that's actually right, or whether they over-answered by mistake.
    Zero-weight categories are omitted — `Public Data` never moves the score either way,
    so listing it would be noise presented as advice.
    """
    return tuple(
        label for key, label, weight in DATA_CLASSIFICATION_QUESTIONS if weight and flags.get(key)
    )


def refusal_message(flags: Mapping[str, bool]) -> str:
    """The sentence the citizen reads when the gate refuses their deploy.

    Names the score and what was declared, because "your deploy was refused" with no
    detail is un-actionable and generates a support ticket every time. Unlike the pre-#115
    wording, this does NOT invite the citizen to change their answers to get published —
    an app that legitimately handles sensitive data needing a human review is the correct
    outcome, not a puzzle to route around. "Adjust your answers" is offered only for the
    case where they were over-cautious/mistaken, alongside the real path (an admin looks
    at it), not as the primary instruction.
    """
    score = total_weight(flags)
    declared = declared_categories(flags)
    detail = f" Declared: {', '.join(declared)}." if declared else ""
    return (
        f"This app scored {score} on the data-classification questions and needs a "
        f"person to review it before it can publish.{detail} An administrator will review "
        "it, or you can revisit your answers if this wasn't what you meant to declare."
    )
