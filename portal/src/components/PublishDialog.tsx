/**
 * The owner's publish dialog: the version being published, the reviewer's answers on every active
 * class, the owner's corrections where the policy allows them, a live score, and a required note
 * whenever the send goes to an administrator.
 *
 * Opening it asks for a review of the saved version, and the dialog polls while one runs. The
 * score here only decides the label and whether the note shows: the server re-reads the stored
 * review and decides. When it disagrees (`note_required`), the dialog re-reads the review, starts
 * a fresh one if the classes changed since it ran, and otherwise asks for the note.
 *
 * An owner sees each class by its title alone. A class description is the reviewer's instruction,
 * and it never reaches this screen.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Lock, X } from 'lucide-react'

import { Alert, AlertDescription, AlertTitle } from './ui/alert'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from './ui/dialog'
import { Label } from './ui/label'
import { Textarea } from './ui/textarea'
import { ToggleGroup, ToggleGroupItem } from './ui/toggle-group'
import { BusyGlyph, WaitingLine } from './ui/Waiting'
import { cn } from '../lib/utils'
import { ApiError } from '../utils/apiError'
import {
  ensureClassificationReview,
  getClassificationReview,
  type ClassificationReview,
  type ReviewClass,
} from '../utils/classificationApi'
import { classificationScore, countedAnswers } from '../utils/classificationScore'
import {
  MAX_NOTE,
  noteRequiredReason,
  SNAPSHOT_MOVED,
  type DeploymentView,
  type NoteRequiredReason,
  type PublishAnswers,
} from '../utils/deployApi'
import { getProject } from '../utils/projectApi'
import { dayMonth, dayMonthTime, timeOfDay } from '../utils/projectDates'
import { canBeRestarted } from '../utils/publishPresentation'
import { shortSha } from '../utils/shortSha'

const REVIEW_POLL_MS = 5000

const WAITING_COPY =
  'We’re checking your saved app for the kinds of data it handles. This usually takes ' +
  'about 20 seconds — sometimes up to a minute. You can close this and come back; the ' +
  'result is kept for this version.'
const NOTHING_SAVED_COPY = 'There’s nothing saved to check yet — press Save first.'
const SAVED_AGAIN_COPY =
  'This app was saved again while the check was running, so this check no longer applies ' +
  'to the version you are publishing.'
const OUT_OF_DATE_COPY = 'This check no longer applies to the version you are publishing.'

const sectionLabel = 'text-[10.5px] font-bold uppercase tracking-[0.6px] text-neutral'
const chip = 'flex-shrink-0 border-0 py-[3px]'

/** `asking` is the POST in flight; `unreachable` is a failure of a request itself, with no stored
 *  review behind it. */
type ReviewPhase =
  | { kind: 'asking' }
  | { kind: 'ready'; review: ClassificationReview }
  | { kind: 'unreachable'; message: string; retryable: boolean }

function unreachableFrom(err: unknown): ReviewPhase {
  return {
    kind: 'unreachable',
    message: err instanceof ApiError ? err.message : 'We couldn’t reach the server. Please try again.',
    // A 503 or a network blip is worth a re-check; a 4xx cannot change by asking again.
    retryable: !(err instanceof ApiError) || err.status >= 500,
  }
}

/** A settled answer about an earlier version or earlier class definitions. */
function outOfDate(review: ClassificationReview): boolean {
  return review.status !== 'running' && review.status !== 'nothing_to_review' && !review.current
}

function sameDay(a: string, b: string): boolean {
  return new Date(a).toDateString() === new Date(b).toDateString()
}

function savedLine(review: ClassificationReview | null, answered: boolean): string | null {
  if (review?.savedAt == null) return null
  const saved = `Saved ${dayMonthTime(review.savedAt)}`
  const checked = answered ? review.checkedAt : null
  if (checked === null) return saved
  return `${saved} · checked ${sameDay(checked, review.savedAt) ? timeOfDay(checked) : dayMonthTime(checked)}`
}

interface LiveNow {
  sha: string | null
  label: string
  detail: string | null
}

function liveNow(deployment: DeploymentView | null): LiveNow {
  if (deployment === null || deployment.deploymentId === null) {
    return { sha: null, label: 'Not live yet', detail: 'This will be the first published version' }
  }
  const serving =
    canBeRestarted(deployment.publishState) ||
    (deployment.status === 'succeeded' && deployment.unpublishedAt === null)
  if (serving && deployment.headSha !== null) {
    return {
      sha: deployment.headSha,
      label: '',
      detail: deployment.finishedAt === null ? null : `Published ${dayMonth(deployment.finishedAt)}`,
    }
  }
  if (deployment.publishState === 'taken_offline') {
    return {
      sha: null,
      label: 'Not live',
      detail: deployment.unpublishedAt === null ? null : `Taken offline ${dayMonth(deployment.unpublishedAt)}`,
    }
  }
  return { sha: null, label: 'Not live yet', detail: null }
}

interface ScorePanel {
  amber: boolean
  title: string
  detail: string
}

function scorePanel(threshold: number, owners: boolean, reason: NoteRequiredReason | null): ScorePanel {
  const record = owners
    ? "The reviewer's answers and yours are both kept on record."
    : "Answers are locked, so the reviewer's answers set the score."
  if (reason === 'over_threshold') {
    return { amber: true, title: `Over ${threshold}: this goes to an administrator`, detail: record }
  }
  if (reason === 'rejection_standing') {
    return {
      amber: true,
      title: 'This goes to an administrator',
      detail: 'An administrator sent this app back, so they check it again.',
    }
  }
  if (reason !== null) return { amber: true, title: 'This goes to an administrator', detail: record }
  if (threshold >= 100) {
    return { amber: false, title: 'Publishes by itself. No administrator needed.', detail: record }
  }
  return {
    amber: false,
    title: `Publishes by itself at ${threshold} or below`,
    detail: `No administrator needed. ${record}`,
  }
}

function Version({ sha }: { sha: string }): React.ReactElement {
  return (
    <>
      Version <code className="rounded bg-[#EEF2F6] px-[5px] py-px text-xs">{shortSha(sha)}</code>
    </>
  )
}

function Section({
  label,
  locked,
  children,
}: {
  label: string
  locked: boolean
  children: React.ReactNode
}): React.ReactElement {
  return (
    <section>
      <div className="mb-2 flex items-center gap-2">
        {locked && <Lock size={13} className="text-neutral" aria-hidden />}
        <h3 className={sectionLabel}>{label}</h3>
      </div>
      <ul className="overflow-hidden rounded-[10px] border border-bial-border">{children}</ul>
    </section>
  )
}

function ClassRow({
  entry,
  className,
  children,
  control,
}: {
  entry: ReviewClass
  className: string
  children?: React.ReactNode
  control: React.ReactNode
}): React.ReactElement {
  return (
    <li
      data-testid={`pd-class-${entry.key}`}
      className={cn('flex items-center gap-3.5 border-b border-bial-border px-3.5 last:border-b-0', className)}
    >
      <div className="min-w-0 flex-grow">
        <span className="text-[13.5px] font-semibold text-tertiary">{entry.title}</span>
        {children}
      </div>
      {control}
    </li>
  )
}

export interface PublishDialogProps {
  projectId: string
  /** The status read the opening surface already holds. The dialog shows what is live off it. */
  deployment: DeploymentView | null
  /** The administrator's note when the app sits rejected. It leads the dialog. */
  rejectionNote?: string | null
  /** Sends about `commitSha`, the version this dialog reviewed. A refusal throws, and the dialog
   *  renders it beside its own button. */
  onConfirm: (commitSha: string, send: PublishAnswers) => Promise<void>
  onCancel: () => void
}

export default function PublishDialog({
  projectId,
  deployment,
  rejectionNote = null,
  onConfirm,
  onCancel,
}: PublishDialogProps): React.ReactElement {
  const ids = useId()
  const [phase, setPhase] = useState<ReviewPhase>({ kind: 'asking' })
  const [ownerAnswers, setOwnerAnswers] = useState<Record<string, boolean>>({})
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Set only for a reason the readout cannot show: the server's own word that the send routes.
  const [serverReason, setServerReason] = useState<NoteRequiredReason | null>(null)
  const [appName, setAppName] = useState<string | null>(null)

  // The version the dialog asked about, latched from each POST. Polls and re-reads are filtered
  // against it, so the review a send is made from is always about this version.
  const askedShaRef = useRef<string | null>(null)
  // Bumped on unmount and on every fresh ask, so a stale response never paints over a newer one.
  const generation = useRef(0)
  useEffect(
    () => () => {
      generation.current += 1
    },
    [],
  )

  useEffect(() => {
    let alive = true
    getProject(projectId).then(
      (project) => {
        if (alive) setAppName(project.name || null)
      },
      // The title falls back to "your app": a name that cannot be read blocks nothing.
      () => undefined,
    )
    return () => {
      alive = false
    }
  }, [projectId])

  const ask = useCallback(async (): Promise<void> => {
    const mine = ++generation.current
    setPhase({ kind: 'asking' })
    setServerReason(null)
    try {
      const first = await ensureClassificationReview(projectId)
      if (generation.current !== mine) return
      askedShaRef.current = first.headSha
      setPhase({ kind: 'ready', review: first })
    } catch (err) {
      if (generation.current !== mine) return
      setPhase(unreachableFrom(err))
    }
  }, [projectId])

  useEffect(() => {
    void ask()
  }, [ask])

  // The interval fires without waiting for the previous request, so `tick` keeps a late
  // `running` from walking the dialog back over a newer `complete`.
  const polling = phase.kind === 'ready' && phase.review.status === 'running'
  useEffect(() => {
    if (!polling) return undefined
    const mine = generation.current
    let latest = 0
    let applied = 0
    const timer = window.setInterval(() => {
      const tick = ++latest
      void (async () => {
        try {
          const next = await getClassificationReview(projectId)
          if (generation.current !== mine || tick <= applied) return
          applied = tick
          if (next.reviewedSha !== askedShaRef.current) {
            setPhase({ kind: 'unreachable', message: SAVED_AGAIN_COPY, retryable: true })
          } else if (outOfDate(next)) {
            void ask()
          } else {
            setPhase({ kind: 'ready', review: next })
          }
        } catch (err) {
          if (generation.current !== mine || tick <= applied) return
          applied = tick
          setPhase(unreachableFrom(err))
        }
      })()
    }, REVIEW_POLL_MS)
    return () => window.clearInterval(timer)
  }, [polling, projectId, ask])

  const review = phase.kind === 'ready' ? phase.review : null
  const headSha = review?.headSha ?? null
  const classes = review?.classes ?? []
  const verdicts = review?.status === 'complete' && review.current ? review.verdicts : null
  const answered = verdicts !== null
  const outOfAttempts = review?.status === 'failed' && !review.retryable
  const owners = review?.policy.ownersCanChangeAnswers ?? false
  const threshold = review?.policy.threshold ?? 100

  const reviewerYes = (key: string): boolean => verdicts?.[key]?.verdict === 'yes'
  const reviewerAnswers = Object.fromEntries(classes.map((c) => [c.key, reviewerYes(c.key)]))
  const shownYes = (key: string): boolean => (owners ? (ownerAnswers[key] ?? reviewerYes(key)) : reviewerYes(key))
  const hardBlocks = classes.filter((c) => c.kind === 'hard_block')
  const scored = classes.filter((c) => c.kind === 'scored')
  const blocked = hardBlocks.some((c) => reviewerYes(c.key))
  const score = classificationScore(
    countedAnswers({ reviewer: reviewerAnswers, owner: ownerAnswers, ownersCanChangeAnswers: owners }),
    classes,
  )

  // The server's decision table, in its order, as far as this readout can see it.
  let reason: NoteRequiredReason | null = null
  if (outOfAttempts) reason = 'review_unfinished'
  else if (answered) {
    if (blocked) reason = 'hard_block'
    else if (rejectionNote !== null || serverReason === 'rejection_standing') reason = 'rejection_standing'
    else if (score > threshold) reason = 'over_threshold'
    else reason = serverReason
  }

  const noteBlank = note.trim() === ''
  const canSend =
    !busy && headSha !== null && (answered || outOfAttempts) && (reason === null || !noteBlank)
  const name = appName ?? 'your app'
  const panel = answered && !blocked ? scorePanel(threshold, owners, reason) : null

  let subtitle: string | null = null
  if (blocked) {
    subtitle = 'It handles data only an administrator can approve. Tell them why, then send it for review.'
  } else if (answered) {
    subtitle = owners
      ? 'The reviewer checked this version. Correct any answer it got wrong.'
      : 'The reviewer checked this version.'
  }

  let status: React.ReactNode = null
  if (phase.kind === 'asking' || review?.status === 'running') {
    status = <WaitingLine label={WAITING_COPY} />
  } else if (phase.kind === 'unreachable') {
    status = phase.message
  } else if (review?.status === 'failed') {
    status = review.failureMessage
  } else if (review?.status === 'nothing_to_review') {
    status = NOTHING_SAVED_COPY
  } else if (!answered) {
    status = OUT_OF_DATE_COPY
  }
  const recheckOffered =
    (phase.kind === 'unreachable' && phase.retryable) || (review?.status === 'failed' && review.retryable)

  const live = liveNow(deployment)
  const saved = savedLine(review, answered)

  const reread = async (refusal: NoteRequiredReason, message: string): Promise<void> => {
    const mine = generation.current
    try {
      const next = await getClassificationReview(projectId)
      if (generation.current !== mine) return
      if (next.headSha !== askedShaRef.current || outOfDate(next)) {
        void ask()
        return
      }
      setPhase({ kind: 'ready', review: next })
      setServerReason(refusal === 'rejection_standing' || refusal === 'unknown' ? refusal : null)
      setError(message)
    } catch (err) {
      if (generation.current !== mine) return
      setPhase(unreachableFrom(err))
    }
  }

  const send = async (): Promise<void> => {
    if (!canSend || headSha === null) return
    setBusy(true)
    setError(null)
    const answers =
      answered && owners && !blocked ? Object.fromEntries(scored.map((c) => [c.key, shownYes(c.key)])) : {}
    try {
      await onConfirm(headSha, { answers, note: reason === null ? null : note.trim() })
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Could not publish. Please try again.'
      const refusal = noteRequiredReason(err)
      if (refusal !== null) {
        await reread(refusal, message)
      } else {
        setError(message)
        if (err instanceof ApiError && err.code === SNAPSHOT_MOVED) {
          // The answers were about a version that is no longer the saved one; the note stays.
          setOwnerAnswers({})
          void ask()
        }
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !busy) onCancel()
      }}
    >
      <DialogContent
        hideClose
        data-testid="publish-dialog"
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] max-w-[720px] flex-col gap-0 rounded-2xl border-0 bg-white p-0 font-manrope leading-[normal] shadow-2xl sm:rounded-2xl"
        {...(subtitle === null ? { 'aria-describedby': undefined } : {})}
      >
        <div className="flex items-start justify-between gap-3 border-b border-bial-border px-6 pb-4 pt-5">
          <div>
            <DialogTitle className="text-[17px] font-bold leading-[normal] tracking-normal text-tertiary">
              {blocked ? `${name} needs an administrator` : `Publish ${name}`}
            </DialogTitle>
            {subtitle !== null && (
              <DialogDescription className="mt-1 text-[12.5px] leading-normal text-neutral">
                {subtitle}
              </DialogDescription>
            )}
          </div>
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            aria-label="Close"
            className="flex h-[30px] w-[30px] flex-shrink-0 items-center justify-center rounded-lg text-neutral transition hover:bg-bial-bg hover:text-tertiary disabled:opacity-50"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto px-6 pb-2 pt-4">
          {rejectionNote !== null && (
            <section
              aria-labelledby={`${ids}-rejection`}
              data-testid="pd-rejection-note"
              className="rounded-[10px] border border-red-200 bg-red-50 px-3.5 py-2.5"
            >
              <h3 id={`${ids}-rejection`} className="text-[12.5px] font-bold text-red-800">
                An administrator sent this back
              </h3>
              <p className="mt-1 whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-red-800">
                {rejectionNote}
              </p>
            </section>
          )}

          <div
            data-testid="pd-version"
            className="grid grid-cols-2 rounded-[10px] border border-bial-border bg-surface-muted"
          >
            <div className="border-r border-bial-border px-3.5 py-2.5">
              <p className={sectionLabel}>Publishing</p>
              <p className="mt-[3px] text-[13px] font-semibold text-tertiary">
                {review?.headSha ? <Version sha={review.headSha} /> : '—'}
              </p>
              {saved !== null && <p className="mt-0.5 text-[11.5px] text-neutral">{saved}</p>}
            </div>
            <div className="px-3.5 py-2.5">
              <p className={sectionLabel}>Live now</p>
              <p className="mt-[3px] text-[13px] font-semibold text-tertiary">
                {live.sha !== null ? <Version sha={live.sha} /> : live.label}
              </p>
              {live.detail !== null && <p className="mt-0.5 text-[11.5px] text-neutral">{live.detail}</p>}
            </div>
          </div>

          <div
            className={cn(
              status === null
                ? 'sr-only'
                : 'rounded-[10px] border border-bial-border px-3.5 py-3 text-[12.5px] leading-relaxed text-neutral',
            )}
          >
            {/* One polite region, mounted throughout, so the wait, the arrival and a failure are
                all announced by its text changing. */}
            <div data-testid="pd-status" role="status" aria-live="polite">
              {status ?? 'The reviewer checked this version.'}
            </div>
            {recheckOffered && (
              <button
                type="button"
                data-testid="pd-recheck"
                disabled={busy}
                onClick={() => void ask()}
                className="mt-2 block text-[12.5px] font-semibold text-primary hover:underline disabled:opacity-50"
              >
                Check again
              </button>
            )}
          </div>

          {answered && hardBlocks.length > 0 && (
            <Section label="Checked by the reviewer · you can't change these" locked>
              {hardBlocks.map((entry) => {
                const found = reviewerYes(entry.key)
                return (
                  <ClassRow
                    key={entry.key}
                    entry={entry}
                    className={cn('py-2.5', found && 'bg-red-50')}
                    control={
                      <Badge
                        variant="outline"
                        className={cn(chip, 'px-2.5', found ? 'bg-red-700 text-white' : 'bg-slate-100 text-slate-600')}
                      >
                        {found ? 'Found' : 'Not found'}
                      </Badge>
                    }
                  >
                    {found && (
                      <p className="mt-[3px] whitespace-pre-wrap break-words text-xs leading-normal text-red-900">
                        {verdicts?.[entry.key]?.reason}
                      </p>
                    )}
                  </ClassRow>
                )
              })}
            </Section>
          )}

          {answered && !blocked && scored.length > 0 && (
            <Section
              label={owners ? 'Your answers' : 'Scored by the reviewer · locked by your administrator'}
              locked={!owners}
            >
              {scored.map((entry) => {
                const yes = shownYes(entry.key)
                const changed = owners && yes !== reviewerYes(entry.key)
                return (
                  <ClassRow
                    key={entry.key}
                    entry={entry}
                    className={cn(owners ? 'py-[9px]' : 'py-2.5', changed && 'bg-amber-50')}
                    control={
                      owners ? (
                        <ToggleGroup
                          type="single"
                          data-testid={`pd-toggle-${entry.key}`}
                          aria-label={entry.title}
                          value={yes ? 'yes' : 'no'}
                          disabled={busy}
                          onValueChange={(next) => {
                            if (next === 'yes' || next === 'no') {
                              setOwnerAnswers((prev) => ({ ...prev, [entry.key]: next === 'yes' }))
                            }
                          }}
                          className="flex-shrink-0 gap-0 overflow-hidden rounded-lg border border-bial-border"
                        >
                          {(['no', 'yes'] as const).map((value) => (
                            <ToggleGroupItem
                              key={value}
                              value={value}
                              className="h-[30px] min-w-0 rounded-none bg-white px-3.5 text-[12.5px] font-semibold text-neutral hover:text-tertiary data-[state=on]:bg-primary data-[state=on]:text-white data-[state=on]:shadow-none"
                            >
                              {value === 'yes' ? 'Yes' : 'No'}
                            </ToggleGroupItem>
                          ))}
                        </ToggleGroup>
                      ) : (
                        <Badge
                          variant="outline"
                          className={cn(
                            chip,
                            'px-3',
                            yes
                              ? 'bg-amber-50 text-amber-700 shadow-[inset_0_0_0_1px_rgba(217,119,6,0.25)]'
                              : 'bg-slate-100 text-slate-600',
                          )}
                        >
                          {yes ? 'Yes' : 'No'}
                        </Badge>
                      )
                    }
                  >
                    {reviewerYes(entry.key) && (
                      <p className="mt-0.5 whitespace-pre-wrap break-words text-[11.5px] text-primary-900">
                        Reviewer: {verdicts?.[entry.key]?.reason}
                      </p>
                    )}
                    {changed && <span className="sr-only">changed by the owner</span>}
                  </ClassRow>
                )
              })}
            </Section>
          )}

          {panel !== null && (
            <Alert
              role="status"
              data-testid="pd-score"
              className={cn(
                'flex items-center gap-3.5 rounded-[10px] border-0 px-3.5 py-3 leading-[normal]',
                panel.amber
                  ? 'bg-amber-50 shadow-[inset_0_0_0_1px_rgba(217,119,6,0.25)]'
                  : 'bg-emerald-50 shadow-[inset_0_0_0_1px_rgba(5,150,105,0.2)]',
              )}
            >
              <div
                className={cn(
                  'flex items-baseline gap-0.5 tabular-nums',
                  panel.amber ? 'text-amber-700' : 'text-emerald-700',
                )}
              >
                <span className="sr-only">Score </span>
                <span className="text-2xl font-extrabold leading-none">{score}</span>
                <span className="text-xs font-bold">/100</span>
              </div>
              <div className={cn('flex-grow', panel.amber ? 'text-amber-800' : 'text-emerald-800')}>
                <AlertTitle className="mb-0 text-[13px] font-bold leading-[normal] tracking-normal">
                  {panel.title}
                </AlertTitle>
                <AlertDescription className="mt-0.5 text-[11.5px]">{panel.detail}</AlertDescription>
              </div>
            </Alert>
          )}

          {reason !== null && (
            <div>
              <Label
                htmlFor={`${ids}-note`}
                className="mb-1.5 block text-[12.5px] font-bold leading-[normal] text-tertiary"
              >
                {blocked ? 'Why does this app need this data?' : 'A note for the administrator'}{' '}
                <span className="font-semibold text-red-700">Required</span>
              </Label>
              <Textarea
                id={`${ids}-note`}
                data-testid="pd-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                disabled={busy}
                maxLength={MAX_NOTE}
                aria-required
                aria-describedby={blocked ? `${ids}-note-help` : undefined}
                placeholder={
                  blocked
                    ? 'For example: security asks us to keep an ID copy for every visitor entering airside, for 30 days.'
                    : 'For example: the Zoho connection only reads our own vendor list.'
                }
                className={cn(
                  'resize-none rounded-lg border-bial-border px-3 py-2.5 text-[13.5px] font-medium leading-[1.6] text-primary-900 shadow-none md:text-[13.5px]',
                  blocked ? 'h-[92px]' : 'h-16',
                )}
              />
              {blocked && (
                <p id={`${ids}-note-help`} className="mt-1.5 text-[11.5px] text-neutral">
                  The administrator reads this first. Once they approve, the app publishes by itself.
                </p>
              )}
            </div>
          )}

          {error !== null && (
            <p role="alert" data-testid="pd-error" className="text-xs text-danger">
              {error}
            </p>
          )}
        </div>

        <div className="mt-2.5 flex items-center justify-end gap-2.5 border-t border-bial-border px-6 py-3.5">
          <Button
            type="button"
            variant="outline"
            data-testid="pd-cancel"
            onClick={onCancel}
            disabled={busy}
            className="h-9 rounded-lg border-bial-border bg-white px-3.5 text-[13.5px] font-semibold text-primary-900 shadow-none"
          >
            Cancel
          </Button>
          <Button
            type="button"
            data-testid="pd-confirm"
            onClick={() => void send()}
            disabled={!canSend}
            className="h-9 rounded-lg px-[18px] text-[13.5px] font-semibold shadow-none"
          >
            {busy && <BusyGlyph size={14} />}
            {reason === null ? 'Publish' : 'Send for review'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
