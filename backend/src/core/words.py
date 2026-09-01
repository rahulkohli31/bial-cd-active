"""ONE definition of "a word", shared by every server-side word limit.

Two surfaces count words (#158): the project title (§14, max 8) and the reason someone
gives for deleting a project (§13.2, 5–50). The issue is explicit about the hazard:

>   Define "word" once and share it. Client and server must split identically, or a
>   message that passes in the browser gets refused by the API.

So this is the server's single definition, and `portal/src/utils/words.ts` is the client's.
They are deliberately one line each, and deliberately equivalent:

    Python   len(value.split())
    TypeScript   value.trim().split(/\\s+/).filter(Boolean).length

`str.split()` with no argument is the exact behaviour we want, and is not the same as
`split(" ")`: it splits on RUNS of arbitrary Unicode whitespace and discards empty tokens,
so a double space, a tab, a newline, a CRLF, a non-breaking space (U+00A0) and an
ideographic space (U+3000) all behave the way a reader would expect. JavaScript's `\\s`
covers the same Unicode set, and `.trim()` plus `.filter(Boolean)` discards the empty
leading/trailing tokens Python drops for free.

Verified equivalent on: `""`, `"   "`, `"one"`, `"a  b"`, `"a\\tb"`, `"a\\nb"`, `"a\\r\\nb"`,
`" lead and trail "`, `"a\\u00a0b"`, `"a\\u3000b"`. Both sides have a test pinning that list,
because the whole point is that they cannot drift apart.

Counting words rather than characters is the issue's choice, not ours to relitigate here:
a title is a name, and "about 6 to 8 words" is what a person can act on. The one thing a
word rule owes them in return is that the browser and the API agree, which is this module.
"""

from __future__ import annotations


def count_words(value: str) -> int:
    """How many words `value` contains, by the shared rule above.

    `str.split()` with NO argument, never `split(" ")` — the latter would count a double
    space as an extra empty word and disagree with the client.
    """
    return len(value.split())
