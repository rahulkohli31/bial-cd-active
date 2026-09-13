/**
 * `/projects/:projectId` — the project screen IS the app now.
 *
 * WHY THIS EXISTS: it shows the RUNNING SANDBOX beside the rail, behind one control the person
 * presses deliberately — nothing starts a container because a screen was opened (the pane reads
 * a cheap state endpoint, no container call). No passive view of stored code, no lifecycle badge,
 * no reroute into a chat; the suite beside this file asserts their absence.
 *
 * This file owns the route, the data, and the beacon — everything visual moved down
 * (`ProjectWorkspace` publishes on the workspace channel, `WorkspaceRail` renders it); it holds
 * no layout of its own, since the two-column frame belongs to `WorkspaceShell`, above the Outlet.
 * THE BEACON FIRES FROM EXACTLY ONE PLACE — the successful-load branch below — because it feeds a
 * measurement nothing in the UI reflects, so a drop or a double-fire makes the numbers wrong with
 * no symptom and no failing test; `observe.ts`'s per-project guard only makes a REPEATED call a
 * no-op, so a second tracker (tempting, since `ProjectWorkspace` independently needs
 * `project.appId`) would bypass that guard rather than be caught by it.
 *
 * Identity model (see: app identity + flat URL model): `appId`/`hasRelaunchableSnapshot` are READ
 * off the project (a backend LEFT JOIN), never via a mutating provision call; `appStatus` lives on
 * the admin registry, not here. A new chat opens at a flat `/chat/{uuid}` carrying its project in
 * a transient `?projectId=&kind=` query — the row doesn't exist until its first message.
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import ProjectWorkspace from '../components/workspace/ProjectWorkspace'
import { usePublishHeading, useWorkspaceProject } from '../components/workspace/workspaceChannel'
import { getProject } from '../utils/projectApi'
import type { Project } from '../utils/projectApi'
import { ApiError } from '../utils/apiError'
import { markProjectOpened } from '../utils/observe'
import { PROJECT_GONE_NOTICE } from './ProjectsPage'

/**
 * WHAT THE CARD SAYS — and the one status whose sentence is never the server's.
 *
 * A 422 on this GET can only be the PATH PARAMETER: the read carries no body for Pydantic to
 * validate, so the `detail[]` FastAPI sends back is always the parser's account of an id that is
 * not a UUID, and `flattenValidationDetail` was joining it straight onto the screen:
 *
 *   "Input should be a valid UUID, invalid group length in group 4: expected 12, found 7"
 *
 * Nobody who reads that sentence typed the id. The realistic path here is a link that lost
 * characters — a truncated paste, an address wrapped by a mail client — and which group came up
 * five short is addressed to whoever produced the link, not to the citizen holding it. So the
 * sentence is `PROJECT_GONE_NOTICE`: from where they stand a malformed address and a deleted one
 * are the same event, an address that does not lead anywhere, and they get the same words for it.
 *
 * IT DOES NOT BOUNCE, and that is the whole difference from the 404 branch above. A 404 is a
 * project that WAS an address and stopped being one, so the list is where the citizen now
 * belongs. A 422 never addressed a project at all, and redirecting out of an address somebody
 * deliberately opened reads as the app taking their place away. The page stays — with its back
 * control on it, which is what makes staying a choice rather than a dead end.
 *
 * EVERY OTHER STATUS KEEPS `err.message`. Those are the backend's citizen-facing envelope-1
 * messages; this is not a licence to replace them all with one line.
 */
function loadErrorFor(err: unknown): string {
  if (!(err instanceof ApiError)) return 'Could not load this project.'
  return err.status === 422 ? PROJECT_GONE_NOTICE : err.message
}

export default function ProjectPage() {
  const { projectId } = useParams()
  // WHICH PROJECT THE WORKSPACE IS SHOWING. Declared above the early returns below, because the
  // loading and load-error branches are still this project's screen. A held preview address
  // outlives the surface that published it, and this is the only thing that can retire a stale one
  // — a surface that says nothing leaves the previous project's app framed, invisibly, with nothing
  // able to notice. `ProjectWorkspace` declares it again once the project resolves; the channel's
  // value comparison makes the second call free.
  useWorkspaceProject(projectId ?? null)
  const navigate = useNavigate()

  const [project, setProject] = useState<Project | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // WHAT THE TOOLBAR ROW NAMES, PUBLISHED FROM THE ROUTE — above the early returns
  // below, for the same reason the project declaration is above them. The loading and load-error
  // branches are still this project's screen, and the row draws its back control and holds its own
  // height on both, rather than appearing once the fetch lands. `chatTitle`/`chatKind` are `null`
  // here and that IS the signal: a heading with no kind is a project screen.
  //
  // THE SAME IDENTITY GUARD `ChatRoute.tsx` ALREADY CARRIES. `projectId` is a route param on
  // a route that is NOT remounted when it changes (see the wait region's own note below), so
  // moving from one project to another re-renders this same instance with the OLD `project` still
  // in state until the new fetch resolves. Without the guard, the rename pencil kept gating on
  // `project?.name` alone — non-null, so still drawn — while `ProjectWorkspace`, the only
  // registrar of the actual rename handler, had already unmounted for the load: a control that
  // still LOOKS live over a project that is no longer on screen, and does nothing pressed. Gating
  // on the id agreeing is what `ChatRoute.tsx` does for the identical hazard.
  usePublishHeading({
    projectId: projectId ?? null,
    projectName: project !== null && project.id === projectId ? project.name : null,
    chatTitle: null,
    chatKind: null,
  })

  const goToProjects = useCallback(() => navigate('/projects', { replace: true }), [navigate])

  // THE SAME BOUNCE, CARRYING THE REASON IT USED TO THROW AWAY. Two navigations rather than one
  // flag, because they are not the same event: `goToProjects` is the back control a citizen
  // PRESSED, and being told "that project is no longer available" after asking to leave a project
  // that is perfectly fine would be a lie. This one is the involuntary exit.
  //
  // The sentence is `ProjectsPage`'s constant, never `err.message`. The server's 404 for another
  // citizen's project is deliberately identical to its 404 for a project that never existed —
  // this platform is single-tenant with no cross-user existence leak — and piping its text
  // through is the one change that could ever make those two print differently.
  const bounceGone = useCallback(
    () => navigate('/projects', { replace: true, state: { notice: PROJECT_GONE_NOTICE } }),
    [navigate],
  )

  // THE SAME INVOLUNTARY-BOUNCE SHAPE AS `bounceGone`, for a recipient who opened this exact
  // address. It is real — unlike a deleted project, nothing here has gone missing — it is just
  // the wrong screen for what `access` says they hold, so `replace` and no notice: the sentence
  // that fits a vanished project would be a lie about one that is very much still there.
  const bounceToShared = useCallback(
    (id: string) => navigate(`/shared/${id}`, { replace: true }),
    [navigate],
  )

  // Load the project. A 404 means it was deleted elsewhere — bounce to the index rather than
  // strand the user on a dead page.
  useEffect(() => {
    if (!projectId) {
      goToProjects()
      return
    }
    let active = true
    setLoading(true)
    void (async () => {
      try {
        const loaded = await getProject(projectId)
        if (!active) return
        // A RECIPIENT NEVER RENDERS THIS SCREEN. This is the owner's full workspace — chat,
        // build controls, save, publish, rename — none of which requirement 14 permits a
        // shared recipient to reach, and the resolver's tri-state `access` is exactly the
        // signal that tells the two apart. `replace` because this bounce is involuntary: the
        // address they opened is real, it just belongs to the other view of it.
        if (loaded.access === 'shared') {
          bounceToShared(projectId)
          return
        }
        setProject(loaded)
        setLoadError(null)
        // The chat-open ratio's denominator, and the time-to-app-visible clock's start. Marked
        // HERE rather than on the raw mount because `hasApp` is only knowable once the project
        // has loaded — a project with nothing built has no app to first-see, and starting a
        // clock for it would make this number and the sandbox-first number answer different
        // questions. `markProjectOpened` is idempotent per project id per page load, which is
        // also the StrictMode guard.
        markProjectOpened(loaded.id, { hasApp: loaded.appId !== null })
      } catch (err) {
        if (!active) return
        if (err instanceof ApiError && err.status === 404) {
          bounceGone()
          return
        }
        setLoadError(loadErrorFor(err))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [projectId, goToProjects, bounceGone, bounceToShared])

  /* THE CHATS READ, ITS ERROR AND THE DELETE HANDLER ARE DELIBERATELY ABSENT. They existed for
     one renderer, the rail's "Conversations · this project" list, which the client asked not to
     have — nothing points back to a chat, running or finished. Removing the list removed the only
     route back to an existing chat AND the only way to delete one; both are the owner's decision,
     taken knowingly. Chats, their plans and their uploaded files stay in the database. Said here
     as well as in the rail because this is where the reads would be, and an absent fetch explains
     itself to nobody. */

  /* THE THREE BRANCHES ARE ONE RETURN, AND THE POLITE REGION IS ABOVE ALL OF THEM.
     They used to be three early returns, and that shape is exactly what cannot carry a live
     region: a region inserted together with its text is missed entirely by several reader-and-
     browser combinations (`TurnBanner`, `LivePreview` both record it), and an early return means
     the region is born with the sentence already inside it. So the region is rendered here on
     every branch, EMPTY when the project is already on screen, and the skeleton box is what
     appears inside it.

     THIS IS NOT A THEORETICAL CASE ON THIS PAGE. `projectId` is a route param on a route that is
     not remounted when it changes, so moving between two projects flips a settled screen back to
     `loading` against a region that has been in the accessibility tree the whole time.

     The region WRAPS the sentence rather than duplicating it `sr-only` — `Announcer.tsx` records
     that a second copy is the sentence read twice, and that writing it that way broke three
     tests. `aria-busy` stays on the box: it is a property, not a speech. */
  return (
    <>
      {/* Laid out only while it holds something. An empty region is a zero-height flex child; the
          wait needs the column's full height for the same reason the branch it replaced was a
          `flex-1` `<main>`. The NODE is unchanged either way — only its class list is. */}
      <div
        role="status"
        aria-live="polite"
        data-testid="project-wait"
        className={loading ? 'flex-1 min-h-0 flex flex-col' : ''}
      >
        {loading ? (
          <main className="flex-1 min-h-0 overflow-y-auto" aria-busy="true">
            <div className="w-full px-5 py-6">
              <p className="text-sm font-medium text-neutral mb-4">Loading this project…</p>
              <div className="h-6 w-48 bg-gray-100 rounded animate-pulse mb-4" />
              <div className="h-24 bg-gray-100 rounded-2xl animate-pulse" />
            </div>
          </main>
        ) : null}
      </div>

      {loading ? null : loadError || !project ? (
        <main className="flex-1 min-h-0 overflow-y-auto">
          <div className="w-full px-5 py-6">
            <button
              onClick={goToProjects}
              className="flex items-center gap-1 text-sm text-neutral hover:text-primary transition mb-4"
            >
              <ArrowLeft size={15} /> Back to projects
            </button>
            <div className="bg-white border border-danger/20 rounded-2xl py-16 px-6 text-center">
              <p className="text-sm font-semibold text-tertiary">Couldn’t load this project</p>
              <p className="text-xs text-neutral mt-1">{loadError || 'It may have been deleted.'}</p>
            </div>
          </div>
        </main>
      ) : (
        <ProjectWorkspace project={project} onProjectUpdate={setProject} />
      )}
    </>
  )
}
