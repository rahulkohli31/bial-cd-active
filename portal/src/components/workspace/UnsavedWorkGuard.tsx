/**
 * WHY THIS EXISTS: the half `beforeunload` cannot cover.
 *
 * Two guards already exist: a hoisted `beforeunload` for leaving the TAB, and the
 * server-driven reclaim dialog for another project taking the workspace. Unguarded: an
 * in-place navigation OUT of the workspace (a navigation destination, the back control,
 * opening another project) while unsaved work exists and no 409 is involved — a same-page navigation is
 * not an unload, so `beforeunload` never fires for it.
 *
 * THE ARMING RULE. `beforeunload` stays armed only on a definite `true` — its prompt has
 * fixed text, so arming it on "we could not check" trains people to dismiss prompts. (It takes
 * the recovery carve-out below as well, and for the same reason; what it cannot take is the
 * `null` arm, because its fixed text cannot say "we could not check".) This
 * in-app dialog CAN carry a reason, so it also warns on `null` — but `null` has TWO causes:
 * (1) the check ran and could not answer, or (2) it was NEVER ASKED, because `fetchSaveState`
 * only runs on a live workspace, so a stopped/never-built project is permanently `null` with
 * nothing to check. Warning on case 2 would fire on every exit from every stopped project —
 * the exact prompt-with-nothing-behind-it the rule exists to avoid. So: warn on `true`; warn
 * on `null` ONLY while alive; never on `false`; never when not running — with the one exception
 * the next paragraph carves out of `true`.
 *
 * AND A `true` IS NOT ALWAYS SOMETHING TO LOSE. `dirty` answers "is there a saved VERSION of this
 * tree?", so THE BUILD ITSELF makes it true: a citizen who described an app, watched the platform
 * build it and then touched nothing arrives at `dirty: true, savedHead: null` — and this guard
 * stopped them on the way out over work they never did. Nothing was at risk. The platform writes
 * a RECOVERY copy of the tree at every turn boundary, and `SessionManager.newest_restore_source`
 * hands THAT copy, not the saved one, to every automatic restore. So a `true` the platform holds
 * a recovery copy of is precisely the prompt-with-nothing-behind-it the paragraph above refuses
 * to raise, and `recoveryAt` is the fact that tells the two apart. A `true` with NO recovery copy
 * is real unsaved work and still stops somebody, unchanged.
 *
 * WHAT THIS IS NOT: a claim that anything was saved. A recovery copy is not a version — `dirty`
 * stays true, the Save control stays where it is and does what it did, and Save remains the
 * citizen's own manual act. The only thing that changes is that leaving stops being treated as a
 * way to lose something the platform can put back. THE RULE IN FULL: warn on a `true` with no
 * recovery copy; warn on `null` ONLY while alive; never on `false`; never when not running.
 *
 * Uses confirm-before-navigate, not `useBlocker`: that needs a data router and the app is on
 * `BrowserRouter` — migrating for one hook is out of scope; the workspace's own exits are
 * served by an exit function the shell's chrome consults instead.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { AlertTriangle } from 'lucide-react'
import { BusyGlyph, useElapsedSeconds, ELAPSED_AFTER_MS } from '../ui/Waiting'
import { canBePutBack, saveProject } from '../../utils/buildSessionApi'

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
  /**
   * WHOSE WORK IS AT RISK, or `null` when the caller cannot say.
   *
   * "This app has changes that are not saved yet" is ambiguous the moment a citizen has more than
   * one project — and the two exits this dialog covers, the navigation and the back control, are
   * exactly the ones taken while thinking about a different app. Naming it costs one prop and
   * removes the ambiguity entirely.
   */
  projectName?: string | null
  /**
   * WHEN THE PLATFORM LAST WROTE A RECOVERY COPY of this app's tree (ISO-8601), or `null` when it
   * holds none. `null` is also the right answer when the caller cannot say: "no copy" is the
   * reading that keeps warning, and an absent fact must never be the reason somebody loses work.
   *
   * NON-NULL MEANS "THE PLATFORM CAN PUT THIS BACK" — not "this was saved". Every automatic
   * restore goes through `SessionManager.newest_restore_source`, which returns this copy in
   * preference to the saved bundle; where it does not — the saved bundle is genuinely newer, or
   * the two hold the same tree — what comes back is no older than this instant anyway. That is
   * what makes a `true` beside a non-null `recoveryAt` a warning about nothing.
   *
   * Comes from `SaveState.recoveryAt` on `GET /build-sessions/projects/{id}/save-state`, the same
   * read that produces `saveDirty` — the two must describe ONE reading, or the guard is deciding
   * from a dirty flag of one moment and a recovery instant of another.
   */
  recoveryAt?: string | null
}

/** Is there anything a person could lose by leaving right now? */
function worthWarningAbout(
  saveDirty: boolean | null,
  workspaceIsAlive: boolean,
  recoveryAt: string | null,
): boolean {
  // A definite `true` is unsaved work — but only work the platform CANNOT put back is work
  // leaving could cost somebody. A recovery copy is what every automatic restore already
  // reaches for first, so warning beside one is a prompt with nothing behind it.
  //
  // THE QUESTION IS ASKED THROUGH `canBePutBack`, NOT WITH `!== null`, and the difference is a
  // real one: `!== null` reads an `undefined` from a caller that has not been updated as "there
  // is a copy" and disarms this guard on work nothing is holding. Absent means warn.
  if (saveDirty === true) return !canBePutBack(recoveryAt)
  // `null` while ALIVE is a check that ran and could not answer, so the platform says so. A
  // recovery copy does NOT quiet this arm: the question that went unanswered was whether the
  // container holds anything at all, and the honest dialog for that is the one that says so.
  // (The server cannot even pair the two today — every `dirty=None` arm of `project_save_state`
  // reports no recovery instant — so this reads the same either way, deliberately, rather than
  // depending on that.)
  // `null` while not alive is a check nobody asked, which is not the same claim at all.
  return saveDirty === null && workspaceIsAlive
}

export function useUnsavedWorkGuard({
  saveDirty,
  workspaceIsAlive,
  projectId,
  projectName = null,
  recoveryAt = null,
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
      if (!worthWarningAbout(saveDirty, workspaceIsAlive, recoveryAt)) {
        go()
        return
      }
      // Stored as a thunk INSIDE a setter callback: `setPending(go)` would call `go` immediately,
      // because React treats a function argument as an updater. The bug is silent — the navigation
      // simply happens, guard and all.
      setError(null)
      setPending(() => go)
    },
    [saveDirty, workspaceIsAlive, recoveryAt],
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
      projectName={projectName}
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
  /** Whose work is at risk, or `null` when the caller cannot say. */
  projectName: string | null
  /** `true` = we know there are unsaved changes; `false` = we could not check and say so. */
  certain: boolean
  saving: boolean
  error: string | null
  canSave: boolean
  onSaveAndLeave: () => void
  onLeaveAnyway: () => void
  onStay: () => void
}

/**
 * HAND-ROLLED, MATCHING `ReclaimWorkspaceDialog`: has `aria-modal`/`aria-labelledby`,
 * Escape/overlay-click to stay (inert mid-save), initial focus on Stay — but no focus trap or
 * scroll lock, so Tab can walk out into the page behind it.
 * Worth closing, since this is the last guard on someone's unsaved work; `components/ui/dialog.tsx`
 * (Radix, see `AttachmentPreview.tsx`) is the upgrade path, left separate so this change doesn't
 * also move real behaviour. `ReclaimWorkspaceDialog` stays the copy/focus-park pattern either way.
 */
function UnsavedWorkDialog({
  certain,
  saving,
  error,
  canSave,
  onSaveAndLeave,
  onLeaveAnyway,
  onStay,
  projectName,
}: DialogProps) {
  const subject = projectName ? `“${projectName}”` : 'This app'
  const subjectLower = projectName ? `“${projectName}”` : 'this app'
  // A save here is the same 40-second write the hand-over dialog performs, against the same
  // container — so it needs the same honest wait. See `ui/Waiting.tsx` for why a spinner alone
  // is not one, and why this number appears under motion as well as without it.
  const elapsed = useElapsedSeconds(saving)
  const showElapsed = elapsed * 1000 >= ELAPSED_AFTER_MS
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
          {/* NAMED WHERE THE CALLER KNOWS IT. "This app" is ambiguous the moment
              somebody has more than one project, and both exits this dialog covers are taken
              while thinking about a different one. Falls back to the old phrasing rather than
              rendering an empty pair of quotes. */}
          {certain
            ? `${subject} has changes that are not saved yet. Save them and they come back exactly as you left them; leave without saving and they go.`
            : // Say that the platform could not tell, rather than reporting there is nothing
              // to lose. A wrong reassurance is the one answer that costs somebody their work.
              `We could not tell whether ${subjectLower} has unsaved changes. Saving first is the safe option.`}
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
              {saving ? <BusyGlyph size={15} /> : null} Save and leave
              {showElapsed && (
                <span aria-hidden="true" className="tabular-nums opacity-80">
                  {elapsed}s
                </span>
              )}
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
 * THE ONE EXIT FUNCTION, owned by the workspace and consulted by every control that leaves it.
 *
 * A CONTEXT, NOT A PROP, and the context is PROVIDED ABOVE THE WORKSPACE RATHER THAN BY IT. Its
 * consumers sit on both sides of the workspace: the toolbar's back control is a descendant, while
 * the left navigation, the brand link and Sign out are all rendered by the shell that FRAMES the
 * workspace, so a provider inside it could never reach them. A guard those controls cannot see is
 * not a weaker guard, it is no guard at all on the routes a citizen actually leaves by — and the
 * failure is silent, because every one of them still navigates perfectly.
 *
 * THE VALUE IS STABLE AND THE GUARD IS REGISTERED INTO IT. The workspace mounts and unmounts as a
 * citizen moves in and out of an application, but the function the navigation holds must not
 * change identity every time that happens, so the host hands out one delegate for the life of the
 * app and the workspace writes its guard behind it.
 *
 * NO WORKSPACE IS ORDINARY, not an error: with nothing registered the delegate simply goes,
 * leaving every other page's navigation exactly as it was.
 */
const WorkspaceExitContext = createContext<((go: () => void) => void) | null>(null)
const WorkspaceExitRegistrar = createContext<((guard: Guard | null) => void) | null>(null)

type Guard = (go: () => void) => void

/** Wraps the whole authenticated app: everything that can leave a workspace reads from here. */
export function WorkspaceExitHost({ children }: { children: ReactNode }) {
  const registered = useRef<Guard | null>(null)
  const register = useCallback((guard: Guard | null) => {
    registered.current = guard
  }, [])
  const exit = useCallback<Guard>((go) => (registered.current ?? runStraightThrough)(go), [])
  return (
    <WorkspaceExitRegistrar.Provider value={register}>
      <WorkspaceExitContext.Provider value={exit}>{children}</WorkspaceExitContext.Provider>
    </WorkspaceExitRegistrar.Provider>
  )
}

/**
 * The workspace publishes its guard. A LAYOUT EFFECT, not an ordinary one: an effect that ran
 * after paint would leave a window in which the navigation is on screen, clickable, and still
 * holding the passthrough.
 */
export function useRegisterWorkspaceExit(guard: Guard): void {
  const register = useContext(WorkspaceExitRegistrar)
  useLayoutEffect(() => {
    register?.(guard)
    return () => register?.(null)
  }, [register, guard])
}

/** Run an exit through the workspace's guard, or straight through when there is none. */
export function useWorkspaceExit(): Guard {
  const guard = useContext(WorkspaceExitContext)
  return guard ?? runStraightThrough
}

const runStraightThrough = (go: () => void) => go()
