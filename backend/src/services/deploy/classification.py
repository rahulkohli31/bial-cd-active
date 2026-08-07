"""The data-classification questionnaire that gates one-click deploy.

The citizen answers six yes/no questions about what data their app handles, the answers
are scored server-side, and a total AT OR ABOVE `AUTO_DEPLOY_AT` proceeds to deploy with
no human in the loop. Below it the deploy is refused and the citizen is told why.

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

THE TABLE AND THE THRESHOLD ARE ONE POLICY UNIT. `AUTO_DEPLOY_AT` is meaningless without
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

# At or above this total the deploy proceeds automatically. DELIBERATELY INDEPENDENT of
# `NOTES_REQUIRED_AT`: a declaration can cross the notes gate (must explain itself) and
# still fall short of this one — Personal Information + Financial Data (40) requires an
# explanation and is still refused. Do not collapse the two constants or assume one
# implies the other.
AUTO_DEPLOY_AT = 50

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
    """Whether this answer set clears the automatic-deploy threshold."""
    return total_weight(flags) >= AUTO_DEPLOY_AT


def declined_categories(flags: Mapping[str, bool]) -> tuple[str, ...]:
    """The labels of the weighted categories answered No, most significant first.

    This is the actionable half of a refusal: the citizen cannot act on a bare number, but
    they can look at the categories their app was not declared to handle and tell whether
    they answered too conservatively. Zero-weight categories are omitted — `Public Data`
    could never have changed the outcome, so listing it would be noise presented as advice.
    """
    return tuple(
        label
        for key, label, weight in DATA_CLASSIFICATION_QUESTIONS
        if weight and not flags.get(key)
    )


def refusal_message(flags: Mapping[str, bool]) -> str:
    """The sentence the citizen reads when the gate refuses their deploy.

    Names the score, the threshold, and what is missing, because "your deploy was refused"
    with no number is un-actionable and generates a support ticket every time. The wording
    stays neutral about WHY the answers were what they were — the platform knows what was
    declared, not whether it was declared correctly.
    """
    score = total_weight(flags)
    missing = declined_categories(flags)
    detail = f" Not declared: {', '.join(missing)}." if missing else ""
    return (
        f"This app scored {score} on the data-classification questions and needs "
        f"{AUTO_DEPLOY_AT} to deploy automatically.{detail} Review your answers and try "
        "again, or ask an administrator to review the app instead."
    )
