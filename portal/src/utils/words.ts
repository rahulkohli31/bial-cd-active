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
 *     TypeScript   value.trim().split(/\s+/).filter(Boolean).length
 *
 * `str.split()` with no argument splits on RUNS of Unicode whitespace and drops empty
 * tokens. `/\s+/` covers the same Unicode set, `.trim()` removes the leading/trailing
 * empties Python discards for free, and `.filter(Boolean)` handles the empty string, where
 * `''.split(/\s+/)` yields `['']` rather than `[]`.
 *
 * Verified equivalent on: `''`, `'   '`, `'one'`, `'a  b'`, `'a\tb'`, `'a\nb'`, `'a\r\nb'`,
 * ` lead and trail `, U+00A0 (non-breaking space) and U+3000 (ideographic space).
 * `words.test.ts` pins that list here and
 * `tests/api/v1/projects/test_project_name_words.py` pins its mirror image, because "the
 * same rule on both sides" is only a fact while something checks it.
 */

/** How many words `value` contains, by the shared rule above. */
export function countWords(value: string): number {
  return value.trim().split(/\s+/).filter(Boolean).length
}

/** The project title cap (#158 §14) — mirrors `MAX_PROJECT_NAME_WORDS` in
 *  `backend/src/db/models/project.py`. */
export const MAX_PROJECT_NAME_WORDS = 8

/** The delete-reason bounds (#158 §13.2). */
export const MIN_DELETE_REASON_WORDS = 5
export const MAX_DELETE_REASON_WORDS = 50
