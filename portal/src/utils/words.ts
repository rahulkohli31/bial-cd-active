/**
 * ONE definition of "a word" for the client, mirroring `backend/src/core/words.py`.
 *
 * Two surfaces count words (#158): the project title (§14, max 8) and the reason someone
 * gives for deleting a project (§13.2, 5–50). The issue names the hazard directly:
 *
 * >   Define "word" once and share it. Client and server must split identically, or a
 * >   message that passes in the browser gets refused by the API.
 *
 * That is the whole job of this file. The counter a person watches while typing, and the
 * validator that can refuse them, have to agree — otherwise the counter reads `8/8`, the
 * Create button is enabled, and the API answers 422 with no way for the user to tell what
 * it disliked.
 *
 * The two implementations:
 *
 *     Python       len(value.split())
 *     TypeScript   value.split(PY_WHITESPACE).filter(Boolean).length
 *
 * `str.split()` with no argument splits on RUNS of Unicode whitespace and drops empty
 * tokens. `\s` is CLOSE to that set but not equal to it (see `PY_WHITESPACE` below), so the
 * class is written out; `.filter(Boolean)` drops the leading, trailing and empty tokens
 * Python discards for free.
 *
 * Verified equivalent on: `''`, `'   '`, `'one'`, `'a  b'`, `'a\tb'`, `'a\nb'`, `'a\r\nb'`,
 * ` lead and trail `, U+00A0 (non-breaking space) and U+3000 (ideographic space).
 * `words.test.ts` pins that list here and
 * `tests/api/v1/projects/test_project_name_words.py` pins its mirror image, because "the
 * same rule on both sides" is only a fact while something checks it.
 */

/**
 * Python's whitespace set, written out, because `\s` is NOT it.
 *
 * The two differ on six code points, and an exhaustive sweep is how that was found rather
 * than a guess: Python's `str.isspace()` includes the file/group/record/unit separators
 * `U+001C`–`U+001F` and `U+0085` (NEL), which JavaScript's `\s` does not; and `\s` matches
 * `U+FEFF` (the byte-order mark), which Python does not. Six characters nobody types on
 * purpose — but a title pasted out of a spreadsheet, a mainframe export or a UTF-8 file
 * with a BOM carries them, and the whole point of this module is that a count the user
 * watches cannot disagree with the validator that refuses them.
 *
 * `.trim()` is deliberately not used either: it trims by the JS set, so a leading `U+FEFF`
 * would vanish here and count as part of the first word on the server.
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

/** The project title cap (#158 §14) — mirrors `MAX_PROJECT_NAME_WORDS` in
 *  `backend/src/db/models/project.py`. */
export const MAX_PROJECT_NAME_WORDS = 8

/** The delete-reason bounds (#158 §13.2). */
export const MIN_DELETE_REASON_WORDS = 5
export const MAX_DELETE_REASON_WORDS = 50
