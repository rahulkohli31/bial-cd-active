import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, PowerOff, RotateCcw } from 'lucide-react'
import { restartApp, takeAppDown } from '../../utils/deployApi'
import { ApiError } from '../../utils/apiError'
import { canBeRestarted, canBeTakenDown, RESTART_FAILED_CODES } from '../../utils/publishPresentation'
import { BusyGlyph } from '../ui/Waiting'
import AppStatusPanel from './AppStatusPanel'

/**
 * SETTINGS › PRODUCTION — where an application stands, and what an owner may do about it.
 *
 * WHERE IT STANDS IS `AppStatusPanel`'S TO SAY, and it is MOUNTED here rather than restated: the
 * state pill, the provenance rows, the published address with its copy control and Send for
 * review all come from that one component, off ONE deployment read. This file adds only what the
 * panel has no opinion about — restarting a live application and taking one out of production —
 * and it reads the state through the panel rather than asking the server a second time.
 *
 * RESTART RUNS THE SAME VERSION AGAIN, AND THE COPY HAS TO SAY SO. To a citizen who did not write
 * the code, "restart" is the appliance remedy for "my app is broken" — and it is not one: it
 * recycles the revision already serving, so if the fault is in the application's own logic the
 * restart is a guaranteed no-op that still costs a wait. Saying it plainly is what stops an owner
 * pressing a button that cannot help them.
 *
 * TAKE DOWN'S SENTENCE SAYS WHAT IS KEPT, not what is lost, because the fear it answers is "will
 * I lose my work". It removes a container. The application, its chats, its data and its files all
 * stay, and Publish again puts it back at the same address.
 */

type Pending = 'restart' | 'takedown' | null

export interface ProductionTabProps {
  projectId: string
  /** Told when an operation settles, so the row underneath can stop showing a stale chip. */
  onSettled?: () => void
}

export default function ProductionTab({ projectId, onSettled }: ProductionTabProps) {
  const [pending, setPending] = useState<Pending>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const alive = useRef(true)
  /**
   * WHAT ACTUALLY HOLDS THE DOOR, as opposed to what draws the label.
   *
   * `pending` is state, and state is read through the closure of the render that created the
   * handler — so two presses inside one frame both see `null` and both start a request. A ref is
   * read and written synchronously, which is the only thing that can settle a race decided before
   * React re-renders. The state beside it exists to say "Restarting…" and nothing else.
   */
  const inFlight = useRef<Pending>(null)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const act = useCallback(
    (which: Exclude<Pending, null>, refresh: () => Promise<void>) => {
      // A SECOND PRESS WHILE THE FIRST IS IN FLIGHT DOES NOTHING. The server treats a second
      // restart as a no-op on the same operation, which is exactly why the interface must not
      // accept the press: a click that silently does nothing is the failure this plan avoids
      // everywhere else.
      if (inFlight.current !== null) return
      inFlight.current = which
      setRefusal(null)
      setPending(which)
      void (async () => {
        try {
          if (which === 'restart') await restartApp(projectId)
          else await takeAppDown(projectId)
          await refresh()
        } catch (err) {
          // THE SERVER'S STATED REASON, NOT A GENERIC FAILURE. Every refusal on these two routes
          // names something the owner can act on — publish it again, wait for the deploy to
          // finish, ask an administrator — and flattening them into "something went wrong" throws
          // that away.
          if (!alive.current) return
          setRefusal(err instanceof ApiError ? err.message : 'That did not work. Try again.')
        } finally {
          // Cleared whether or not this component is still mounted: the ref outlives a tab hop,
          // and leaving it set would refuse every press on the next mount for ever.
          inFlight.current = null
          if (alive.current) setPending(null)
          onSettled?.()
        }
      })()
    },
    [projectId, onSettled],
  )

  return (
    <div data-testid="production-tab" className="flex flex-col gap-5">
      <AppStatusPanel
        projectId={projectId}
        actions={({ state, failureCode, hasServingRow, refresh }) => (
          <>
            {/* A RESTART THAT FAILED ON AN APPLICATION THAT IS STILL SERVING. The status
                above says Live, and it is right — the previous revision never stopped. But
                the attempt ended, and saying nothing would leave an owner who pressed
                Restart four minutes ago with no idea whether it ever finished. The state
                word is not the place for it: that would be the screen claiming the
                application is down while the list beside it shows it up. */}
            {failureCode !== null && RESTART_FAILED_CODES.has(failureCode) && canBeRestarted(state) && (
              <p
                data-testid="production-restart-failed"
                className="mt-3 flex items-start gap-2 rounded-xl bg-status-amber-bg p-3 text-xs text-status-amber-fg"
              >
                <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
                The last restart did not finish. The version that was already running is
                still running, so nothing was lost — you can try again.
              </p>
            )}
            {refusal !== null && (
              <p
                role="alert"
                data-testid="production-refusal"
                className="mt-3 flex items-start gap-2 rounded-xl border border-danger/30 bg-red-50/50 p-3 text-xs text-danger"
              >
                <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
                {refusal}
              </p>
            )}

            {/* EACH ACTION IS OFFERED WHERE THE SERVER WOULD ACCEPT IT, and the two are not
                accepted on the same grounds — see `canBeTakenDown`. A control that exists in
                order to be refused teaches a citizen to distrust the screen; a control WITHHELD
                where the server would have accepted it is the same lesson learned the other way
                round, and it is the one an owner cannot work around. */}
            {(canBeRestarted(state) || canBeTakenDown(state, hasServingRow)) && (
              <div className="mt-4 flex flex-col gap-3 border-t border-bial-border pt-4">
                {canBeRestarted(state) && (
                <div>
                  <button
                    type="button"
                    data-testid="production-restart"
                    aria-disabled={pending !== null}
                    onClick={() => act('restart', refresh)}
                    className="inline-flex items-center gap-2 rounded-xl border border-bial-border px-3 py-2 text-xs font-semibold text-primary-900 transition hover:bg-surface-muted aria-disabled:opacity-50"
                  >
                    {pending === 'restart' ? <BusyGlyph size={14} /> : <RotateCcw size={14} />}
                    {pending === 'restart' ? 'Restarting…' : 'Restart'}
                  </button>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-neutral">
                    Runs the same version again at the same address. It does not pick up anything
                    you have saved since.
                  </p>
                </div>
                )}

                {canBeTakenDown(state, hasServingRow) && (
                <div>
                  <button
                    type="button"
                    data-testid="production-takedown"
                    aria-disabled={pending !== null}
                    onClick={() => act('takedown', refresh)}
                    className="inline-flex items-center gap-2 rounded-xl border border-danger/40 px-3 py-2 text-xs font-semibold text-danger transition hover:bg-red-50 aria-disabled:opacity-50"
                  >
                    {pending === 'takedown' ? <BusyGlyph size={14} /> : <PowerOff size={14} />}
                    {pending === 'takedown' ? 'Taking it down…' : 'Take down'}
                  </button>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-neutral">
                    Stops serving it to BIAL staff. Its chats, its data and its files are all kept,
                    and Publish again puts it back at the same address.
                    {state === 'in_review' && ' The version waiting for review is untouched.'}
                  </p>
                </div>
                )}
              </div>
            )}
          </>
        )}
      />
    </div>
  )
}
