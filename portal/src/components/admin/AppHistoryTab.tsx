import { useId, useState } from 'react'
import { ExternalLink } from 'lucide-react'
import type {
  AppHistory,
  HistoryAttempt,
  HistoryDecision,
  HistoryEvent,
  HistoryVersion,
  VersionState,
} from '../../utils/appRegistryApi'
import { assertNever } from '../../utils/assertNever'
import { dayMonth, dayMonthTime } from '../../utils/projectDates'
import { cn } from '../../lib/utils'
import { Alert, AlertDescription } from '../ui/alert'
import { Badge } from '../ui/badge'
import { Card } from '../ui/card'
import { BusyGlyph } from '../ui/Waiting'
import AnswersTable from './AnswersTable'
import { auditLabel } from './auditLabels'
import { handle } from './columns'
import { agentFinding, readDeclaration, shortSha, verdictWord } from './declaration'
import type { QuestionDeclaration } from './declaration'

const STATE_BADGE: Record<VersionState, { label: string; className: string }> = {
  waiting: { label: 'Waiting for review', className: 'border-amber-600/25 bg-amber-50 text-amber-700' },
  live: { label: 'Live', className: 'border-emerald-600/20 bg-emerald-50 text-emerald-700' },
  replaced: { label: 'Replaced', className: 'border-slate-600/20 bg-slate-100 text-slate-600' },
  taken_offline: { label: 'Taken offline', className: 'border-slate-600/20 bg-slate-100 text-slate-600' },
  publishing: { label: 'Publishing', className: 'border-primary/25 bg-[#F0F9FA] text-primary' },
  publish_failed: { label: 'Publish failed', className: 'border-red-700/20 bg-red-50 text-red-700' },
  not_published: { label: 'Not published', className: 'border-gray-500/20 bg-surface-muted text-neutral' },
  rejected: { label: 'Rejected', className: 'border-red-700/20 bg-red-50 text-red-700' },
  withdrawn: { label: 'Withdrawn', className: 'border-gray-500/20 bg-surface-muted text-neutral' },
  not_recorded: { label: 'Not recorded', className: 'border-gray-500/20 bg-surface-muted text-neutral' },
}

const ORDINALS = ['first', 'second', 'third', 'fourth', 'fifth']

const failedWords = (attempt: HistoryAttempt): string =>
  attempt.failureCode === 'build_failed' ? 'failed to build' : 'failed'

function decisionText(decision: HistoryDecision): string {
  const by = decision.by === null ? '' : ` by ${handle(decision.by)}`
  switch (decision.kind) {
    case 'published':
      return 'Published by itself (under the threshold)'
    case 'approved':
      return `Approved${by}${decision.at === null ? '' : `, ${dayMonthTime(decision.at)}`}`
    case 'rejected':
      return `Rejected${by}${decision.at === null ? '' : `, ${dayMonth(decision.at)}`}${
        decision.note === null ? ' · note not recorded' : `: "${decision.note}"`
      }`
    case 'withdrawn':
      return `Withdrawn${by}${decision.at === null ? '' : `, ${dayMonthTime(decision.at)}`}`
    case 'waiting':
      return 'Waiting for review'
    case 'not_recorded':
      return 'Not recorded'
    default:
      return assertNever(decision.kind)
  }
}

/** How many failed attempts came before the first that succeeded; nothing when it was the first. */
function retriedText(attempts: HistoryAttempt[], first: number): string {
  if (first === 0) return ''
  const failed = attempts.slice(0, first)
  const which = failed.length === 1 ? `the first ${failedWords(failed[0])}` : `the first ${failed.length} failed`
  return `, on the ${ORDINALS[first] ?? `number ${first + 1}`} attempt (${which})`
}

/** What became of a published version since: replaced, taken offline, or nothing yet. */
function afterText(version: HistoryVersion): string {
  if (version.replacedBy !== null && version.replacedAt !== null) {
    return ` · replaced by v${version.replacedBy} on ${dayMonth(version.replacedAt)}`
  }
  if (version.state === 'taken_offline') return ' · no longer live'
  return ''
}

/** When the version went live and what replaced it, or where its publishing stands. Null when
 *  nothing was ever to be published, as for a version rejected or withdrawn. */
function publishedText(version: HistoryVersion): string | null {
  const { attempts } = version
  const first = attempts.findIndex((attempt) => attempt.status === 'succeeded')
  if (first >= 0 && version.publishedAt !== null) {
    return `${dayMonthTime(version.publishedAt)}${retriedText(attempts, first)}${afterText(version)}`
  }
  const last = attempts.at(-1)
  if (last?.status === 'running') return `Not yet: publishing since ${dayMonthTime(last.startedAt)}`
  if (last?.status === 'failed') return `Not yet: the attempt on ${dayMonthTime(last.startedAt)} ${failedWords(last)}`
  if (version.state === 'not_published') return 'Not yet'
  if (version.decision.kind === 'published') return 'Not recorded'
  return null
}

type Tone = 'yes' | 'changed' | 'rest'

const CHIP_TONE: Record<Tone, string> = {
  yes: 'bg-red-50 text-red-900',
  changed: 'bg-amber-50 text-amber-800',
  rest: 'bg-slate-100 text-slate-600',
}

/** A six-question version's answers: the Yes answers, then where the agent and the owner
 *  disagreed, then how many questions were No. */
function answerChips(read: QuestionDeclaration): { tone: Tone; text: string }[] {
  if (read.citizenAnswers.length === 0 && read.disputes.length === 0) {
    return [{ tone: 'rest', text: 'Answers not recorded' }]
  }
  const disputed = new Set(read.disputes.map((row) => row.key))
  const settled = read.citizenAnswers.filter((row) => !disputed.has(row.key))
  const noCount = settled.filter((row) => !row.yes).length
  return [
    ...settled.filter((row) => row.yes).map((row) => ({ tone: 'yes' as const, text: `${row.label} · Yes` })),
    ...read.disputes.map((row) => ({
      tone: 'changed' as const,
      text: `${row.label} · agent ${verdictWord(row.reviewVerdict, 'history')}, owner ${
        row.citizenYes === null ? '—' : row.citizenYes ? 'Yes' : 'No'
      }`,
    })),
    ...(noCount === 0 ? [] : [{ tone: 'rest' as const, text: `${noCount} other ${noCount === 1 ? 'class' : 'classes'} No` }]),
  ]
}

function Row({ label, children }: { label: string; children: string }) {
  return (
    <div className="grid grid-cols-[110px_minmax(0,1fr)] gap-2.5 py-[3px] text-[12.5px]">
      <div className="text-neutral">{label}</div>
      <div className="text-primary-900">{children}</div>
    </div>
  )
}

function VersionCard({ version }: { version: HistoryVersion }) {
  const [open, setOpen] = useState(false)
  const answersId = useId()
  const live = version.state === 'live'
  const badge = STATE_BADGE[version.state]
  const published = publishedText(version)
  const read = readDeclaration(version.declaration)
  return (
    <Card
      data-testid={`version-${version.number}`}
      className={cn(
        'rounded-[10px] border-bial-border bg-white px-3.5 py-3 shadow-none',
        live && 'border-transparent bg-[#F7FDFA] shadow-[inset_0_0_0_1px_rgba(5,150,105,0.35)]',
      )}
    >
      <div className="mb-1.5 flex items-center gap-2.5">
        <span className="text-sm font-extrabold text-tertiary">v{version.number}</span>
        <code className="rounded bg-[#EEF2F6] px-[5px] py-px text-xs">{shortSha(version.commitSha)}</code>
        <Badge variant="outline" className={cn('px-[9px]', badge.className)}>
          {badge.label}
        </Badge>
        <span className="flex-grow" />
        <button
          type="button"
          aria-expanded={open}
          aria-controls={answersId}
          onClick={() => setOpen((was) => !was)}
          className="text-xs font-semibold text-primary transition hover:text-primary-dark focus-visible:underline focus-visible:outline-none"
        >
          Answers
        </button>
      </div>
      <Row label="Sent">
        {`${dayMonthTime(version.sentAt)}${version.sentBy === null ? '' : ` by ${handle(version.sentBy)}`}`}
      </Row>
      {read.version === 2 && <Row label="Agent">{agentFinding(read)}</Row>}
      <Row label="Decision">{decisionText(version.decision)}</Row>
      {published !== null && <Row label="Published">{published}</Row>}
      {open && (
        <div id={answersId} className="mt-2 border-t border-dashed border-bial-border pt-2">
          {read.version === 2 ? (
            <AnswersTable classes={read.classes} />
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {answerChips(read).map(({ tone, text }) => (
                <span key={text} className={cn('rounded-md px-2 py-[3px] text-[11.5px]', CHIP_TONE[tone])}>
                  {text}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

function EventLine({ event }: { event: HistoryEvent }) {
  const by = event.by === null ? '' : ` by ${handle(event.by)}`
  const reenabled = event.reenabledAt === null ? '' : ` · re-enabled ${dayMonthTime(event.reenabledAt)}`
  return (
    <div data-testid={`event-${event.action}`} className="flex items-center gap-2.5 px-3.5 py-0.5 text-xs text-neutral">
      <span className="h-[7px] w-[7px] flex-shrink-0 rounded-full bg-slate-400" />
      <span className="text-primary-900">{`${auditLabel(event.action).title}${by}${reenabled}`}</span>
      <span className="flex-grow" />
      <span className="tabular-nums">{dayMonthTime(event.at)}</span>
    </div>
  )
}

function LiveNow({ history, versions }: { history: AppHistory; versions: HistoryVersion[] }) {
  const { live, liveUrl } = history
  if (live === null) return null
  const sent = versions[0]?.number ?? 0
  const first = versions.find((version) => version.number === 1)
  const facts = [
    live.since === null ? null : `Published ${dayMonthTime(live.since)}`,
    sent === 0
      ? null
      : `${sent} ${sent === 1 ? 'version' : 'versions'} sent for publishing${first ? ` since ${dayMonth(first.sentAt)}` : ''}`,
  ].filter((fact): fact is string => fact !== null)
  return (
    <div
      data-testid="history-live-now"
      className="flex items-center gap-3.5 rounded-[10px] bg-emerald-50 px-3.5 py-[11px] shadow-[inset_0_0_0_1px_rgba(5,150,105,0.2)]"
    >
      <div className="flex-grow">
        <div className="text-[13px] font-bold text-emerald-800">
          Live now: {live.number === null ? '' : `v${live.number} · `}
          {shortSha(live.commitSha)}
        </div>
        {facts.length > 0 && <div className="mt-0.5 text-[11.5px] text-emerald-800">{facts.join(' · ')}</div>}
      </div>
      {liveUrl !== null && (
        <a
          href={liveUrl}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-[5px] text-[12.5px] font-semibold text-primary hover:text-primary-dark"
        >
          Open live app <ExternalLink size={13} aria-hidden />
        </a>
      )}
    </div>
  )
}

/** The app's History: the version live now, then every version sent for publishing and the app's
 *  other events between them, newest first. */
export default function AppHistoryTab({ history, error }: { history: AppHistory | null; error: string | null }) {
  if (error !== null) return <p className="text-sm text-red-600">{error}</p>
  if (history === null) {
    return (
      <p className="flex items-center gap-2 text-sm text-neutral">
        <BusyGlyph size={14} /> Loading history…
      </p>
    )
  }
  const versions = history.entries.filter((entry): entry is HistoryVersion => entry.kind === 'version')
  return (
    <>
      {history.truncated && (
        <Alert className="rounded-xl border-bial-border bg-bial-bg px-3 py-2.5">
          <AlertDescription className="text-xs text-neutral">
            Only the newest records are read, so the oldest versions and events are not shown.
          </AlertDescription>
        </Alert>
      )}
      <LiveNow history={history} versions={versions} />
      {history.entries.length === 0 && (
        <p className="text-sm text-neutral">Nothing has been sent for publishing yet.</p>
      )}
      {history.entries.map((entry) =>
        entry.kind === 'version' ? (
          <VersionCard key={`v${entry.number}`} version={entry} />
        ) : (
          <EventLine key={`${entry.action}-${entry.at}`} event={entry} />
        ),
      )}
      <p className="text-[11.5px] text-neutral">
        Versions are the ones sent for publishing, numbered in order. Other events on the app sit between them by date.
        Saves that were never sent do not appear.
      </p>
    </>
  )
}
