"""Every sentence this plan puts in front of a citizen, in one place.

ONE FILE, FOR TWO REASONS THAT ARE BOTH ABOUT KEEPING A PROMISE. The plan commits that no message
it introduces contains a file path, a command, a library name or a framework term — and a promise
about a class of text can only be tested if the class has an address. `test_no_jargon_reaches_the_
citizen` iterates over this module; a sentence written inline at its call site would be outside
that guard by construction, and nobody would notice until a user read it.

The second reason is precedence. The plan's message surface allows AT MOST ONE banner on screen at
a time, newest wins, and deciding that is only possible when the whole set is visible together.

THE REGISTER, and it is the point of the plan rather than a style preference. On 2026-08-18 the
agent wrote 2,397 words of developer jargon to a non-technical user. These sentences say what
happened, what the user is looking at, and one thing they can do — in the words the person who
asked for the app already knows. When a sentence here needs to name something technical, that is
a sign the sentence is wrong, not that the rule is.

*The agent's own narration is NOT covered by any of this, and it is still 2,397 words. That is the
companion plan's work; this file is deliberately the whole of what this one changes about voice.*
"""

from __future__ import annotations

from typing import Final

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
"""R13 — how a turn ends when the change could not be made to work.

THE ALTERNATIVE IS A PROGRESS STATE THAT RUNS FOREVER, which is what the citizen got: the build
stopped and the screen did not, so the only way to learn it was over was to wait long enough to
stop believing it. Ending is not the failure — pretending not to have ended is.

`{showing}` is filled from the health verdict, never guessed, and it is there because "it didn't
work" leaves the user unable to act: whether they are looking at the starting template, at their
own app one change behind, or at nothing at all changes what they should do next."""


COULD_NOT_CONFIRM_TEXT: Final = (
    "Your app looks like it's running, but we couldn't confirm this change went in. "
    "Open the preview and see — and if something looks wrong, say so and we'll fix it."
)
"""R10 — how a turn ends when the platform genuinely could not tell.

RARE BY CONSTRUCTION, and it has to exist anyway. The health verdict already asks again before
reporting anything, so reaching this means several checks in a row came back unanswerable. The
alternative was to dress that up as a defect — which is what the platform used to do, and it cost
the citizen a repair run, their tokens and their time chasing a fault that was never there.

It does not apologise and it does not alarm. The likeliest truth is that the app is fine; what we
are short of is a confirmation, and the person looking at the preview can supply one in a second.
"""


RECOVERED_TEXT: Final = (
    "Your workspace had been reset, so we're putting your app back from the last copy we kept. "
    "This takes a moment. Send your message again once it's back."
)
"""R3/R5 — what a citizen is told when the platform finds their workspace has been wiped.

SAID BEFORE THE RESTORE RUNS, not after, and that ordering is the whole reason this string is a
constant rather than a return value. Putting an app back is a full bundle of the reverted tree
plus a complete restore — tens of seconds during which the screen would otherwise say nothing at
all, which is indistinguishable from the product having hung.

IT ALSO ASKS FOR THE MESSAGE AGAIN, and that is not politeness. The instruction the citizen typed
was written against a workspace that no longer exists; running it against the restored tree would
execute an instruction whose premise was true when it was typed and false when it ran. Asking is
the honest answer to the latency this path introduces.

NO JARGON, checked against the same bar as the rest of this module: no bundle, no container, no
snapshot, no git. "Workspace" is the word the product already uses on screen."""


NOT_RECOVERED_TEXT: Final = (
    "Your workspace was reset and we don't have a copy of your app to put back. "
    "Nothing you see below is your work. Please tell your administrator before you carry on."
)
"""AE3 — the worst sentence in the product, and it has to exist.

Confirmed loss with nothing to restore from: no autosave, no saved version. The temptation is to
soften it, and softening it is exactly what causes the harm — the citizen would carry on building
on top of an empty template believing it to be their app, and the next turn's copy would make
that permanent.

EXACTLY ONE NEXT ACTION. "Tell your administrator" is the only true one: there is nothing the
citizen can do themselves, and offering a retry would be a lie about what a retry does.

The middle sentence is doing the most work. Without it the preview beside this banner shows a
running app — the starter template — and a reasonable person reads a running app as their app."""


UNVERIFIED_TEXT: Final = (
    "We couldn't check whether your workspace is intact, so keep an eye on your app as you go. "
    "If something looks wrong, say so and we'll sort it out."
)
"""R2 — the honest middle answer, said once per session and then not again.

The check came back structurally unanswerable: retrying will not help, so the turn proceeds rather
than locking the citizen out of their own project. Nothing is restored and nothing is destroyed.

SAID ONCE. Repeating it every turn would train the reader to skip it, and it is the same fact each
time — the state of the app, not an event. It is also the sentence most likely to be a false
alarm, which is another reason not to shout it."""


COULD_NOT_CHECK_TEXT: Final = (
    "We couldn't reach your workspace to check on it. Please try again in a moment."
)
"""R2 — the RETRYABLE half, and the reason it is a different sentence from `UNVERIFIED_TEXT`.

The two read alike and mean opposite things. This one is a blip: the container did not answer, so
nothing was checked, nothing was changed, and trying again is likely to work. `UNVERIFIED_TEXT` is
structural — retrying will not help, so that turn PROCEEDS while this one stops.

Stopping is the right trade exactly once, which is why the verdict caps consecutive unanswerable
checks at two: a container that has permanently stopped answering must not be able to refuse a
citizen their own project on every message, with a retry prompt that can never succeed."""


KEPT_A_COPY: Final = "we've kept a copy of your app, so nothing you did today is lost"
"""The reassuring half of `AT_LIMIT_TEXT`, said only when a copy has actually been stored.

A SEPARATE CONSTANT BECAUSE THE CLAIM IS CONDITIONAL. Folding "your app is safe" into the
at-limit sentence would make the platform assert it on the one path where it might not be true —
and a false reassurance is worse here than no reassurance at all, because the citizen acts on it
by closing the tab."""


COULD_NOT_KEEP_A_COPY: Final = (
    "we weren't able to keep a copy of your app just now, so save it before you leave this page"
)
"""The other half, and the reason the reassurance is conditional at all.

WHAT IT REPLACES is the old at-limit sentence, which told every citizen to click Save whether or
not anything had been secured, and secured nothing itself. That reads as a formality — most
people ignore it — so on the day the copy genuinely did not land, the one sentence that should
have been alarming looked exactly like the boilerplate it appeared beside every other time.

Saying it ONLY when it is true is what gives it teeth. It also has to be honest about the order
of events: the platform tried first and failed, so this is a request for help rather than an
instruction the citizen was always going to be given."""


AT_LIMIT_TEXT: Final = (
    "You've used up your building budget for today, {kept}. "
    "You can carry on after midnight, and if you need more before then, email {contact}."
)
"""R31/AE18 — what a citizen is told when their daily budget runs out.

THREE FACTS, AND EACH ONE IS THERE BECAUSE ITS ABSENCE COST SOMEBODY SOMETHING.

*What happened*, in the words the person used to ask for the app: a budget for the day, used up.
Not a token cap, not a quota, not a limit exceeded — none of which tell a non-technical reader
whether they broke something, whether it will happen again, or whether it is about them at all.
"Budget" is also the word the existing surfaces already use for the same fact, so the citizen is
not asked to learn a second name for one thing.

*Whether their work survived*, filled from `KEPT_A_COPY` / `COULD_NOT_KEEP_A_COPY` by the caller
that actually performed the write. The old sentence said "your changes are still in the
workspace — click Save to keep them", which was a guess dressed as a fact: it was true only for
as long as the container lived, and nothing in the sentence had checked.

*When it comes back, and who to ask if that is too late.* "Contact your administrator" was the
previous answer and it names a role, not a person — the citizen has no way to turn it into an
address, so the sentence ends in a dead end. `{contact}` is a single configured support address
(`ApiSettings.SUPPORT_CONTACT_EMAIL`), and it is a plain address rather than a `mailto:` URI on
purpose: the banner above the composer renders text, and a URI scheme printed mid-sentence is
exactly the register this module exists to keep out. Making it clickable is the renderer's job —
`BuildProgress` turns the address in this sentence into a real `mailto:` link.

"After midnight" rather than a clock time: the reset is the next IST midnight, this is a
single-tenant deployment in one timezone, and a rendered timestamp would invite the reader to
work out whether it means tonight or tomorrow. The exact instant is still on the wire
(`QuotaFrame.resets_at`) for the surfaces that want to show it."""
