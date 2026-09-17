/**
 * ONE definition of "a word" for the client, mirroring `backend/src/core/words.py`. Two
 * surfaces count words — project title (max 8), delete reason (5–50) — and client/server
 * must split identically, or a message that passes in the browser gets refused by the API
 * with no way to tell what it disliked (counter reads `8/8`, Create enabled, API 422s).
 * Python: `len(value.split())`. TypeScript: `value.split(PY_WHITESPACE).filter(Boolean).length`
 * — `\s` is CLOSE to Python's whitespace class but not equal (see `PY_WHITESPACE` below), so
 * the class is written out; `.filter(Boolean)` drops what Python discards for free. Verified
 * equivalent on empty/whitespace/tab/newline/CRLF/leading-trailing/NBSP/ideographic-space —
 * `words.test.ts` pins the list, `test_project_name_words.py` pins its mirror, because "same
 * rule both sides" is only a fact while something checks it.
 */

/**
 * Python's whitespace set, written out, because `\s` is NOT it — they differ on six code
 * points (Python's `isspace()` adds `U+001C`–`U+001F`/`U+0085` NEL; `\s` adds `U+FEFF` BOM).
 * Nobody types these on purpose, but a pasted spreadsheet title or a BOM'd UTF-8 file
 * carries them, and the point of this module is that the count a user watches cannot
 * disagree with the validator that refuses them. `.trim()` is deliberately unused too: it
 * trims by the JS set, so a leading BOM would vanish here but count as a word on the server.
 */
// The control characters below are the POINT, not a typo. eslint's no-control-regex exists
// because one in a regex is normally accidental; U+001C-U+001F are in Python's whitespace
// set, and omitting them is exactly what made the two counters disagree. The exhaustive
// sweep in `words.test.ts` is what keeps the two sides honest.
const PY_WHITESPACE =
  // eslint-disable-next-line no-control-regex
  /[\t\n\v\f\r \u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/

/** How many words `value` contains, by the shared rule above. */
export function countWords(value: string): number {
  return value.split(PY_WHITESPACE).filter(Boolean).length
}

/** The project title cap — mirrors `MAX_PROJECT_NAME_WORDS` in
 *  `backend/src/db/models/project.py`. */
export const MAX_PROJECT_NAME_WORDS = 8

/** The project description bounds — mirrors `MIN_PROJECT_DESCRIPTION_WORDS` /
 *  `MAX_PROJECT_DESCRIPTION_WORDS` in `backend/src/db/models/project.py`. Unlike the title,
 *  description has a MINIMUM: a one-line description embeds into a single vector for
 *  semantic search, so one too short to say anything embeds to nothing worth matching. */
export const MIN_PROJECT_DESCRIPTION_WORDS = 15
export const MAX_PROJECT_DESCRIPTION_WORDS = 120

/** The delete-reason bounds (#158 §13.2). */
export const MIN_DELETE_REASON_WORDS = 5
export const MAX_DELETE_REASON_WORDS = 50

/** THE COLUMN'S PASTE BACKSTOP, not the rule anybody is told about — that is the word count.
 *  Mirrors the server's own `clean_deletion_reason` cap, and lives here because BOTH deletes
 *  now need it: the citizen's project delete and the administrator's app delete. It was a
 *  private constant in `ProjectDeleteDialog` while only one of them existed. */
export const MAX_DELETE_REASON_CHARS = 2000
