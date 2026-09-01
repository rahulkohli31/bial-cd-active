/**
 * First 7 of a commit — THE length the product shows a person, everywhere.
 *
 * IT LIVES HERE BECAUSE THE DISAGREEMENT IT USED TO DESCRIBE IS OVER. This was private to
 * the admin declaration module, and its comment there recorded a defect it could not fix
 * from where it sat: the admin screen showed 7 while the citizen's publish card and review
 * status card showed 12 for the same commit — "nobody chose that, and only one of the two
 * can be right", left alone because changing either was a visible change rather than a
 * refactor. Both of those cards are now gone, replaced by one publish chip, so there is one
 * citizen-facing commit display again and it can simply agree with the administrator's.
 *
 * `null` reads as an em dash rather than an empty string: a version row that renders nothing
 * where a commit belongs looks like a layout bug, and a reader cannot tell it from a commit
 * that happened to be blank.
 */
export function shortSha(sha: string | null): string {
  return sha === null ? '—' : sha.slice(0, 7)
}
