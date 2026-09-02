/**
 * THE HALF `beforeunload` CANNOT COVER (Plan F, U8).
 *
 * ═══ THE HONEST SCOPE, WHICH IS NARROWER THAN THE OBVIOUS FRAMING ═══
 *
 * There are already two guards. Plan A's hoisted `beforeunload` handler covers leaving the TAB, and
 * the reclaim dialog covers another project taking the workspace — it is already an in-place guard,
 * and it is server-driven. What is genuinely unguarded is an in-place navigation OUT of the
 * workspace — the navbar's links, the breadcrumb, opening a different project — while the app holds
 * unsaved work and no 409 is involved. `beforeunload` cannot fire for those, because a single-page
 * navigation is not an unload.
 *
 * ═══ THE ARMING RULE, AND THE FOURTH CASE THAT DECIDES WHETHER THIS IS A NUISANCE ═══
 *
 * `beforeunload` stays armed only on a definite `true`, and this unit does not change that: the
 * browser's prompt renders fixed text a page cannot supply a reason to, so arming it on "we could
 * not check" produces a prompt with nothing answerable behind it — which is how people learn to
 * dismiss prompts.
 *
 * An in-app dialog CAN carry a reason, so R62 changes the rule here and only here: it warns on
 * `null` too, saying the platform could not check. But `null` has TWO causes and they are not the
 * same:
 *
 *   1. the check ran and could not answer — a real "we could not tell";
 *   2. THE CHECK WAS NEVER ASKED. `fetchSaveState` compares the container's HEAD to the saved
 *      bundle's, and it may only be called on a live workspace — so on a stopped or never-built
 *      project the save state is permanently `null` because there is nothing to check.
 *
 * Warning on the second would fire "we could not tell whether you have unsaved work" on every exit
 * from every stopped project, which is exactly the prompt-with-nothing-behind-it the arming rule
 * exists to avoid. So: warn on `true`; warn on `null` ONLY while the workspace is alive; never on
 * `false`; never when the workspace is not running.
 *
 * ═══ WHY A CONFIRM-BEFORE-NAVIGATE AND NOT `useBlocker` ═══
 *
 * `useBlocker` needs a data router; the app is on `BrowserRouter`. Migrating the router to obtain
 * one hook is a large blast radius for the last plan in a set to ship, and the guard's real job —
 * the workspace's own exits — is served by an exit function the shell's chrome consults.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { saveProject } from '../../utils/buildSessionApi'

export interface UnsavedWorkGuardHandle {
  /**
   * Run `go` — unless there is something to lose, in which case ask first and run it only if the
   * person says so. Every in-place exit from the workspace routes through this ONE function; a
   * control that navigates directly is a control this guard cannot see.
   */
  guard: (go: () => void) => void
  dialog: React.ReactElement | null
}

export interface UnsavedWorkGuardOptions {
  /** TRI-STATE. `true` definitely dirty, `false` definitely clean, `null` no claim. */
  saveDirty: boolean | null
  /** Whether the workspace is running. A `null` from a stopped project means "never asked". */
  workspaceIsAlive: boolean
  /** The project a Save would write. `null` disables the save-then-leave arm, not the warning. */
  projectId: string | null
}

/** Is there anything a person could lose by leaving right now? */
function worthWarningAbout(saveDirty: boolean | null, workspaceIsAlive: boolean): boolean {
  if (saveDirty === true) return true
  // `null` while ALIVE is a check that ran and could not answer — R62 says the platform says so.
  // `null` while not alive is a check nobody asked, which is not the same claim at all.
  return saveDirty === null && workspaceIsAlive
}

export function useUnsavedWorkGuard({
  saveDirty,
  workspaceIsAlive,
  projectId,
}: UnsavedWorkGuardOptions): UnsavedWorkGuardHandle {
  const [pending, setPending] = useState<(() => void) | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const guard = useCallback(
    (go: () => void) => {
      if (!worthWarningAbout(saveDirty, workspaceIsAlive)) {
        go()
        return
      }
      // Stored as a thunk INSIDE a setter callback: `setPending(go)` would call `go` immediately,
      // because React treats a function argument as an updater. The bug is silent — the navigation
      // simply happens, guard and all.
      setError(null)
      setPending(() => go)
    },
    [saveDirty, workspaceIsAlive],
  )

  const leave = useCallback(() => {
    const go = pending
    setPending(null)
    go?.()
  }, [pending])

  const saveThenLeave = useCallback(async () => {
    if (!projectId) return
    setSaving(true)
    setError(null)
    try {
      await saveProject(projectId)
      if (!mounted.current) return
      leave()
    } catch (err) {
      if (!mounted.current) return
      // A SAVE THAT FAILED MUST NOT LET THE NAVIGATION THROUGH. Leaving anyway after promising to
      // save first is the exact data loss this dialog exists to prevent, arriving through the door
      // marked "safe".
      setError(err instanceof Error ? err.message : 'Could not save your work. Try again.')
    } finally {
      if (mounted.current) setSaving(false)
    }
  }, [projectId, leave])

  const dialog = pending ? (
    <UnsavedWorkDialog
      certain={saveDirty === true}
      saving={saving}
      error={error}
      canSave={projectId !== null}
      onSaveAndLeave={() => void saveThenLeave()}
      onLeaveAnyway={leave}
      onStay={() => setPending(null)}
    />
  ) : null

  return { guard, dialog }
}

interface DialogProps {
  /** `true` = we know there are unsaved changes; `false` = we could not check and say so (R62). */
  certain: boolean
  saving: boolean
  error: string | null
  canSave: boolean
  onSaveAndLeave: () => void
  onLeaveAnyway: () => void
  onStay: () => void
}

/**
 * HAND-ROLLED, MATCHING `ReclaimWorkspaceDialog` — and the docblock says so because it used to
 * claim the opposite.
 *
 * What is actually here: `aria-modal` + `aria-labelledby`, Escape and overlay-click to stay (both
 * inert while a save is in flight, so nobody dismisses the dialog out from under their own write),
 * and initial focus parked on Stay. What is NOT here: a focus trap or a scroll lock, so Tab can
 * still walk out of the dialog into the page behind it.
 *
 * THAT GAP IS WORTH CLOSING and this is the guard it matters most on — it is the last thing
 * between somebody and their unsaved work, so it is the one that most has to be reliable under a
 * keyboard. `components/ui/dialog.tsx` (Radix) is the upgrade path and brings the trap and the
 * lock for free; `AttachmentPreview.tsx` is the worked example. Left as its own change because
 * swapping the primitive moves real behaviour, which is not what the commit introducing this note
 * was doing. `ReclaimWorkspaceDialog` stays the pattern for the COPY and the busy-window focus
 * park either way.
 */
function UnsavedWorkDialog({
  certain,
  saving,
  error,
  canSave,
  onSaveAndLeave,
  onLeaveAnyway,
  onStay,
}: DialogProps) {
  const stayRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    stayRef.current?.focus()
  }, [])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope"
      role="dialog"
      aria-modal="true"
      aria-labelledby="unsaved-work-title"
    >
      <div className="absolute inset-0 bg-black/40" onClick={saving ? undefined : onStay} />
      <div
        className="relative w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl focus:outline-none"
        onKeyDown={(e) => {
          if (e.key === 'Escape' && !saving) onStay()
        }}
      >
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-bial-bg">
            <AlertTriangle size={17} className="text-warning" />
          </div>
          <h3 id="unsaved-work-title" className="text-base font-bold text-tertiary">
            {certain ? 'Save your changes before you go?' : 'We could not check for unsaved changes'}
          </h3>
        </div>

        <p className="mt-3 text-sm leading-relaxed text-neutral">
          {certain
            ? 'This app has changes that are not saved yet. Save them and they come back exactly as you left them; leave without saving and they go.'
            : // R62: say that the platform could not tell, rather than reporting there is nothing
              // to lose. A wrong reassurance is the one answer that costs somebody their work.
              'We could not tell whether this app has unsaved changes. Saving first is the safe option.'}
        </p>

        {error && (
          <p role="alert" className="mt-3 text-sm leading-relaxed text-danger">
            {error}
          </p>
        )}

        <div className="mt-5 flex flex-col gap-2.5">
          {canSave && (
            <button
              type="button"
              disabled={saving}
              onClick={onSaveAndLeave}
              className="flex w-full items-center justify-center gap-2 rounded-xl bg-primary py-2.5 text-sm font-semibold text-white transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {saving ? <Loader2 size={15} className="animate-spin" /> : null} Save and leave
            </button>
          )}
          <button
            type="button"
            disabled={saving}
            onClick={onLeaveAnyway}
            className="w-full rounded-xl border border-bial-border py-2.5 text-sm font-semibold text-tertiary transition hover:bg-bial-bg disabled:cursor-not-allowed disabled:opacity-50"
          >
            Leave without saving
          </button>
          <button
            ref={stayRef}
            type="button"
            disabled={saving}
            onClick={onStay}
            className="w-full rounded-xl py-2 text-sm font-semibold text-neutral transition hover:text-tertiary disabled:opacity-50"
          >
            Stay here
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * THE ONE EXIT FUNCTION, provided by the shell and consulted by the workspace's chrome.
 *
 * A CONTEXT RATHER THAN A PROP, and for a reason that is structural rather than stylistic: the
 * exits are the navbar's links and the breadcrumb, which are not this guard's descendants by props
 * — the navbar is a sibling of the grid, and it is also rendered on pages that have no workspace at
 * all. Threading a prop would mean every one of those pages passing a guard it does not have.
 *
 * `null` OUTSIDE A WORKSPACE IS THE ORDINARY CASE, not an error. A navbar on the projects list has
 * nothing to guard, so `useWorkspaceExit` hands back a function that simply goes — which is what
 * keeps every other page's navigation unchanged by this unit.
 */
const WorkspaceExitContext = createContext<((go: () => void) => void) | null>(null)

export const WorkspaceExitProvider = WorkspaceExitContext.Provider

/** Run an exit through the workspace's guard, or straight through when there is none. */
export function useWorkspaceExit(): (go: () => void) => void {
  const guard = useContext(WorkspaceExitContext)
  return guard ?? runStraightThrough
}

const runStraightThrough = (go: () => void) => go()
