/**
 * `/shared/:projectId` — a colleague's restricted view of a project shared with them (#198).
 *
 * DELIBERATELY NOT PART OF `WorkspaceShell`. That layout route exists to keep a builder's own
 * running app framed beside chat/rail/toolbar surfaces across a move between `/projects/:id`
 * and `/chat/:id` — none of which a shared recipient may ever reach (requirement 14). Rather
 * than hide those surfaces inside the builder's workspace, this is its own top-level route with
 * its own minimal chrome: a back link, the project's name, the "Can use" label (Key Decision 3
 * — never "view only"), and the app itself in a plain iframe. There is no chat, no save, no
 * publish, no rename here for the same reason there is no code to hide them behind — the API
 * enforces the restriction independently, so a recipient's browser never has the option in the
 * first place, not merely a UI that declines to show it.
 *
 * Launch attaches to an already-live view when one is up; Refresh always re-restores from
 * whatever the owner currently has saved, and reports when that snapshot was taken — the two
 * `buildSessionApi` calls this wires to directly (see their own docstrings for the distinction).
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, RefreshCw } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import { BusyGlyph } from '../components/ui/Waiting'
import ReclaimWorkspaceDialog from '../components/projects/ReclaimWorkspaceDialog'
import { getProject } from '../utils/projectApi'
import type { Project } from '../utils/projectApi'
import {
  asReclaimBlocked,
  handOverWorkspace,
  launchSharedPreview,
  refreshSharedPreview,
  type HandoverStep,
  type ReclaimBlocked,
} from '../utils/buildSessionApi'
import type { SharedPreviewResponse } from '../utils/buildSessionTypes'
import { ApiError } from '../utils/apiError'
import { relativeTimeVerbose } from '../utils/relativeTime'
import { PROJECT_GONE_NOTICE } from './ProjectsPage'

export default function SharedProjectPage(): React.JSX.Element {
  const { projectId } = useParams()
  const navigate = useNavigate()

  const [project, setProject] = useState<Project | null>(null)
  const [projectError, setProjectError] = useState<string | null>(null)

  const [preview, setPreview] = useState<SharedPreviewResponse | null>(null)
  const [launching, setLaunching] = useState(false)
  const [launchError, setLaunchError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  // FORCES THE IFRAME TO ACTUALLY RELOAD. `preview.previewUrl` is a hash of (app, recipient)
  // — `shr_name_for` — so it is byte-identical across a teardown-and-restore: a successful
  // Refresh writes the SAME `src` to the SAME node, React sees no prop change, and the
  // browser never reloads. The recipient keeps looking at a document whose container
  // underneath it has already been destroyed and rebuilt. Bumped on every successful
  // launch/refresh and folded into the iframe's `key`, which — unlike `src` — React always
  // treats a change to as "this is a new element", forcing a real remount.
  const [frameNonce, setFrameNonce] = useState(0)

  // THE HAND-OVER PROMPT (requirement 24, its frontend half). `launchSharedPreview`/
  // `refreshSharedPreview` DO take the caller's own one-per-user slot — see their own
  // corrected docstrings — so either can 409 `sandbox_reclaim_blocked` exactly as a relaunch
  // can, and it must reach a citizen sitting on this standalone route the same way it reaches
  // one inside the workspace shell. `pendingAction` remembers WHICH call to retry once the
  // slot is freed — Launch and Refresh both funnel through here, and the dialog itself does
  // not know or care which one asked.
  const [blocked, setBlocked] = useState<ReclaimBlocked | null>(null)
  const [pendingAction, setPendingAction] = useState<'launch' | 'refresh' | null>(null)
  const [step, setStep] = useState<HandoverStep | null>(null)
  const [resolving, setResolving] = useState(false)

  const bounceGone = useCallback(
    () => navigate('/projects', { replace: true, state: { notice: PROJECT_GONE_NOTICE } }),
    [navigate],
  )

  // Load the project for its name and to confirm the share still stands. A 404 here is
  // identical, deliberately, to a 404 on `/projects/:id` — the caller no longer has access,
  // whether because the project was deleted or the share was revoked, and the non-leaking
  // 404 the resolver already gives every other reader gives the same answer to both.
  useEffect(() => {
    if (!projectId) {
      navigate('/projects', { replace: true })
      return
    }
    let active = true
    void (async () => {
      try {
        const loaded = await getProject(projectId)
        if (!active) return
        // An owner who lands here belongs on their own workspace, not this restricted one.
        if (loaded.access === 'owner') {
          navigate(`/projects/${projectId}`, { replace: true })
          return
        }
        setProject(loaded)
        setProjectError(null)
      } catch (err) {
        if (!active) return
        if (err instanceof ApiError && err.status === 404) {
          bounceGone()
          return
        }
        setProjectError(err instanceof Error ? err.message : 'Could not load this project.')
      }
    })()
    return () => {
      active = false
    }
  }, [projectId, navigate, bounceGone])

  const launch = useCallback((): void => {
    if (!projectId) return
    setLaunching(true)
    setLaunchError(null)
    launchSharedPreview(projectId)
      .then((res) => {
        setPreview(res)
        setFrameNonce((n) => n + 1)
      })
      .catch((err: unknown) => {
        // THE HAND-OVER PROMPT, NOT A GENERIC FAILURE (requirement 24). This 409 means the
        // caller's own slot is not free — either their own build, or another shared view they
        // opened earlier — and a Retry that just calls Launch again would fail identically
        // forever. `pendingAction` is what lets `resolve` below know to call THIS function
        // again once the slot is freed.
        const reclaim = asReclaimBlocked(err)
        if (reclaim) {
          setPendingAction('launch')
          setBlocked(reclaim)
          return
        }
        setLaunchError(err instanceof Error ? err.message : 'Could not open this shared project.')
      })
      .finally(() => setLaunching(false))
  }, [projectId])

  // Fires once the project resolves — UNLESS the owner has confirmed nothing saved
  // (`hasSavedSnapshot === false`, requirement 10's second sentence). Launching into that is a
  // guaranteed, already-known failure; the render below shows the explanation directly instead
  // of spending a round trip to learn what `getProject` already said. `null` (unknown) still
  // launches — that reading means "cannot say", not "confirmed missing", and the reactive
  // failure state below is exactly what covers a wrong guess in that direction.
  useEffect(() => {
    if (project !== null && project.hasSavedSnapshot !== false) launch()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project])

  const onRefresh = useCallback((): void => {
    if (!projectId) return
    setRefreshing(true)
    setLaunchError(null)
    refreshSharedPreview(projectId)
      .then((res) => {
        setPreview(res)
        setFrameNonce((n) => n + 1)
      })
      .catch((err: unknown) => {
        const reclaim = asReclaimBlocked(err)
        if (reclaim) {
          setPendingAction('refresh')
          setBlocked(reclaim)
          return
        }
        setLaunchError(err instanceof Error ? err.message : 'Could not refresh this shared project.')
      })
      .finally(() => setRefreshing(false))
  }, [projectId])

  // Fresh sequencing every time the dialog opens — a hand-over that failed and was retried
  // must not carry the PREVIOUS attempt's step forward.
  const cancelBlocked = useCallback((): void => {
    setBlocked(null)
    setPendingAction(null)
    setStep(null)
  }, [])

  const retryPendingAction = useCallback((): void => {
    if (pendingAction === 'refresh') onRefresh()
    else launch()
  }, [pendingAction, onRefresh, launch])

  // `handOverWorkspace` branches on `blocked.isSharedView` ITSELF now (taking the whole
  // `ReclaimBlocked` rather than a bare project id) — the ONE place that decision is made,
  // so no call site can call `stopActiveBuild`/`release` against a shared occupant's OWNER
  // id again by forgetting to check the flag. A shared occupant's `dirty` is always `false`,
  // so `ReclaimWorkspaceDialog`'s own `copyFor` never renders a Save button for it — `save`
  // is accepted for symmetry with the dialog's two-button contract and is unreachable there.
  const resolveBlocked = useCallback(
    async (save: boolean): Promise<void> => {
      if (blocked === null) return
      setResolving(true)
      setStep('stopping')
      try {
        await handOverWorkspace(blocked, save, {}, (next) => setStep(next))
        cancelBlocked()
        retryPendingAction()
      } catch (err) {
        setLaunchError(
          err instanceof Error ? err.message : 'Could not close the other app just now.',
        )
        cancelBlocked()
      } finally {
        setResolving(false)
      }
    },
    [blocked, cancelBlocked, retryPendingAction],
  )

  const busy = launching || refreshing

  return (
    <div className="min-h-screen font-manrope flex flex-col bg-bial-bg">
      <Navbar />

      <div className="flex items-center gap-3 px-6 py-3 border-b border-bial-border bg-white">
        <button
          type="button"
          onClick={() => navigate('/projects')}
          className="flex items-center gap-1 text-sm text-neutral hover:text-primary transition flex-shrink-0"
        >
          <ArrowLeft size={15} /> Back to projects
        </button>
        <h1 className="text-sm font-bold text-tertiary truncate min-w-0">
          {project?.name || 'Shared project'}
        </h1>
        {/* "Can use", never "view only" — Key Decision 3. What this recipient has is not a
            read-only preview; anything they enter here is saved into the project's real data,
            exactly as the share panel that granted it says. */}
        <span className="flex-shrink-0 text-[10px] font-bold uppercase tracking-wide text-neutral bg-bial-bg px-2 py-0.5 rounded-full border border-bial-border">
          Can use
        </span>
        <div className="ml-auto flex items-center gap-3 flex-shrink-0">
          {preview?.snapshotTakenAt && (
            <span className="text-[11px] text-neutral whitespace-nowrap hidden sm:inline">
              Snapshot from {relativeTimeVerbose(preview.snapshotTakenAt)}
            </span>
          )}
          <button
            type="button"
            onClick={onRefresh}
            // A REAL `disabled`, not `aria-disabled` — that attribute alone never actually
            // stops a click, so Refresh fired while a launch was in flight, while a refresh
            // was already running, and when the owner had nothing saved (the one case this
            // was added for). Tailwind's `disabled:` variant also only matches the true
            // `:disabled` pseudo-class, so the dim styling silently never applied either.
            disabled={busy || project === null || project.hasSavedSnapshot === false}
            className="inline-flex items-center gap-1.5 text-xs font-semibold text-primary hover:underline disabled:opacity-50 disabled:pointer-events-none"
          >
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : undefined} /> Refresh
          </button>
        </div>
      </div>

      <main className="flex-1 min-h-0 flex flex-col">
        {projectError !== null ? (
          <div className="flex-1 flex items-center justify-center p-6">
            <div className="bg-white border border-danger/20 rounded-2xl py-16 px-6 text-center max-w-md">
              <p className="text-sm font-semibold text-tertiary">Couldn’t load this project</p>
              <p className="text-xs text-neutral mt-1">{projectError}</p>
            </div>
          </div>
        ) : project !== null && project.hasSavedSnapshot === false ? (
          // LAUNCH DISABLED, WITH THE SAME EXPLANATION the share panel and a launch attempt
          // would each give (requirement 10's second sentence) — shown up front, never
          // attempted into a failure the project's own GET already knew about.
          <div className="flex-1 flex items-center justify-center p-6">
            <div className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center max-w-md">
              <p className="text-sm font-semibold text-tertiary">Nothing to launch yet</p>
              <p className="text-xs text-neutral mt-1">
                The owner hasn’t saved a version of this app yet.
              </p>
            </div>
          </div>
        ) : launchError !== null ? (
          // A STATED FAILURE, WITH RETRY (requirement 29) — never a blank frame.
          <div className="flex-1 flex items-center justify-center p-6">
            <div className="bg-white border border-danger/20 rounded-2xl py-16 px-6 text-center max-w-md">
              <p className="text-sm font-semibold text-tertiary">Couldn’t open this app</p>
              <p className="text-xs text-neutral mt-1 mb-3">{launchError}</p>
              <button type="button" onClick={launch} className="text-xs text-primary font-semibold hover:underline">
                Retry
              </button>
            </div>
          </div>
        ) : preview !== null && preview.ready ? (
          <iframe
            key={`${preview.previewUrl}#${frameNonce}`}
            title={project?.name || 'Shared project'}
            src={preview.previewUrl}
            className="flex-1 w-full border-0"
          />
        ) : (
          // A STATED WAIT (requirement 29) rather than a blank pane, for both the initial
          // launch and a not-yet-ready response.
          <div className="flex-1 flex items-center justify-center" role="status" aria-live="polite">
            <div className="flex flex-col items-center gap-3">
              <BusyGlyph size={24} className="text-primary" />
              <p className="text-sm font-medium text-neutral">Getting this app ready…</p>
            </div>
          </div>
        )}
      </main>

      {blocked !== null && (
        <ReclaimWorkspaceDialog
          blocked={blocked}
          startingProjectName={project?.name ?? null}
          onSaveAndSwitch={() => resolveBlocked(true)}
          onSwitchAnyway={() => resolveBlocked(false)}
          onCancel={cancelBlocked}
          step={resolving ? step : null}
        />
      )}
    </div>
  )
}
