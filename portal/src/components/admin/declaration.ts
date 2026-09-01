/**
 * Reading the submitted data-classification declaration for the administrator's review
 * screen (U13: R15, P3, OD-B).
 *
 * The declaration is STORED DATA, not a wire schema: the publish gate writes it once
 * (`backend/src/api/v1/deploy/router.py::_declaration`), three consumers read it, and the
 * questionnaire it is keyed by is expected to be reworded. So this module narrows
 * defensively at every step rather than typing the document and trusting it — an
 * unrecognised key renders as nothing, never as a crash and never as a blank dispute row
 * an administrator would read as "nothing was flagged".
 *
 * WHAT THE ADMINISTRATOR IS DECIDING (P3): whether an app holding this kind of data is
 * acceptable to publish. Not whether the code is correct — they are not re-auditing it.
 *
 * WHAT THEY NEVER SEE (OD-B): evidence locations. They are structurally absent from the
 * declaration — the review stores them in a separate `evidence` document that no path
 * reaching this screen reads — so this module has no branch to get wrong. The reasons it
 * does render were written for a non-technical reader and passed through the shared
 * redactor before they were stored.
 *
 * DRIFT (the version the citizen never saw), READ FROM THE `drift` BLOCK — not from the
 * two commits. `drift.answeredAbout` is the commit the citizen's answers and explanation
 * describe; when the pipeline routes a version they never saw, that is the commit that
 * differs from `commits.shipping`, and the explanation on file was written about the OTHER
 * one — so the screen names both and marks the newly-raised categories as unexplained
 * rather than presenting old prose as an answer to a new finding.
 *
 * An earlier version derived this from `commits.shipping !== commits.reviewed` and was
 * therefore DEAD: the writer sets `reviewed` to `head_sha if review.available else None`
 * against the same `head_sha` it writes to `shipping`, so the pair is only ever equal or
 * half-null — never two different commits. The banner and the per-category "not covered by
 * the explanation" marker could not render in production, on exactly the path they were
 * built for. `commits.reviewed === null` remains the different, far more common thing: no
 * review informed the decision at all.
 */
import { isRecord } from '../../utils/apiError'
import { DATA_CLASSIFICATION_QUESTIONS } from '../../utils/deployApi'
import type { ReviewVerdict } from '../../utils/classificationApi'

/** The questionnaire, in the order the citizen answered it, under the snake_case keys the
 *  declaration document is stored with (it is stored data, not a camelCase wire body).
 *
 *  DERIVED, not re-typed: the labels an administrator reads here are the same six the
 *  citizen answered on the form, and the one thing worse than a reworded question is a
 *  reworded question that only half the product agrees with. `deployApi` carries both
 *  spellings of each key precisely so this list can be projected rather than maintained. */
export const CLASSIFICATION_CATEGORIES: ReadonlyArray<readonly [key: string, label: string]> =
  DATA_CLASSIFICATION_QUESTIONS.map(([, label, , storedKey]) => [storedKey, label] as const)

/** The rejection note's floor, mirroring `MIN_REJECTION_NOTE` in
 *  `backend/src/api/v1/admin/schemas.py`. The server is the gate (422); this copy only
 *  spares an administrator discovering the floor by hitting it. */
export const MIN_REJECTION_NOTE = 20

/** One verdict, from the same union the citizen's review client narrows the live wire
 *  into — the stored document records exactly what that surface showed. */
export type { ReviewVerdict }

/** What the merge put on record for one category, in plain language. Mirrors
 *  `DisagreementKind` in `backend/src/services/classification/merge.py`; an unrecognised
 *  value is dropped rather than shown raw — a snake_case token is not an explanation. */
const DISAGREEMENT_COPY: Record<string, string> = {
  review_yes_over_citizen_no:
    'The automatic check found this kind of data; the developer answered No. The Yes stands.',
  citizen_yes_over_review_no:
    'The developer declared this kind of data; the automatic check did not find it. The Yes stands.',
  tier_a_overrule:
    'A credential-shaped value was found in the code and the automatic check still answered No. The disagreement is why this app is in front of you — please look at the code before approving.',
  scan_stood_in:
    'No automatic verdict was recorded for this question. A credential-shaped value was found in the code and stands in as the answer.',
  unevidenced_yes_routed:
    'The automatic check answered Yes here but pointed at parts of the code that do not exist, so its answer was not counted. It is in front of you because the check raised something, not because it proved it.',
}

export interface DisputedCategory {
  key: string
  label: string
  /** The developer's own answer, or `null` when the declaration did not record one. */
  citizenYes: boolean | null
  /** The automatic check's verdict, or `null` when it recorded none for this category. */
  reviewVerdict: ReviewVerdict | null
  /** The check's plain-language reason. `null` when none was recorded. */
  reason: string | null
  /** Why this category is in dispute — one sentence per recorded disagreement. */
  notes: string[]
  /** The answer of record after the merge. */
  mergedYes: boolean
  /** A Yes the developer did not declare — so their explanation is not about it. */
  newlyRaised: boolean
}

export interface CitizenAnswer {
  key: string
  label: string
  yes: boolean
}

export interface ReadDeclaration {
  /** False for a runbook-lineage row, or one queued before this feature shipped. */
  present: boolean
  /** The commit that was submitted. */
  shippingCommit: string | null
  /** The commit the recorded verdicts are about; `null` when no review informed them. */
  reviewedCommit: string | null
  /** The commit the citizen's answers and explanation were written about, when the
   *  pipeline recorded one (`drift.answeredAbout`). Null on the ordinary path, where the
   *  citizen answered about the version being shipped. */
  answeredAbout: string | null
  /** `commits.reviewed === null` — the most common new arrival, and it says so. */
  noReviewAtAll: boolean
  /** The pipeline routed a version the citizen never saw. */
  drift: boolean
  /** The categories in dispute, in questionnaire order. Leads the screen. */
  disputes: DisputedCategory[]
  /** Every category the developer answered, in questionnaire order. */
  citizenAnswers: CitizenAnswer[]
  /** The developer's (already-redacted) explanation, or null when they wrote none. */
  explanation: string | null
}

function record(parent: Record<string, unknown>, key: string): Record<string, unknown> {
  const child = parent[key]
  return isRecord(child) ? child : {}
}

function shaOrNull(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

function verdictOrNull(value: unknown): ReviewVerdict | null {
  return value === 'yes' || value === 'no' || value === 'unanswered' ? value : null
}

/** An empty reading — what a row with no declaration produces, and what the screen turns
 *  into "this app's declaration is unavailable" rather than six blank rows. */
const NOTHING: ReadDeclaration = {
  present: false,
  shippingCommit: null,
  reviewedCommit: null,
  answeredAbout: null,
  noReviewAtAll: true,
  drift: false,
  disputes: [],
  citizenAnswers: [],
  explanation: null,
}

/**
 * Narrow one submitted declaration into what the review screen renders.
 *
 * `declaration` arrives as whatever the server had on the row: `null` for a pre-feature
 * or runbook-lineage item, and otherwise a document this function is the only reader of.
 * Every field is optional to this function; none of them can throw.
 */
export function readDeclaration(declaration: Record<string, unknown> | null): ReadDeclaration {
  if (declaration === null || Object.keys(declaration).length === 0) return NOTHING

  const commits = record(declaration, 'commits')
  const citizen = record(declaration, 'citizen')
  const review = record(declaration, 'review')
  const merged = record(declaration, 'merged')
  const citizenAnswers = record(citizen, 'answers')
  const reviewAnswers = record(review, 'answers')
  const reviewReasons = record(review, 'reasons')
  const mergedAnswers = record(merged, 'answers')
  const differences = record(declaration, 'differences')

  const shippingCommit = shaOrNull(commits.shipping)
  const reviewedCommit = shaOrNull(commits.reviewed)
  // U10's own block. Absent on the ordinary path — its PRESENCE is the signal that this
  // queue item was routed by the pipeline after a save, with nobody at the form.
  const answeredAbout = shaOrNull(record(declaration, 'drift').answeredAbout)

  const disputes: DisputedCategory[] = []
  const answers: CitizenAnswer[] = []

  for (const [key, label] of CLASSIFICATION_CATEGORIES) {
    const citizenYes =
      typeof citizenAnswers[key] === 'boolean' ? (citizenAnswers[key] as boolean) : null
    if (citizenYes !== null) answers.push({ key, label, yes: citizenYes })

    const recorded = differences[key]
    const notes = (Array.isArray(recorded) ? recorded : [])
      .map((kind) => (typeof kind === 'string' ? DISAGREEMENT_COPY[kind] : undefined))
      .filter((copy): copy is string => copy !== undefined)
    // A category with no recorded disagreement is not in dispute — it is either agreed
    // or was never raised, and neither belongs at the top of this screen.
    if (notes.length === 0) continue

    const mergedYes = mergedAnswers[key] === true
    disputes.push({
      key,
      label,
      citizenYes,
      reviewVerdict: verdictOrNull(reviewAnswers[key]),
      reason: typeof reviewReasons[key] === 'string' ? (reviewReasons[key] as string) : null,
      notes,
      mergedYes,
      // The developer's explanation cannot be about a Yes they never declared.
      newlyRaised: mergedYes && citizenYes === false,
    })
  }

  const explanation =
    typeof citizen.explanation === 'string' && citizen.explanation.trim() !== ''
      ? citizen.explanation
      : null

  return {
    present: true,
    shippingCommit,
    reviewedCommit,
    noReviewAtAll: reviewedCommit === null,
    answeredAbout,
    // The citizen answered about a DIFFERENT commit than the one shipping. Read from the
    // `drift` block because that is the only place the two ever differ — see the module
    // docstring for why the `commits` pair cannot express this.
    drift: answeredAbout !== null && shippingCommit !== null && answeredAbout !== shippingCommit,
    disputes,
    citizenAnswers: answers,
    explanation,
  }
}

/** Moved to `utils/shortSha.ts` and re-exported here so this module's consumers keep their
 *  one import. It left because it stopped being an admin detail: the two citizen surfaces
 *  that disagreed with it — showing 12 where this screen showed 7 — are retired, and the
 *  one publish chip that replaced them shares this. */
export { shortSha } from '../../utils/shortSha'
