import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, PowerOff, RotateCcw } from 'lucide-react'
import { getDeployment, restartApp, takeAppDown } from '../../utils/deployApi'
import type { DeploymentView } from '../../utils/deployApi'
import { ApiError } from '../../utils/apiError'
import { presentationFor } from '../../utils/publishPresentation'
import { BusyGlyph } from '../ui/Waiting'
import { listDate } from '../../utils/projectDates'

/**
 * WHERE AN APPLICATION STANDS IN PRODUCTION, AND THE TWO THINGS AN OWNER CAN DO ABOUT IT.
 *
 * RESTART RUNS THE SAME VERSION AGAIN, AND THE COPY HAS TO SAY SO. To a citizen who did not write
 * the code, "restart" is the appliance remedy for "my app is broken" — and it is not one: it
 * recycles the revision already serving, so if the fault is in the application's own logic the
 * restart is a guaranteed no-op that still costs a wait. Saying it plainly is what stops an owner
 * pressing a button that cannot help them, and the failure states point at the remedy that does.
 *
 * TAKE DOWN'S CONFIRMATION SAYS WHAT IS KEPT, not what is lost, because the fear it answers is
 * "will I lose my work". It removes a container. The application, its chats, its data and its
 * files all stay, and Publish again puts it back at the same address.
 *
 * A FAILED RESTART IS NOT "DIDN'T START". The deployment read reports `did_not_start` for it,
 * which is the right word for a first deploy and the wrong one here — the previous version is
 * still serving. `failureCode` is what tells them apart, so this panel reads that rather than the
 * state word, the same way the portal already special-cases a routed submission.
 *
 * ONE POLLER AT A TIME. This panel polls only while an operation of its own is in flight, and
 * stops the moment it settles: the list is not a polling surface and must not become one.
 */

const RESTART_FAILED_CODES = new Set(['restart_failed', 'restart_not_ready'])

/** How often the deployment read is re-asked while an operation runs. The read is cheap and the
 *  operations take minutes; anything faster is spent on nothing. */
const POLL_MS = 3000

type Pending = 'restart' | 'takedown' | null

export interface ProductionTabProps {
  projectId: string
  /** Lets the panel start from what the dialog already knows, rather than blank. */
  initialDeployment?: DeploymentView | null
  /** Told when an operation settles, so the row underneath can stop showing a stale chip. */
  onSettled?: () => void
}

export default function ProductionTab({ projectId, initialDeployment = null, onSettled }: ProductionTabProps) {
  const [deployment, setDeployment] = useState<DeploymentView | null>(initialDeployment)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [pending, setPending] = useState<Pending>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const read = useCallback(async () => {
    try {
      const next = await getDeployment(projectId)
      if (!alive.current) return next
      setDeployment(next)
      setLoadError(null)
      return next
    } catch (err) {
      if (alive.current) {
        setLoadError(err instanceof ApiError ? err.message : 'Could not read the production status.')
      }
      return null
    }
  }, [projectId])

  useEffect(() => {
    void read()
  }, [read])

  // POLLING IS CONDITIONAL ON AN OPERATION OF OUR OWN, and it stops when the deployment stops
  // running. A panel that polled whenever it was open would keep a request in flight for as long
  // as somebody left a dialog on screen.
  useEffect(() => {
    if (pending === null) return undefined
    const timer = setInterval(() => {
      void (async () => {
        const next = await read()
        if (next !== null && next.status !== 'running') {
          setPending(null)
          onSettled?.()
        }
      })()
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [pending, read, onSettled])

  const state = deployment?.publishState ?? 'nothing_built'
  // A FAILED ATTEMPT, not a stale code. `failureCode` outlives the attempt that wrote it, so an
  // application whose last restart failed and which is serving again would otherwise be announced
  // as broken. The status is what says the attempt is the one still standing.
  // IT CORRECTS ONE WORD AND ONLY THAT WORD. `failureCode` outlives the attempt that wrote it,
  // and an administrator's lockout or a fresh submission outranks the deployment row server-side
  // — so keying off the code alone would shout "Could not restart" over "Switched off" and "In
  // review", both of which are the more current fact. `did_not_start` is the single state this
  // renames, because it is the single state a failed restart is spoken as wrongly.
  const restartFailed =
    state === 'did_not_start' &&
    deployment?.failureCode != null &&
    RESTART_FAILED_CODES.has(deployment.failureCode)
  const live = state === 'live_current' || state === 'live_newer_work' || state === 'live_drift_unknown'
  const busy = pending !== null || deployment?.status === 'running'

  const act = (which: Exclude<Pending, null>) => {
    // A SECOND PRESS WHILE THE FIRST IS IN FLIGHT DOES NOTHING. The server treats a second
    // restart as a no-op on the same operation, which is exactly why the interface must not
    // accept the press: a click that silently does nothing is the failure this plan avoids
    // everywhere else.
    if (busy) return
    setRefusal(null)
    setPending(which)
    void (async () => {
      try {
        if (which === 'restart') await restartApp(projectId)
        else await takeAppDown(projectId)
        await read()
        if (which === 'takedown') {
          setPending(null)
          onSettled?.()
        }
      } catch (err) {
        // THE SERVER'S STATED REASON, NOT A GENERIC FAILURE. Every refusal on these two routes
        // names something the owner can act on — publish it again, wait for the deploy to
        // finish, ask an administrator — and flattening them into "something went wrong" throws
        // that away.
        if (!alive.current) return
        setRefusal(err instanceof ApiError ? err.message : 'That did not work. Try again.')
        setPending(null)
      }
    })()
  }

  if (loadError !== null && deployment === null) {
    return (
      <div data-testid="production-error" className="rounded-2xl border border-danger/30 p-4">
        <p className="text-sm text-danger">{loadError}</p>
        <button
          type="button"
          onClick={() => void read()}
          className="mt-2 text-xs font-semibold text-primary hover:underline"
        >
          Try again
        </button>
      </div>
    )
  }

  const presentation = presentationFor(state)

  return (
    <div data-testid="production-tab" className="flex flex-col gap-5">
      <div>
        <p className="text-xs font-semibold text-neutral">Status</p>
        <p data-testid="production-state" className="mt-1 text-lg font-bold text-tertiary">
          {restartFailed ? 'Could not restart' : presentation.label}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-neutral">
          {restartFailed
            ? 'Your app did not come back up. A restart runs the same version again, so if it ' +
              'keeps failing the fault is in the app itself — describe the fix in a chat and ' +
              'send it for review.'
            : presentation.sentence}
        </p>
      </div>

      {deployment?.url !== null && deployment?.url !== undefined && live && (
        <div>
          <p className="text-xs font-semibold text-neutral">Address</p>
          <a
            data-testid="production-url"
            href={deployment.url}
            target="_blank"
            rel="noreferrer"
            className="mt-1 block truncate text-sm font-medium text-primary hover:underline"
          >
            {deployment.url}
          </a>
          {deployment.finishedAt !== null && (
            <p className="mt-1 text-[11px] text-neutral">Published {listDate(deployment.finishedAt)}</p>
          )}
        </div>
      )}

      {refusal !== null && (
        <p role="alert" data-testid="production-refusal" className="flex items-start gap-2 rounded-xl border border-danger/30 bg-red-50/50 p-3 text-xs text-danger">
          <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
          {refusal}
        </p>
      )}

      {/* THE TWO ACTIONS ARE OFFERED ONLY WHERE THE SERVER WOULD ACCEPT THEM. A control that
          exists to be refused teaches a citizen to distrust the screen. */}
      {live && (
        <div className="flex flex-col gap-3 border-t border-bial-border pt-4">
          <div>
            <button
              type="button"
              data-testid="production-restart"
              aria-disabled={busy}
              onClick={() => act('restart')}
              className="inline-flex items-center gap-2 rounded-xl border border-bial-border px-3 py-2 text-xs font-semibold text-primary-900 transition hover:bg-surface-muted aria-disabled:opacity-50"
            >
              {pending === 'restart' ? <BusyGlyph size={14} /> : <RotateCcw size={14} />}
              {pending === 'restart' ? 'Restarting…' : 'Restart'}
            </button>
            <p className="mt-1.5 text-[11px] leading-relaxed text-neutral">
              Runs the same version again at the same address. It does not pick up anything you
              have saved since.
            </p>
          </div>

          <div>
            <button
              type="button"
              data-testid="production-takedown"
              aria-disabled={busy}
              onClick={() => act('takedown')}
              className="inline-flex items-center gap-2 rounded-xl border border-danger/40 px-3 py-2 text-xs font-semibold text-danger transition hover:bg-red-50 aria-disabled:opacity-50"
            >
              {pending === 'takedown' ? <BusyGlyph size={14} /> : <PowerOff size={14} />}
              {pending === 'takedown' ? 'Taking it down…' : 'Take down'}
            </button>
            <p className="mt-1.5 text-[11px] leading-relaxed text-neutral">
              Stops serving it to BIAL staff. Its chats, its data and its files are all kept, and
              Publish again puts it back at the same address.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
