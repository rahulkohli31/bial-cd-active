"""ONE definition of "a word", shared by every server-side word limit.

Two surfaces count words (#158): the project title (§14, max 8) and the reason someone
gives for deleting a project (§13.2, 5–50). The issue is explicit about the hazard:

>   Define "word" once and share it. Client and server must split identically, or a
>   message that passes in the browser gets refused by the API.

So this is the server's single definition, and `portal/src/utils/words.ts` is the client's.
They are deliberately small, and deliberately equivalent:

    Python   len(value.split())
    TypeScript   value.split(PY_WHITESPACE).filter(Boolean).length

`str.split()` with no argument is the exact behaviour we want, and is not the same as
`split(" ")`: it splits on RUNS of arbitrary Unicode whitespace and discards empty tokens,
so a double space, a tab, a newline, a CRLF, a non-breaking space (U+00A0) and an
ideographic space (U+3000) all behave the way a reader would expect.

THE CLIENT SPELLS THE WHITESPACE SET OUT, and that is not fussiness: JavaScript's `\\s` is
NOT this set. Sweeping all 1,114,112 code points found six disagreements — Python's
`str.isspace()` includes the separators U+001C-U+001F and U+0085 (NEL), which `\\s` does not,
and `\\s` matches U+FEFF (the BOM), which Python does not. Nobody types those on purpose, but
a title pasted out of a spreadsheet, a mainframe export or a BOM'd file carries them, and
the whole point of this pair is that the counter someone watches cannot disagree with the
validator that refuses them. `words.ts` therefore writes the class out as `PY_WHITESPACE`
and skips `.trim()`, which trims by the JS set; `.filter(Boolean)` drops the empty
leading/trailing tokens Python discards for free.

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
