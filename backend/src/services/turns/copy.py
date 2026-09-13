"""Every sentence the platform puts in front of a citizen, in one place.

ONE MODULE RATHER THAN STRINGS AT THEIR CALL SITES, because a promise about a CLASS of text can
only be kept if the class has an address. The guard
`test_no_sentence_this_plan_shows_a_citizen_carries_developer_jargon` iterates `vars()` of this
module, so a sentence written inline at its call site is outside that guard by construction.

THE REGISTER these sentences are held to: no file path, no command, no library or framework name.
They say what happened, what the person is looking at, and one thing they can do, in the words the
person who asked for the app already knows. A sentence here that needs to name something technical
is a wrong sentence rather than a case for relaxing the rule."""

from __future__ import annotations

from typing import Final

WORKSPACE_UNAVAILABLE_TEXT: Final = (
    "Your workspace isn't available right now, so this message wasn't sent. Try again in a moment."
)
"""There is nothing to run the turn against, said at the moment of sending — before the message is
spent rather than after."""

WORKSPACE_UNAVAILABLE_CODE: Final = "workspace_unavailable"
"""The code this refusal carries, which is how a client tells it from the workspace CONFLICTS that
share its status family: same status, different remedies."""

CHAT_TOO_LONG_TEXT: Final = (
    "This chat has got too long to carry on. Start a new chat to keep going — your app and "
    "everything you have built stays exactly as it is."
)
"""What a citizen is told when a conversation has grown past the boundary set for them.

IT NAMES NO NUMBER. "You have used 203,412 of your 200,000" is not something a person can act on,
and both halves of it are words for the platform's accounting rather than for what is in front of
them. The number belongs where an administrator sets it.

THE SECOND SENTENCE IS THE LOAD-BEARING ONE. The only reason a citizen would hesitate to start a
new chat is the fear that the work goes with the conversation. It does not — the app lives in the
project — and without saying so, the honest reading of the first sentence is "you have lost your
app"."""

CHAT_TOO_LONG_CODE: Final = "context_hard_limit_exceeded"
"""The code the too-long refusal carries.

NOTHING IN THIS CODEBASE FORCES A READER TO HANDLE A NEW CODE — no `Literal` union, no native
enum, no exhaustiveness anywhere on the refusal path; every code is an open string compared by
hand. So adding one is free and silent, and only a test can notice a reader dropping it."""

ALREADY_BUILDING_HERE_CODE: Final = "already_building_here"
"""This user's one workspace is committed to another chat of their own, and the remedy is to finish
or stop what is running there.

The OTHER 409 on this route, `sandbox_reclaim_blocked`, means somebody's work in a DIFFERENT
project is in the way and its remedy is a choice about that project. Two remedies behind one status
code: without a machine code on each, a client can only tell them apart by reading prose, which is
how a bug of exactly this shape has already shipped here once."""

WRITING_UP_THE_PLAN_LABEL: Final = "Writing up the plan"
"""What the screen says between the model beginning the offer call and the plan arriving —
thousands of tokens stream through that gap with nothing else on screen for the whole of it.

A PRESENT-PARTICIPLE PHRASE like every other step label, so the long-operation narrator can restate
it ("Still writing up the plan — this one takes a little longer.") without a second table to keep
in step."""

PLAN_NOT_KEPT_TEXT: Final = (
    "That plan didn't arrive in a form we could keep, so there's nothing to build from yet. "
    "Ask for it again and it should come through whole."
)
"""The offer carried no plan, or one past what a message can hold — one sentence for both, because
from where the citizen sits they are one thing: they asked for a plan and there is nothing to
press. It never mentions a limit, a tool or a character count.

THE LONG PLAN IS REFUSED RATHER THAN TRIMMED, deliberately: a plan cut mid-sentence is one the
citizen would agree to and the build would never see the end of."""

STILL_SHOWING_TEMPLATE: Final = "the starting template"
"""The app responds, and its home page is still the one the workspace was created with."""

STILL_SHOWING_EARLIER: Final = "an earlier version of itself"
"""The app responds and is genuinely the user's app — just not with this change in it."""

STILL_SHOWING_NOTHING: Final = "nothing yet"
"""The app is not serving at all, so there is no version of it to describe."""

DID_NOT_COME_TOGETHER_TEXT: Final = (
    "That change didn't come together. Your app is still showing {showing}. "
    "Try describing it a different way."
)
"""How a turn ends when the change could not be made to work.

`{showing}` is filled from the health verdict, never guessed, and it is there because "it didn't
work" leaves the user unable to act: whether they are looking at the starting template, at their
own app one change behind, or at nothing at all changes what they should do next."""


COULD_NOT_CONFIRM_TEXT: Final = (
    "Your app looks like it's running, but we couldn't confirm this change went in. "
    "Open the preview and see — and if something looks wrong, say so and we'll fix it."
)
"""How a turn ends when the platform genuinely could not tell — rare by construction, because the
health verdict already asks again before reporting anything, so reaching this means several checks
in a row came back unanswerable.

It does not apologise and it does not alarm. The likeliest truth is that the app is fine and what
is missing is a confirmation, which the person looking at the preview can supply in a second.
Dressing an unanswerable check up as a defect instead costs the citizen a repair run, their tokens
and their time chasing a fault that was never there.
"""


RECOVERED_TEXT: Final = (
    "Your workspace had been reset, so we're putting your app back from the last copy we kept. "
    "This takes a moment. Send your message again once it's back."
)
"""What a citizen is told when the platform finds their workspace has been wiped.

SAID BEFORE THE RESTORE RUNS, not after, and that ordering is the whole reason this string is a
constant rather than a return value. Putting an app back takes tens of seconds, during which the
screen would otherwise say nothing at all — indistinguishable from the product having hung.

IT ALSO ASKS FOR THE MESSAGE AGAIN, and that is not politeness. The instruction the citizen typed
was written against a workspace that no longer exists; running it against the restored tree would
execute an instruction whose premise was true when it was typed and false when it ran."""


NOT_RECOVERED_TEXT: Final = (
    "Your workspace was reset and we don't have a copy of your app to put back. "
    "Nothing you see below is your work. Please tell your administrator before you carry on."
)
"""Confirmed loss with nothing to restore from: no autosave, no saved version.

THE MIDDLE SENTENCE IS DOING THE MOST WORK, and softening it is exactly what causes the harm.
Without it the preview beside this banner shows a running app — the starter template — and a
reasonable person reads a running app as their app, carries on building on top of it, and the next
turn's copy makes that permanent.

EXACTLY ONE NEXT ACTION. "Tell your administrator" is the only true one: there is nothing the
citizen can do themselves, and offering a retry would be a lie about what a retry does."""


UNVERIFIED_TEXT: Final = (
    "We couldn't check whether your workspace is intact, so keep an eye on your app as you go. "
    "If something looks wrong, say so and we'll sort it out."
)
"""The check came back structurally unanswerable: retrying will not help, so the turn proceeds
rather than locking the citizen out of their own project. Nothing is restored and nothing is
destroyed.

SAID ONCE PER SESSION. Repeating it every turn would train the reader to skip it, and it is the
same fact each time — the state of the app, not an event. It is also the sentence most likely to
be a false alarm, which is another reason not to shout it."""


COULD_NOT_CHECK_TEXT: Final = (
    "We couldn't reach your workspace to check on it. Please try again in a moment."
)
"""The RETRYABLE half, and the reason it is a different sentence from `UNVERIFIED_TEXT`: the two
read alike and mean opposite things. This one is a blip — the container did not answer, so nothing
was checked, nothing was changed, and trying again is likely to work — so this turn STOPS where
`UNVERIFIED_TEXT`'s proceeds."""


KEPT_A_COPY: Final = "we've kept a copy of your app, so nothing you did today is lost"
"""The reassuring half of `AT_LIMIT_TEXT`, said only when a copy has actually been stored.

A SEPARATE CONSTANT BECAUSE THE CLAIM IS CONDITIONAL. Folding "your app is safe" into the at-limit
sentence would make the platform assert it on the one path where it might not be true — and a false
reassurance is worse here than no reassurance at all, because the citizen acts on it by closing the
tab."""


COULD_NOT_KEEP_A_COPY: Final = (
    "we weren't able to keep a copy of your app just now, so save it before you leave this page"
)
"""The other half, said ONLY when the platform tried to keep a copy and failed.

Saying it only when it is true is what gives it teeth: a save instruction attached to every
at-limit message, whether or not anything was secured, reads as a formality and is ignored — so on
the day the copy genuinely did not land, the one alarming sentence looks like boilerplate. The
wording has to carry that ordering too, since the platform tried first: this is a request for help
rather than an instruction the citizen was always going to be given."""


AT_LIMIT_TEXT: Final = (
    "You've used up your building budget for today, {kept}. "
    "You can carry on after midnight, and if you need more before then, email {contact}."
)
"""What a citizen is told when their daily budget runs out. THREE FACTS, EACH LOAD-BEARING.

*What happened*, in the words the person used to ask for the app: a budget for the day, used up.
Not a token cap, not a quota, not a limit exceeded — none of which tell a non-technical reader
whether they broke something, whether it will happen again, or whether it is about them at all.
"Budget" is also the word the existing surfaces already use for the same fact, so the citizen is
not asked to learn a second name for one thing.

*Whether their work survived*, filled from `KEPT_A_COPY` / `COULD_NOT_KEEP_A_COPY` by the caller
that actually performed the write — so the reassurance is never a guess dressed as a fact.

*When it comes back, and who to ask if that is too late.* `{contact}` is a single configured
support address (`ApiSettings.SUPPORT_CONTACT_EMAIL`) rather than a role, which the citizen has no
way to turn into an address. It is a plain address rather than a `mailto:` URI on purpose: the
banner above the composer renders text, and a URI scheme printed mid-sentence is exactly the
register this module exists to keep out. Making it clickable is the renderer's job.

"After midnight" rather than a clock time: the reset is the next IST midnight, this is a
single-tenant deployment in one timezone, and a rendered timestamp would invite the reader to work
out whether it means tonight or tomorrow."""


PROPOSAL_EVERYTHING_LEAD: Final = "Here is everything I picked up from that:"
"""Say the whole thing back before narrowing it.

WITHOUT THIS THE PROPOSAL READS AS A REFUSAL. A citizen who asked for nine things and is answered
with three has been told "no" to six of them unless they can see that all nine were heard. Listing
them back costs a few lines and turns the same message from a decline into a running order.

The list under it is the agent's own words for its own pieces, and nothing here filters them: the
register bar above applies to the platform's own sentences, never to model text."""

PROPOSAL_FIRST_LEAD: Final = "I would start with:"
"""What this round is. Present tense and first person, matching the register the agent narrates
in — the platform is framing the agent's proposal, not announcing a decision of its own."""

PROPOSAL_REST_TEXT: Final = (
    "The rest stays on the list — say the word once these are working and I will carry on."
)
"""What happens to everything else, and it is only true when something IS left.

RENDERED CONDITIONALLY, because a slice that covers everything found has no remainder and a
sentence promising to come back to nothing is the platform inventing an outstanding item. The
renderer omits it rather than softening it.

IT NAMES THE NEXT ACTION. "Say the word" is something the citizen can do in the composer already
in front of them; "the rest is deferred" tells them a state and leaves them nowhere."""

REMAINDER_TEXT: Final = "Still to do from what we agreed: {pieces}."
"""What was agreed and not built, named by the platform from its own record rather than from the
agent's recollection. The agreed list is the arguments of the proposal call the citizen read, the
finished list is what the agent marked as it landed, and this sentence is the difference — nothing
here reads the closing summary or any other prose.

`{pieces}` is filled from the agreed list, so the citizen sees the same words in the same order
they agreed to, not a re-description of them."""

CANNOT_TELL_WHAT_REMAINS_TEXT: Final = (
    "Some of what we agreed may still be outstanding — I could not tell which pieces landed. "
    "Have a look and say what is missing."
)
"""The honest middle, and the reason the remainder is not simply `agreed − marked`.

The finished half is agent-supplied: an agent that built all four pieces and marked none is
indistinguishable, from the marks alone, from one that built nothing. Rendering "these four remain"
in the platform's own voice would be a false fact the citizen has no reason to doubt — strictly
worse than the agent's own recollection, which is what a platform-computed remainder replaces.

So the claim is keyed on something the platform DOES hold: whether the workspace was touched. Marks
and no touch, marks and touch, no marks and no touch are all answerable. No marks and work landed
is not, and this is what it says instead — "could not tell" is never collapsed into a verdict."""


SPENT_ENOUGH_TEXT: Final = (
    "This one has taken as much as I want to spend on it in a single go, {kept}. "
    "Your app is working — have a look, and send me the next bit when you are ready."
)
"""How a turn ends when it reaches the platform's spend bound.

IT DOES NOT NAME A BOUND, AND THAT IS DELIBERATE. Three internal ceilings can end a run —
requests, wall clock, spend — and which one fired is not something a citizen can act on
differently: the next move is the same message either way. Naming one would also run straight into
this module's own register rule, since "token budget", "request limit" and "wall clock" are all
words for the platform's problem rather than theirs. Which bound fired is in the record and the
logs, where the person who can act on it will look.

"AS MUCH AS I WANT TO SPEND" rather than "you have run out". The citizen has not done anything
wrong and has not hit a limit of their own — the daily budget is a different sentence, and
confusing the two would tell them to wait until midnight when they can carry on right now.

IT SAYS THE APP IS WORKING, because that is what the piece-at-a-time ordering buys and it is the
fact that makes this ending survivable. `{kept}` is filled by the same securing function the
daily-budget ending uses, so the reassurance is conditional on a copy actually landing."""


MODEL_UNAVAILABLE_CODE: Final = "model_unavailable"
"""The machine-readable half of a model service that failed mid-turn: the `reason` on the terminal
frame, mirrored by the portal's `OUTCOME_COPY`. A token, never prose."""

MODEL_UNAVAILABLE_TEXT: Final = (
    "The assistant's service stopped responding partway through, {kept}. "
    "Send your message again in a minute and it will carry on from here."
)
"""What a citizen is told when the model service fails mid-build: a retryable status that outlived
the SDK's own retries, a connection that never answered, or a stream that ended in an error event.

WHY NOT THE GENERIC SENTENCE. "The assistant hit a problem and this turn was stopped" is right for
a platform bug and wrong here: the assistant is fine, the workspace is exactly as the last write
left it, and the thing to do is send again. `{kept}` is filled by the same securing function the
daily budget and the run bounds use, so the reassurance is verified rather than assumed.

NOT A CLAIM ABOUT 2026-09-11. That incident's generic failure is still unexplained: Foundry's
metrics for the minute show only status 200, no errors and no client-closed requests. The terminal
row's `error` field (`engine.py::_error_signature`) is what records the real cause next time."""

MODEL_UNAVAILABLE_PLAN_TEXT: Final = (
    "The assistant's service stopped responding partway through. "
    "Send your message again in a minute."
)
"""The same ending in a Plan chat, where there is no workspace to have kept a copy of."""


DEPENDENCY_DRIFT_TEXT: Final = (
    "Your app couldn't be packaged up: the ready-made pieces it is built from no longer match "
    "the list it was set up with. Nothing was published — ask me to put that right and try "
    "again."
)
"""What a citizen is told when publishing stops while the app's pieces are being fetched.

THE DIAGNOSTIC IS UNSAYABLE HERE. It is a package name and two version numbers, and that IS the
whole of what went wrong — there is no honest way to shorten it into this register, so the
sentence names the shape of the fault instead and the diagnostic goes to the operator detail,
where the person who can act on it looks.

IT SAYS NOTHING WAS PUBLISHED rather than that a previous version is still running, which is the
reassurance the other build failures carry: on a first publish there is no previous version, and
this failure happens before anything the citizen could be looking at has changed."""


STILL_OPEN_WITH_CHANGES_TEXT: Final = (
    "“{project}” is still open and has changes that are not saved yet."
)
"""Another project of this person's holds the one workspace, and it was read as having work in
it.

Stated as a fact because it is one — the tree was questioned and it answered. It borrows the
hand-over dialog's own words for the same situation, so a citizen who meets both is not asked
to work out whether they are being told about one thing or two."""


STILL_OPEN_ALL_SAVED_TEXT: Final = "“{project}” is still open, and everything in it is saved."
"""The same project, questioned and answered clean.

THE STATE THIS EXISTS FOR. The answer is three-valued and one hedge used to cover two of them,
so a project the platform had just proven saved was described as one that might have unsaved
work — sending the citizen to look for changes that are not there, and teaching them that what
the platform says about their work is a guess. A hedge is honest only where something is
genuinely unknown."""


STILL_OPEN_MAY_HAVE_CHANGES_TEXT: Final = (
    "“{project}” is still open and may have changes that are not saved yet."
)
"""The third answer: the project could not be questioned at all, or would not say.

The hedge leans towards unsaved on purpose. Nothing was read, and of the two ways to be wrong
here only one costs the citizen their work."""


SAVE_OR_CLOSE_IT_TEXT: Final = "Save or close it, then send this again."
"""The one action to offer beside a project that has, or may have, work in it."""


CLOSE_IT_TEXT: Final = "Close it, then send this again."
"""The same action with the save dropped, for the arm where there is nothing to save. Asking
someone to save a project the platform has just proven saved is the same untruth as the hedge,
wearing a verb instead of an adjective."""


def still_open_text(project_name: str, *, dirty: bool | None) -> str:
    """Which of the three a citizen reads about the project holding their workspace.

    THE THREE-WAY SPLIT LIVES HERE rather than at the refusals, because there are two of them —
    the conflict a client renders as a choice, and the sentence a turn ends on — and a split
    written out twice is a split that gets corrected once."""
    if dirty is None:
        template = STILL_OPEN_MAY_HAVE_CHANGES_TEXT
    elif dirty:
        template = STILL_OPEN_WITH_CHANGES_TEXT
    else:
        template = STILL_OPEN_ALL_SAVED_TEXT
    return template.format(project=project_name)


def still_open_send_again_text(project_name: str, *, dirty: bool | None) -> str:
    """The same sentence for a reader with no buttons beside it, so it carries the action too.

    Built ON `still_open_text` rather than beside it: the two surfaces make one claim about the
    work, and only what the citizen can do next differs."""
    next_step = CLOSE_IT_TEXT if dirty is False else SAVE_OR_CLOSE_IT_TEXT
    return f"{still_open_text(project_name, dirty=dirty)} {next_step}"
