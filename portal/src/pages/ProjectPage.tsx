/**
 * `/projects/:projectId` — the project screen IS the app now.
 *
 * ═══ THE PHASE-1 DECISION THIS REVERSES, AND WHY THE REVERSAL IS NOT A REGRESSION ═══
 *
 * This page's previous docblock recorded a removal: "the passive 'View app' preview is HIDDEN in
 * Phase-1: a stored app is not a running sandbox". That decision was RIGHT ABOUT WHAT IT REMOVED.
 * What it took away was a passive view of stored code, plus a lifecycle badge and a reroute into a
 * chat — three things that told a citizen about an artefact rather than showing them their app.
 *
 * What arrives here is not that. It is the RUNNING SANDBOX, in a pane beside the rail, behind one
 * control the person presses deliberately. Nothing starts a container because a screen was opened
 * (R3): the pane reads a cheap state endpoint that makes no container call, and the only thing that
 * starts anything is a press. So the argument the removal rested on is answered rather than
 * overruled — a stored app is still not a running sandbox, and this screen no longer shows one a
 * stored app. The three things it removed stay removed, and the suite beside this file keeps
 * asserting their absence.
 *
 * ═══ WHAT THIS FILE OWNS AFTER THE SPLIT ═══
 *
 * The route, the data, and the beacon. Everything visual moved down: `ProjectWorkspace` is the
 * project-scoped publisher on the workspace channel, and `WorkspaceRail` is what the rail renders.
 * This file starts no publish of its own and holds no layout — the two-column frame belongs to
 * `WorkspaceShell`, above the Outlet, and building a second one here would nest one grid inside
 * another and remount the app on every navigation.
 *
 * THE OBSERVATION BEACON FIRES FROM EXACTLY ONE PLACE, and that place is here — the successful-load
 * branch below. It feeds a measurement nothing in the UI reflects, so dropping it, double-firing
 * it, or letting a remount fire it twice makes the numbers wrong with no symptom and no failing
 * test. `ProjectWorkspace` independently needs `project.appId` for the rail's status line, which is
 * exactly the pull that would make somebody add a second tracker down there; `observe.ts`'s own
 * per-project guard makes a repeated call a safe no-op, so the risk is not defeating that guard but
 * bypassing it with a second mechanism it does not cover.
 *
 * Identity model (memory: app identity + flat URL model):
 *   - `appId` / `hasRelaunchableSnapshot` are READ off the project (a LEFT JOIN on the backend);
 *     the portal never fires a mutating provision call just to learn them. `appStatus` is not
 *     surfaced here — app lifecycle lives on the admin registry, not the citizen project screen.
 *   - a new chat opens at a flat `/chat/{uuid}` carrying its project in a transient
 *     `?projectId=&kind=` query; the row does not exist until its first message.
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

  // WHAT THE TOOLBAR ROW NAMES, PUBLISHED FROM THE ROUTE (plan 002, U2) — above the early returns
  // below, for the same reason the project declaration is above them. The loading and load-error
  // branches are still this project's screen, and the row draws its back control and holds its own
  // height on both, rather than appearing once the fetch lands. `chatTitle`/`chatKind` are `null`
  // here and that IS the signal: a heading with no kind is a project screen.
  usePublishHeading({
    projectId: projectId ?? null,
    projectName: project?.name ?? null,
    chatTitle: null,
    chatKind: null,
  })

  const goToProjects = useCallback(() => navigate('/projects', { replace: true }), [navigate])

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
        setProject(loaded)
        setLoadError(null)
        // R105's denominator, and the R104 clock's start. Marked HERE rather than on the raw mount
        // because `hasApp` is only knowable once the project has loaded — a project with nothing
        // built has no app to first-see, and starting a clock for it would make this number and the
        // sandbox-first number answer different questions. `markProjectOpened` is idempotent per
        // project id per page load, which is also the StrictMode guard.
        markProjectOpened(loaded.id, { hasApp: loaded.appId !== null })
      } catch (err) {
        if (!active) return
        if (err instanceof ApiError && err.status === 404) {
          goToProjects()
          return
        }
        setLoadError(err instanceof ApiError ? err.message : 'Could not load this project.')
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [projectId, goToProjects])

  /* THE CHATS READ, ITS ERROR AND THE DELETE HANDLER ARE GONE (plan 002, U3). They existed for
     one renderer, the rail's "Conversations · this project" list, which the client asked not to
     have — and the ruling of 2026-09-02 is that nothing points back to a chat, running or
     finished. Removing the list removed the only route back to an existing chat AND the only way
     to delete one; both are the owner's decision, taken knowingly. Chats, their plans and their
     uploaded files stay in the database. Said here as well as in the rail because this is where
     the reads used to be, and an absent fetch explains itself to nobody. */

  if (loading) {
    return (
      <main className="flex-1 min-h-0 overflow-y-auto">
        <div className="w-full px-5 py-6">
          <div className="h-6 w-48 bg-gray-100 rounded animate-pulse mb-4" />
          <div className="h-24 bg-gray-100 rounded-2xl animate-pulse" />
        </div>
      </main>
    )
  }

  if (loadError || !project) {
    return (
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
    )
  }

  return (
    <ProjectWorkspace
      project={project}
      onProjectUpdate={setProject}
    />
  )
}
