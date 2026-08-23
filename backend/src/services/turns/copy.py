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
