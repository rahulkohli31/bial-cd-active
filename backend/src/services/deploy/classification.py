"""The data-classification policy table that gates one-click deploy.

Six yes/no questions about what data an app handles, each with a sensitivity weight. A
declaration whose weighted total is AT OR BELOW `AUTO_DEPLOY_MAX_SCORE` is safe enough
to publish with no human in the loop; anything above it needs a person. Since the
pre-publish review (U9) the answer set the gate scores is no longer the citizen's alone:
the publish request merges the citizen's declaration with the platform's own stored
review of the saved code (`services/classification/merge`, stricter-of per question) and
a weighted Yes on the MERGED set ROUTES the app into the admin approve queue — a real
queue entry an administrator will see, not a refusal and not an out-of-band "ask
someone". This module stays what it always was: the table, the threshold, and the pure
scoring functions both the gate and the merge read their weights from.

FIXED post-#104 (issue #115): the gate previously ran the other way — `>= 50` auto-
deployed, so an app that HONESTLY declared it handled Credentials + Confidential
Business data (score 55) published to a live URL with zero review, while an app
declaring nothing sensitive (score 0) was refused. A since-retired `refusal_message()`
compounded it by coaching the citizen toward declaring MORE sensitive categories to get
published (it was retired with the terminal refusal it explained, in U9 — a routed app
gets a queue entry, so a sentence promising nothing would happen next stopped being
true). The weights are sensitivity values (see below) — a safety gate must let the
LOW-sensitivity case through automatically and route the HIGH-sensitivity case to a
human, not the reverse.

WHERE THE GATE LIVES, AND WHERE THE TWO LINEAGES NOW JOIN. The questionnaire is a
precondition on ONE action — publishing — so the policy belongs beside the thing it
gates, and the gate itself runs inside the deploy route as a precedence ladder
(`api/v1/deploy/router.py`). An earlier revision of this docstring said the admin
`submit`/`approve`/`reject` surface was "deliberately untouched"; that stopped being
true in U9 and the sentence was rewritten rather than left to mislead: the ladder ROUTES
a weighted merged Yes into that surface through the approvals submit service (the one
route into the queue, R15a), and an administrator's approval of the exact shipping
version is what satisfies the gate on the next publish (R17). The automatic decision
still lives here; the human decision still lives there; the ladder is the seam where one
hands the app to the other.

WHY THE SCORE IS COMPUTED SERVER-SIDE AND NOWHERE ELSE. There is deliberately NO "score
my answers" endpoint. A gate a client can decline to call is not a gate — it would be
advisory decoration, bypassable by any caller that skipped straight to deploy. The
answers therefore ride in the deploy request body, and both answer sets AND the merge
outcome are computed inside the same request that publishes, so passing the gate and
being deployed are the same event (the request schema carries no review field at all —
the gate reads the stored review by app and version, never a browser-supplied copy,
R12). A portal may recompute the total locally to drive its own affordances (enabling
Confirm, prompting for an explanation); that copy is a convenience and is never the
decision.

THE TABLE AND THE THRESHOLD ARE ONE POLICY UNIT. `AUTO_DEPLOY_MAX_SCORE` is meaningless without
the weights it is compared against — moving one into configuration and leaving the other
in code would let them drift into a combination nobody chose (a threshold no combination
of answers can reach, or one every answer clears). They change together, in review, in
this file.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

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

# AT OR BELOW this total the deploy proceeds automatically — set to 0 (issue #115): ANY
# weighted category answered Yes routes to a human, deliberately, not a graduated scale.
# "Nothing sensitive declared" is the one shape of answer set safe enough to publish with
# no one looking at it; every other combination needs a person, however small the total.
#
# TIED to `notes_required()` (issue #117 follow-up): every declaration that fails this
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
    """Whether this answer set obliges an explanation — exactly the declarations that
    fail `qualifies_for_deploy`, so a refusal is never left unexplained and an
    explanation is never compelled and then discarded on a declaration that wasn't
    going to be refused."""
    return total_weight(flags) > AUTO_DEPLOY_MAX_SCORE


def qualifies_for_deploy(flags: Mapping[str, bool]) -> bool:
    """Whether this answer set clears the automatic-deploy threshold — i.e. is safe
    enough to publish with no human review.

    Rejects an INCOMPLETE mapping outright rather than scoring it — `total_weight`'s
    per-key omission tolerance exists for reading an old stored answer set, not for
    letting a partial declaration through the gate: a mapping missing every key scores
    0 and would otherwise silently qualify for auto-deploy, the fail-open shape of
    exactly the bug issue #115 was about.
    """
    if any(key not in flags for key in CLASSIFICATION_KEYS):
        raise ValueError("incomplete declaration cannot be scored for auto-deploy")
    return total_weight(flags) <= AUTO_DEPLOY_MAX_SCORE


def labels_for(keys: Iterable[str]) -> tuple[str, ...]:
    """The questionnaire labels for `keys`, in the order given — the ONE reader of the
    key→label pairing.

    Any sentence that names what an app handles ("It handles Credentials / Secrets,
    which needs an administrator's approval") gets its words from here, so a reworded
    question cannot leave a message naming something that is no longer on screen. The
    routed-for-review copy the deploy pipeline writes is the live caller; re-deriving
    the pairing there is what would put the label table in two places.

    An unknown key falls back to itself rather than raising: a stored answer set can
    predate a rename, and a bare key still tells the reader which question is meant —
    a sentence is not worth a 500.
    """
    labels = {key: label for key, label, _weight in DATA_CLASSIFICATION_QUESTIONS}
    return tuple(labels.get(key, key) for key in keys)


def declared_categories(flags: Mapping[str, bool]) -> tuple[str, ...]:
    """The labels of the weighted categories answered Yes, most significant first.

    The citizen cannot act on a bare number, but they can look at the categories an
    answer set DID declare and tell whether that's actually right, or whether they
    over-answered by mistake. Zero-weight categories are omitted — `Public Data` never
    moves the score either way, so listing it would be noise presented as advice.

    NOTE: `refusal_message`, this projection's original consumer, was retired in U9
    with the terminal refusal it explained (a weighted Yes now ROUTES to the admin
    queue instead of refusing). What stays is the flags-shaped reading of the same
    table; a caller that already holds the KEYS it wants named asks `labels_for`.
    """
    return labels_for(
        key for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS if weight and flags.get(key)
    )
