/**
 * The publish score, mirrored so the dialog can show it move as the owner answers. The server's
 * score decides; `__fixtures__/classification-score-cases.json` holds both sides to one rule.
 */
import { shareOf } from './adminClassificationApi'
import type { ReviewClass } from './classificationApi'

type ScoredOn = Pick<ReviewClass, 'key' | 'kind' | 'weight'>

/** The share of the scored weight answered Yes, out of 100, rounded half up; 0 when that weight
 *  totals 0. A hard block adds nothing. */
export function classificationScore(
  answers: Readonly<Record<string, boolean>>,
  classes: readonly ScoredOn[],
): number {
  let total = 0
  let yes = 0
  for (const entry of classes) {
    if (entry.kind !== 'scored') continue
    const weight = entry.weight ?? 0
    total += weight
    if (answers[entry.key] === true) yes += weight
  }
  return shareOf(yes, total) ?? 0
}

/** The answers the score counts: the reviewer's, with the owner's over them while owners may change
 *  answers. An owner answer on a hard block counts for nothing, since a hard block adds nothing. */
export function countedAnswers({
  reviewer,
  owner,
  ownersCanChangeAnswers,
}: {
  reviewer: Readonly<Record<string, boolean>>
  owner: Readonly<Record<string, boolean>>
  ownersCanChangeAnswers: boolean
}): Record<string, boolean> {
  return ownersCanChangeAnswers ? { ...reviewer, ...owner } : { ...reviewer }
}
