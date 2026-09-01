/**
 * `/projects/:projectId` — the home for one tool, and its builder.
 *
 * ONE surface for every project, built or not:
 *   - the Sandbox build composer (`ProjectBuilder`) renders UNCONDITIONALLY at the top
 *     of the main column — Build/Plan toggle, sample prompts, and all —
 *     whether or not the project already has an app. It is never collapsed or pushed
 *     below an app card.
 *   - publishing is ONE CHIP beside the project name (R37), in both the plain and the
 *     renaming header rows so it does not move when a rename starts. It reads a single
 *     server-computed state and is not gated on the project having an app: "nothing built
 *     yet" is a state it names, and a citizen should learn that from the same place they
 *     learn everything else about their app rather than from an absence. The Publish card
 *     and the review-status card that used to stack in the rail are gone with it.
 *   - the description sits in a text-only right rail. The passive "View app" preview is
 *     HIDDEN in Phase-1: a stored app is not a running sandbox, and the live preview now
 *     comes only from a per-session build in the conversation surface. A passive stored-app view is
 *     genuinely unavailable until Track DEPLOY provides a live app URL.
 *   - the project's conversations list below the builder as a plain recents list
 *     (no new-chat buttons). Build and chat happen inline here, so there is no separate
 *     "continue building" reroute and no app lifecycle badge. Each row DOES say which kind
 *     of chat it is, in words (R16) — the two icons alone left a non-technical reader
 *     inferring the difference from a wrench. The word comes from `utils/chatKind.ts`, the
 *     single frontend source of what a kind is called; this page states none of its own.
 *
 * Identity model (memory: app identity + flat URL model):
 *   - `appId`/`appStatus` are READ off the project (a LEFT JOIN on the backend); the
 *     portal never fires a mutating provision call just to learn them. `appStatus` is
 *     no longer surfaced here — app lifecycle (draft/approved/…) lives on the admin
 *     registry, not the citizen project page.
 *   - a new chat opens at a flat `/chat/{uuid}` carrying its project in a transient
 *     `?projectId=&kind=` query; the row does not exist until its first message.
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Pencil, Check, X, MoreVertical } from 'lucide-react'
import { Badge } from '../components/ui/badge'
import { useWorkspaceProject } from '../components/workspace/workspaceChannel'
import ProjectBuilder from '../components/projects/ProjectBuilder'
import ProjectDescriptionEditor from '../components/projects/ProjectDescriptionEditor'
import PublishStatusChip from '../components/PublishStatusChip'
import { getProject, patchProject } from '../utils/projectApi'
import type { Project } from '../utils/projectApi'
import { ApiError, isRecord } from '../utils/apiError'
import { listProjectConversations, deleteConversation } from '../utils/conversationApi'
import { relativeTime } from '../utils/chatHistory'
import { chatKindFor } from '../utils/chatKind'
import { markProjectOpened } from '../utils/observe'

/** The chat-row shape the home renders; narrowed at the JS-module boundary. */
interface ChatSummary {
  id: string
  kind: string
  title: string
  updatedAt: string
}

/**
 * `conversationApi` is untyped JavaScript, so its rows reach us as `unknown` in practice even
 * where the inferred type says otherwise. Parse, don't validate: guard the shape once, here.
 */
function narrowChat(row: unknown): ChatSummary {
  if (!isRecord(row)) return { id: '', kind: '', title: '', updatedAt: '' }
  return {
    id: typeof row.id === 'string' ? row.id : '',
    kind: typeof row.kind === 'string' ? row.kind : '',
    title: typeof row.title === 'string' ? row.title : '',
    updatedAt: typeof row.updatedAt === 'string' ? row.updatedAt : '',
  }
}

export default function ProjectPage() {
  const { projectId } = useParams()
  // WHICH PROJECT THE WORKSPACE IS SHOWING. Declared above the early returns below, because the
  // loading and load-error branches are still this project's screen. A held preview address outlives
  // the conversation that published it, and this is the only thing that can retire a stale one — the
  // surface that says nothing leaves the previous project's app framed, invisibly, with nothing able
  // to notice. Costs one call and no request.
  useWorkspaceProject(projectId ?? null)
  const navigate = useNavigate()

  const [project, setProject] = useState<Project | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [chats, setChats] = useState<ChatSummary[]>([])
  const [chatsError, setChatsError] = useState<string | null>(null)
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null)

  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)

  const goToProjects = useCallback(() => navigate('/projects', { replace: true }), [navigate])

  // Load the project. A 404 means it was deleted elsewhere — bounce to the index
  // rather than strand the user on a dead page.
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
        // R105's denominator, and the R104 clock's start. Marked HERE rather than on the raw
        // mount because `hasApp` is only knowable once the project has loaded — a project with
        // nothing built has no app to first-see, and starting a clock for it would make this
        // number and the sandbox-first number answer different questions. `markProjectOpened`
        // is idempotent per project id per page load, which is also the StrictMode guard.
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

  // Load the project's chats. Deliberately NOT keyset-paginated like `/projects`:
  // `GET /api/conversations?projectId=` caps at 200 with no cursor. Fine at pilot
  // scale; a documented divergence, not a bug (see conversationApi.listProjectConversations).
  const refreshChats = useCallback(async (): Promise<void> => {
    if (!projectId) return
    try {
      const rows = await listProjectConversations(projectId)
      setChats(rows.map(narrowChat))
      setChatsError(null)
    } catch (err) {
      setChatsError(err instanceof ApiError ? err.message : 'Could not load this project’s chats.')
    }
  }, [projectId])

  useEffect(() => {
    void refreshChats()
  }, [refreshChats])

  // Close the row action menu on Escape or an outside click while it is open.
  useEffect(() => {
    if (!menuOpenId) return undefined
    const onDown = () => setMenuOpenId(null)
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpenId(null)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onEsc)
    }
  }, [menuOpenId])

  const startRename = () => {
    if (!project) return
    setNameDraft(project.name)
    setNameError(null)
    setEditingName(true)
  }

  const submitRename = async (): Promise<void> => {
    if (!project) return
    const trimmed = nameDraft.trim()
    // Blocked client-side BEFORE any request: the server 400s on name:null and
    // 422s on "". A whitespace-only name never reaches the wire.
    if (trimmed === '') {
      setNameError('Name cannot be empty.')
      return
    }
    if (trimmed === project.name) {
      setEditingName(false)
      return
    }
    try {
      const updated = await patchProject(project.id, { name: trimmed })
      setProject(updated)
      setEditingName(false)
      setNameError(null)
    } catch (err) {
      setNameError(err instanceof ApiError ? err.message : 'Could not rename. Try again.')
    }
  }

  const handleDeleteChat = useCallback(
    async (id: string): Promise<void> => {
      setMenuOpenId(null)
      setChats((prev) => prev.filter((c) => c.id !== id)) // optimistic
      try {
        await deleteConversation(id)
      } catch {
        void refreshChats() // reconcile — the row reappears if the delete didn't land
      }
    },
    [refreshChats],
  )

  if (loading) {
    return (
      <main className="flex-1 min-h-0 overflow-y-auto">
        <div className="max-w-6xl mx-auto w-full px-6 py-10">
          <div className="h-6 w-48 bg-gray-100 rounded animate-pulse mb-4" />
          <div className="h-24 bg-gray-100 rounded-2xl animate-pulse" />
        </div>
      </main>
    )
  }

  if (loadError || !project) {
    return (
      <main className="flex-1 min-h-0 overflow-y-auto">
        <div className="max-w-6xl mx-auto w-full px-6 py-10">
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
    // AN OUTLET CHILD, NOT A ROOT. The page frame and the navbar belong to the workspace shell,
    // which is a full-height frame with no document scroll — so this surface declares its OWN
    // scroller. That also re-bases the description rail below: sticky resolves against the
    // nearest scroll container, and the container has moved.
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className="max-w-6xl mx-auto w-full px-6 py-10">
        <button
          onClick={goToProjects}
          className="flex items-center gap-1 text-sm text-neutral hover:text-primary transition mb-4"
        >
          <ArrowLeft size={15} /> Back to projects
        </button>

        {/* Header: name with inline rename */}
        <div className="mb-6">
          {editingName ? (
            <div className="flex items-center gap-2">
              <input
                aria-label="Project name"
                value={nameDraft}
                autoFocus
                onChange={(e) => setNameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void submitRename()
                  if (e.key === 'Escape') setEditingName(false)
                }}
                className="flex-1 min-w-0 text-2xl font-extrabold text-tertiary bg-white border border-bial-border rounded-lg px-2 py-1 focus:outline-none focus:ring-2 focus:ring-primary/30"
              />
              {/* Second child of an identical `flex items-center gap-2` row in BOTH
                  branches — name, then chip, then the action buttons — so starting a
                  rename does not move it. */}
              <PublishStatusChip projectId={project.id} />
              <button
                type="button"
                aria-label="Save name"
                onClick={() => void submitRename()}
                className="p-2 rounded-lg text-primary hover:bg-primary/5 transition"
              >
                <Check size={18} />
              </button>
              <button
                type="button"
                aria-label="Cancel rename"
                onClick={() => setEditingName(false)}
                className="p-2 rounded-lg text-neutral hover:bg-surface-muted transition"
              >
                <X size={18} />
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-extrabold text-tertiary">{project.name}</h1>
              {/* R37's "beside the project name", literally. NOT gated on `project.appId`:
                  a project with nothing built learns that from the same place it learns
                  everything else about its app, rather than from an absence. */}
              <PublishStatusChip projectId={project.id} />
              <button
                type="button"
                aria-label="Rename project"
                onClick={startRename}
                className="p-1.5 rounded-lg text-neutral hover:text-primary hover:bg-surface-muted transition"
              >
                <Pencil size={15} />
              </button>
            </div>
          )}
          {nameError && <p className="text-xs font-medium text-danger mt-1" role="alert">{nameError}</p>}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_20rem] gap-6">
          {/* Main column: the always-on builder (Build/Plan toggle at the top) + conversations */}
          <div className="space-y-6 min-w-0">
            {/* The builder — ALWAYS present and always first, every project, built or not. */}
            <ProjectBuilder projectId={project.id} />

            {/* Conversations — a plain recents list filed under this project. */}
            <section data-testid="conversations" className="bg-white border border-bial-border rounded-2xl p-5">
              <h2 className="text-sm font-bold text-tertiary mb-4">Conversations · this project</h2>

              {chatsError ? (
                <p className="text-xs text-danger" role="alert">{chatsError}</p>
              ) : chats.length === 0 ? (
                <p className="text-sm text-neutral">No conversations yet — start a build or plan above.</p>
              ) : (
                <div className="space-y-2">
                  {chats.map((chat) => {
                    // One lookup, not a two-way test on a three-valued field: an `assistant`
                    // row used to draw the Plan icon, which a word cannot get away with.
                    const kind = chatKindFor(chat.kind)
                    const menuOpen = menuOpenId === chat.id
                    return (
                      // F-10: the row is a plain container. The title is a real <button> whose
                      // stretched ::after covers the row, so the whole row opens the chat — but
                      // the ⋮ menu is a SIBLING button layered above (z-10), never an interactive
                      // descendant of the title button.
                      <div
                        key={chat.id}
                        className="group relative flex items-center gap-3 bg-white border border-bial-border rounded-xl px-4 py-3 hover:border-primary/40 hover:shadow-sm transition"
                      >
                        <div
                          className={`w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0 ${kind.tint}`}
                        >
                          <kind.Icon size={15} className={kind.iconTint} />
                        </div>
                        <h3 className="min-w-0 flex-1">
                          <button
                            type="button"
                            onClick={() => navigate(`/chat/${chat.id}`)}
                            className="block w-full text-left text-sm font-semibold text-tertiary cursor-pointer rounded-sm after:absolute after:inset-0 after:rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                          >
                            <span className="block truncate">{chat.title || 'Untitled'}</span>
                          </button>
                        </h3>
                        {/* F-10 again: a SIBLING of the title button, never a descendant —
                            the same layering rule the ⋮ menu below follows. */}
                        <Badge variant="outline" className="flex-shrink-0 border-bial-border bg-white">
                          {/* The word is shown; the completion is read but not seen, so the
                              screen says "Build" and the element's text says "Build chat". An
                              `aria-label` on a role-less span is not reliably exposed — it would
                              satisfy a test and help nobody — so the name is built from text. */}
                          {kind.word}
                          {kind.completion && <span className="sr-only">{kind.completion}</span>}
                        </Badge>
                        <span className="text-[11px] text-neutral flex-shrink-0 tabular-nums">
                          {relativeTime(chat.updatedAt)}
                        </span>
                        <div className="relative z-10 flex-shrink-0">
                          <button
                            type="button"
                            onMouseDown={(e) => e.stopPropagation()}
                            onClick={() => setMenuOpenId(menuOpen ? null : chat.id)}
                            aria-label={`Actions for ${chat.title || 'conversation'}`}
                            className="p-1 rounded-lg text-neutral hover:text-primary hover:bg-surface-muted transition"
                          >
                            <MoreVertical size={16} />
                          </button>
                          {menuOpen && (
                            <div
                              onMouseDown={(e) => e.stopPropagation()}
                              className="absolute right-0 top-8 z-20 w-32 bg-white rounded-lg border border-bial-border shadow-xl py-1"
                            >
                              <button
                                type="button"
                                onClick={() => {
                                  setMenuOpenId(null)
                                  navigate(`/chat/${chat.id}`)
                                }}
                                className="w-full text-left px-3 py-2 text-sm text-tertiary hover:bg-bial-bg transition"
                              >
                                Open
                              </button>
                              <button
                                type="button"
                                onClick={() => void handleDeleteChat(chat.id)}
                                className="w-full text-left px-3 py-2 text-sm text-danger hover:bg-red-50 transition"
                              >
                                Delete
                              </button>
                            </div>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </section>
          </div>

          {/* Right rail: the description (text-only, Save + Generate, no attach). Nothing
              about publishing lives here any more — that is the chip beside the project
              name. The passive "View app" preview is HIDDEN in Phase-1 — a stored app is
              not a running sandbox, so there is nothing live to frame here until Track
              DEPLOY provides a deployed app URL. The live preview now comes only from a
              per-session C3 build in the builder. */}
          {/* RE-BASED, not left alone. Sticky resolves against the nearest SCROLL CONTAINER, and
              this page's is now its own `<main>` rather than the document — so the old `top-20`,
              which was the navbar's 56px plus a 24px gap measured from the viewport, would leave
              an 80px hole below a navbar that is no longer inside anything this rail can see.
              `top-6` is the same 24px gap, measured from the container that now exists. */}
          <aside data-testid="description-rail" className="lg:sticky lg:top-6 self-start space-y-4">
            <div className="bg-white border border-bial-border rounded-2xl p-5">
              <ProjectDescriptionEditor
                projectId={project.id}
                description={project.description}
                onProjectUpdate={setProject}
              />
            </div>
            {/* The Publish card and the review-status card that used to stack here are
                GONE. Two cards reading one response could still tell a citizen two
                different things, and did; publishing is now one chip beside the project
                name, which is the one place R37 asks for. The rail is the description. */}
          </aside>
        </div>
      </div>
    </main>
  )
}
