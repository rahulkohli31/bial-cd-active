"""ONE definition of "a word", shared by every server-side word limit.

WHY THIS EXISTS
Two surfaces count words: the project title (max 8) and the reason given
for deleting a project. Client and server must split identically or
a message that passes in the browser gets refused by the API, so this is
the server's definition, and `portal/src/utils/words.ts` is the client's —
deliberately small and deliberately equivalent:

    Python       len(value.split())
    TypeScript   value.split(PY_WHITESPACE).filter(Boolean).length

`str.split()` with no argument splits on RUNS of Unicode whitespace and
discards empty tokens, unlike `split(" ")`. JavaScript's `\\s` is NOT that
set: sweeping all 1,114,112 code points found six disagreements — e.g.
`str.isspace()`'s U+001C-U+001F and U+0085 (NEL), absent from `\\s`, and
`\\s` matching U+FEFF (the BOM), absent from Python's set. So `words.ts`
spells the class out as `PY_WHITESPACE`, skips `.trim()` (JS whitespace),
and uses `.filter(Boolean)` for the empty tokens Python drops for free.
Verified equivalent on `""`, `"   "`, `"one"`, `"a  b"`, `"a\\tb"`,
`"a\\nb"`, `"a\\r\\nb"`, `" lead and trail "`, `"a\\u00a0b"`, `"a\\u3000b"`
— pinned in tests on both sides so they cannot drift apart. Counting words
rather than characters is a product choice, not this module's: this
module's only job is agreeing with the client on what one word is.
"""

from __future__ import annotations


def count_words(value: str) -> int:
    """How many words `value` contains, by the shared rule above.

    `str.split()` with NO argument, never `split(" ")` — the latter would count a double
    space as an extra empty word and disagree with the client.
    """
    return len(value.split())
