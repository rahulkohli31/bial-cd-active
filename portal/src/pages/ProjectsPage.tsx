/**
 * `/projects` — the new home. A citizen developer opens this, sees their tools
 * (projects), searches across them, opens one, creates one, or deletes one.
 *
 * Pagination is server-side keyset, driven by `useKeysetList` over `listProjects`:
 * forward-only "Load more", no page numbers (keyset returns no total, so numbered
 * pages are not expressible) — this is the deliberate replacement for the old
 * load-everything-then-`slice` model the retired flat all-chats list used.
 *
 * Two empty states that are NOT the same thing and must not be conflated:
 *   - zero projects AND no search  → a first-run "create your first project" CTA
 *   - zero results WITH a search   → a "no matches" message that a cleared query undoes
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Search, Loader2, FolderPlus, X } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import { listProjects, deleteProject, type Project } from '../utils/projectApi'
import { ApiError } from '../utils/apiError'
import { useKeysetList, type KeysetFetchArgs } from '../hooks/useKeysetList'
import ProjectCard from '../components/projects/ProjectCard'
import ProjectCreateModal from '../components/projects/ProjectCreateModal'
import ProjectDeleteDialog from '../components/projects/ProjectDeleteDialog'

export default function ProjectsPage(): React.JSX.Element {
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)
  const [deleting, setDeleting] = useState<Project | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  const fetchPage = useCallback(
    (args: KeysetFetchArgs) => listProjects({ cursor: args.cursor, limit: args.limit, q: args.q || undefined }),
    [],
  )
  const { items, q, appliedQuery, loading, hasMore, error, loadMore, setQuery, refresh, reset, removeLocal } = useKeysetList<Project>({
    fetchPage,
  })

  // First page on mount. `loadMore` is stable (the hook memoizes it), so this fires once.
  useEffect(() => {
    loadMore()
  }, [loadMore])

  const openProject = (id: string): void => navigate(`/projects/${id}`)

  // A brand-new project navigates straight into its home; the list refetches
  // newest-first when the user returns, so there is no client-side prepend to keep
  // in sync (and none to drift). This is also what makes AE3 hold by construction.
  const handleCreated = (project: Project): void => {
    setShowCreate(false)
    navigate(`/projects/${project.id}`)
  }

  const handleDelete = async (project: Project): Promise<void> => {
    setDeleting(null)
    // Optimistic: drop the row now, reconcile only if the server disagrees.
    removeLocal((p) => p.id === project.id)
    try {
      await deleteProject(project.id)
    } catch (caught) {
      // 404 = already deleted (e.g. another tab). The row is already gone; that IS
      // the desired end state, so swallow it — no scary toast for a no-op.
      if (caught instanceof ApiError && caught.status === 404) return
      // Any other failure did not delete: bring the row back and say why. `refresh()` rewinds
      // to page 1 under the CURRENT filter — `reset()` would also clear the search box, so a
      // failed delete would silently discard a query the user typed and is still reading.
      refresh()
      setToast(caught instanceof Error ? caught.message : 'Could not delete the project.')
    }
  }

  const isEmpty = items.length === 0
  // What an empty list MEANS is decided by `appliedQuery` — the query the current rows
  // actually answer — never by `q`, the live input. `q` runs ahead of the data by the
  // 300ms debounce, so clearing a no-match search would, on `q`, read as "this user has
  // no projects" and flash the first-run CTA at someone who has plenty. `appliedQuery`
  // is also null before the first fetch lands, which keeps that same CTA off the paint
  // between mount and the effect that asks the server.
  const settled = appliedQuery !== null || error !== null
  const showInitialSkeleton = isEmpty && (loading || !settled)
  const showListError = error !== null && isEmpty
  const showFirstRun = settled && !loading && !showListError && isEmpty && appliedQuery === ''
  const showNoMatches = settled && !loading && !showListError && isEmpty && !!appliedQuery

  return (
    <div
      className="min-h-screen font-manrope flex flex-col"
      style={{ background: 'linear-gradient(160deg, #ffffff 0%, #f0f9f9 100%)' }}
    >
      <Navbar />

      <main className="flex-1 max-w-5xl mx-auto w-full px-6 py-10">
        <div className="flex items-end justify-between gap-4 mb-6 flex-wrap">
          <div>
            <h1 className="text-2xl font-extrabold text-tertiary">Projects</h1>
            <p className="text-sm text-neutral mt-1">Each project is one tool — its app, its description, and its chats.</p>
          </div>
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary/90 transition"
          >
            <Plus size={15} /> New project
          </button>
        </div>

        <div className="relative mb-5 max-w-md">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral" />
          <input
            value={q}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search projects…"
            aria-label="Search projects"
            className="w-full pl-9 pr-3 py-2 rounded-lg border border-bial-border bg-white text-sm text-tertiary placeholder:text-neutral focus:outline-none focus:ring-2 focus:ring-primary/30"
          />
        </div>

        {showInitialSkeleton ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="bg-white border border-bial-border rounded-2xl px-5 py-4 animate-pulse">
                <div className="h-4 bg-gray-100 rounded w-1/2 mb-3" />
                <div className="h-3 bg-gray-50 rounded w-3/4 mb-2" />
                <div className="h-3 bg-gray-50 rounded w-1/4" />
              </div>
            ))}
          </div>
        ) : showListError ? (
          <div className="bg-white border border-danger/20 rounded-2xl py-16 px-6 text-center">
            <p className="text-sm font-semibold text-tertiary">Couldn’t load your projects</p>
            <p className="text-xs text-neutral mt-1 mb-3">{error?.message}</p>
            <button
              onClick={() => {
                reset()
                loadMore()
              }}
              className="text-xs text-primary font-semibold hover:underline"
            >
              Retry
            </button>
          </div>
        ) : showFirstRun ? (
          <div className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center">
            <div className="w-12 h-12 rounded-2xl bg-primary/10 flex items-center justify-center mx-auto mb-3">
              <FolderPlus size={22} className="text-primary" />
            </div>
            <p className="text-sm font-semibold text-tertiary">No projects yet</p>
            <p className="text-xs text-neutral mt-1 mb-4">A project is where your app, its description, and its chats live.</p>
            <button
              onClick={() => setShowCreate(true)}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary/90 transition"
            >
              <Plus size={15} /> Create your first project
            </button>
          </div>
        ) : showNoMatches ? (
          <div className="bg-white border border-bial-border rounded-2xl py-16 px-6 text-center">
            <Search size={24} className="mx-auto text-neutral/50 mb-3" />
            <p className="text-sm font-semibold text-tertiary">No matches</p>
            {/* Quote the query the rows answer, not the one being typed. */}
            <p className="text-xs text-neutral mt-1">No project matches “{appliedQuery}”. Try a different search.</p>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {items.map((project) => (
                <ProjectCard
                  key={project.id}
                  project={project}
                  onOpen={() => openProject(project.id)}
                  onDelete={() => setDeleting(project)}
                />
              ))}
            </div>

            {/* A page-2+ failure keeps the rows we already have, so the full-page error
                state above never fires. Say so here instead of dropping it on the floor —
                a "Load more" that quietly does nothing reads as a frozen button. */}
            {error !== null && !isEmpty && (
              <p role="alert" className="text-xs text-danger text-center mt-4">
                {error.message}
              </p>
            )}

            {hasMore && (
              <div className="flex justify-center mt-6">
                <button
                  onClick={() => loadMore()}
                  disabled={loading}
                  className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-semibold text-primary border border-primary/30 rounded-lg hover:bg-primary/5 transition disabled:opacity-50"
                >
                  {loading ? <Loader2 size={15} className="animate-spin" /> : null} Load more
                </button>
              </div>
            )}
          </>
        )}
      </main>

      {showCreate && <ProjectCreateModal onClose={() => setShowCreate(false)} onCreated={handleCreated} />}
      {deleting !== null && (
        <ProjectDeleteDialog
          project={deleting}
          onClose={() => setDeleting(null)}
          onConfirm={() => handleDelete(deleting)}
        />
      )}

      {toast !== null && (
        <div
          role="alert"
          className="fixed bottom-6 left-1/2 -translate-x-1/2 flex items-center gap-3 bg-red-600 text-white text-sm font-medium px-4 py-2.5 rounded-xl shadow-lg"
        >
          {toast}
          <button onClick={() => setToast(null)} aria-label="Dismiss" className="text-white/80 hover:text-white">
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  )
}
