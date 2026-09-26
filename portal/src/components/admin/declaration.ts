/**
 * Reads a stored publish declaration for the admin screens. Two shapes exist, told apart by the
 * `version` field: the class declaration every gate decision writes (`version: 2`), and the older
 * six-question shape, which has none and renders as it was sent.
 *
 * Both are stored data, not a wire schema, so each reader narrows defensively: an unrecognised
 * field renders as nothing, never a crash. A class declaration carries its own snapshot of the
 * classes and the policy, so nothing here reads the live configuration. Neither shape carries an
 * evidence location.
 */
import { isRecord } from '../../utils/apiError'
import type { ClassKind } from '../../utils/classificationApi'

/** The rejection note's floor, mirroring `MIN_REJECTION_NOTE` in
 *  `backend/src/api/v1/admin/schemas.py`. The server is the gate (422); this copy only
 *  spares an administrator discovering the floor by hitting it. */
export const MIN_REJECTION_NOTE = 20

function record(parent: Record<string, unknown>, key: string): Record<string, unknown> {
  const child = parent[key]
  return isRecord(child) ? child : {}
}

function shaOrNull(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

function textOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null
}

/** A stored time, or null when it is absent or not a time at all. */
function timeOrNull(value: unknown): string | null {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value)) ? value : null
}

function booleanOrNull(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

// --- the class declaration ---

/** Why a send went to an administrator, as the gate stored it. */
export type RouteReason = 'hard_block' | 'over_threshold' | 'review_unfinished' | 'rejection_standing'

const ROUTE_REASONS: readonly RouteReason[] = ['hard_block', 'over_threshold', 'review_unfinished', 'rejection_standing']

/** One class, as it was judged. */
export interface JudgedClass {
  key: string
  title: string
  kind: ClassKind
  /** Null exactly for a hard block. */
  weight: number | null
  /** Null when the agent's review did not finish. */
  agent: boolean | null
  /** The agent's short reason, when it recorded one. */
  reason: string | null
  /** The answer the final score counted for the owner. Null for a hard block, and for every
   *  class when owners could not change answers. */
  owner: boolean | null
  /** The owner's answer differs from the agent's. */
  changed: boolean
}

export interface ClassDeclaration {
  version: 2
  commit: string | null
  /** When the version was saved; null when the store did not report it. */
  savedAt: string | null
  /** When the agent finished the review the decision read; null when that review was not current. */
  checkedAt: string | null
  /** When the gate decided, which is when its policy snapshot was taken. */
  decidedAt: string | null
  threshold: number | null
  ownersCanChangeAnswers: boolean | null
  /** Hard blocks first, then scored classes, each in the order the snapshot holds them. */
  classes: JudgedClass[]
  /** The hard blocks the agent answered Yes. */
  found: JudgedClass[]
  reviewerScore: number | null
  score: number | null
  /** Null for a send that published by itself. */
  reason: RouteReason | null
  note: string | null
}

function readClasses(declaration: Record<string, unknown>): ClassDeclaration {
  const policy = record(declaration, 'policy')
  const agentAnswers = isRecord(declaration.reviewerAnswers) ? declaration.reviewerAnswers : null
  const ownerAnswers = isRecord(declaration.ownerAnswers) ? declaration.ownerAnswers : null
  const reasons = record(declaration, 'reviewerReasons')
  const snapshot = Array.isArray(declaration.classes) ? declaration.classes : []

  const judged = snapshot.flatMap((entry: unknown): JudgedClass[] => {
    if (!isRecord(entry) || typeof entry.key !== 'string' || typeof entry.title !== 'string') return []
    const kind = entry.kind === 'hard_block' || entry.kind === 'scored' ? entry.kind : null
    if (kind === null) return []
    const agent = booleanOrNull(agentAnswers?.[entry.key])
    // An owner answer absent from a scored class counted as the agent's.
    const owner = kind === 'hard_block' || ownerAnswers === null ? null : (booleanOrNull(ownerAnswers[entry.key]) ?? agent)
    return [
      {
        key: entry.key,
        title: entry.title,
        kind,
        weight: numberOrNull(entry.weight),
        agent,
        reason: textOrNull(reasons[entry.key]),
        owner,
        changed: owner !== null && agent !== null && owner !== agent,
      },
    ]
  })
  const classes = [
    ...judged.filter((entry) => entry.kind === 'hard_block'),
    ...judged.filter((entry) => entry.kind === 'scored'),
  ]

  return {
    version: 2,
    commit: shaOrNull(declaration.commit),
    savedAt: timeOrNull(declaration.savedAt),
    checkedAt: timeOrNull(record(declaration, 'review').checkedAt),
    decidedAt: timeOrNull(declaration.decidedAt),
    threshold: numberOrNull(policy.threshold),
    ownersCanChangeAnswers: booleanOrNull(policy.ownersCanChangeAnswers),
    classes,
    found: classes.filter((entry) => entry.kind === 'hard_block' && entry.agent === true),
    reviewerScore: numberOrNull(declaration.reviewerScore),
    score: numberOrNull(declaration.score),
    reason: ROUTE_REASONS.find((reason) => reason === declaration.reason) ?? null,
    note: textOrNull(declaration.note),
  }
}

const LIST = new Intl.ListFormat('en', { type: 'conjunction' })

/** What the agent found, in the words History and the review panel share. */
export function agentFinding(declaration: ClassDeclaration): string {
  if (declaration.found.length > 0) return `${LIST.format(declaration.found.map((entry) => entry.title))} found (hard block)`
  if (declaration.reviewerScore !== null) return `No hard block · score ${declaration.reviewerScore}/100`
  return 'Review did not finish'
}

// --- the six-question declaration ---

/** The six questions, under their stored keys, in the order the owner answered them. */
const QUESTIONS: ReadonlyArray<readonly [key: string, label: string]> = [
  ['credentials_secrets', 'Credentials / Secrets'],
  ['health_data', 'Health Data'],
  ['personal_information', 'Personal Information (PII)'],
  ['financial_data', 'Financial Data'],
  ['confidential_business_data', 'Confidential Business Data'],
  ['public_data', 'Public Data'],
]

/** One stored verdict of the automatic check. */
export type ReviewVerdict = 'yes' | 'no' | 'unanswered'

/** What the merge put on record for one question, in plain language. An unrecognised value is
 *  dropped rather than shown raw — a snake_case token is not an explanation. */
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

export interface QuestionDeclaration {
  version: 1
  /** False for a row queued without a declaration. */
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

function verdictOrNull(value: unknown): ReviewVerdict | null {
  return value === 'yes' || value === 'no' || value === 'unanswered' ? value : null
}

/** An empty reading — what a row with no declaration produces, and what the screen turns
 *  into "this app's declaration is unavailable" rather than six blank rows. */
const NOTHING: QuestionDeclaration = {
  version: 1,
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

function readQuestions(declaration: Record<string, unknown> | null): QuestionDeclaration {
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
  // The drift block. Absent on the ordinary path — its PRESENCE is the signal that this
  // queue item was routed by the pipeline after a save, with nobody at the form.
  const answeredAbout = shaOrNull(record(declaration, 'drift').answeredAbout)

  const disputes: DisputedCategory[] = []
  const answers: CitizenAnswer[] = []

  for (const [key, label] of QUESTIONS) {
    const citizenYes = booleanOrNull(citizenAnswers[key])
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

  return {
    version: 1,
    present: true,
    shippingCommit,
    reviewedCommit,
    noReviewAtAll: reviewedCommit === null,
    answeredAbout,
    // Read from the `drift` block, not the `commits` pair: the writer set `reviewed` from the
    // same commit as `shipping`, so that pair is only ever equal or half-null.
    drift: answeredAbout !== null && shippingCommit !== null && answeredAbout !== shippingCommit,
    disputes,
    citizenAnswers: answers,
    explanation: textOrNull(citizen.explanation),
  }
}

export type Declaration = ClassDeclaration | QuestionDeclaration

/** Narrow one stored declaration: `null` for a row queued without one, and otherwise a document
 *  this module is the only reader of. Never throws. */
export function readDeclaration(declaration: Record<string, unknown> | null): Declaration {
  return declaration !== null && declaration.version === 2 ? readClasses(declaration) : readQuestions(declaration)
}

/** Moved to `utils/shortSha.ts` and re-exported here so this module's consumers keep their
 *  one import. It left because it stopped being an admin detail: the two citizen surfaces
 *  that disagreed with it — showing 12 where this screen showed 7 — are retired, and the
 *  one publish chip that replaced them shares this. */
export { shortSha } from '../../utils/shortSha'
