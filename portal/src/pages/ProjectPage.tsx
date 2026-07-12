/**
 * `/projects/:projectId` — the home for one tool, and its builder.
 *
 * ONE surface, two states of the same page (D-fold):
 *   - the Sandbox build composer (`ProjectBuilder`) renders UNCONDITIONALLY in the
 *     main column — theme selector, sample prompts, and all — whether or not the
 *     project already has an app. It is never collapsed to a "continue" card.
 *   - when the project HAS an app (`project.appId != null`), an App block is added
 *     ABOVE the builder: status badge, Continue building, and the app's live preview
 *     rendered inline (read-only) from its stored current code.
 * The description moves to a text-only right rail; the project's conversations list
 * below the builder as a plain recents list (no BUILD/PLAN badges, no new-chat buttons).
 *
 * Identity model (memory: app identity + flat URL model):
 *   - `appId`/`appStatus` are READ off the project (a LEFT JOIN on the backend);
 *     the portal never fires a mutating provision call just to learn them.
 *   - the "Open app" link is a plain <a> full-page navigation to the backend
 *     runner at `/apps/{appId}`. It must NOT be a react-router <Link>: nginx
 *     proxies `/apps/` to the runner and the Vite dev proxy does not, so an SPA
 *     route there would work on macOS and 404 in the deployed container.
 *   - a new chat opens at a flat `/chat/{uuid}` carrying its project in a transient
 *     `?projectId=&kind=` query; the row does not exist until its first message.
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Pencil, Check, X, MessageSquare, Wrench, ExternalLink, MoreVertical } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import ProjectBuilder from '../components/projects/ProjectBuilder'
import ProjectDescriptionEditor from '../components/projects/ProjectDescriptionEditor'
import LivePreview from '../components/LivePreview'
import { getProject, patchProject } from '../utils/projectApi'
import type { Project, AppStatus } from '../utils/projectApi'
import { ApiError, isRecord } from '../utils/apiError'
import { listProjectConversations, deleteConversation } from '../utils/conversationApi.js'
import { getBuild } from '../utils/builderHistory.js'
import { relativeTime } from '../utils/chatHistory.js'

/** The chat-row shape the home renders; narrowed at the JS-module boundary. */
interface ChatSummary {
  id: string
  kind: string
  title: string
  updatedAt: string
}

/** Badge styling per app lifecycle state; an unknown/absent status uses the muted fallback. */
const STATUS_STYLES: Record<AppStatus, string> = {
  draft: 'bg-surface-muted text-neutral',
  pending: 'bg-primary/10 text-primary',
  approved: 'bg-secondary/10 text-secondary',
  rejected: 'bg-danger/10 text-danger',
  disabled: 'bg-surface-muted text-neutral',
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

/**
 * Pull the app's stored current code out of a build header (`code.current.source`).
 * `getBuild` is untyped JS, so its result arrives as `unknown` — narrow it here rather
 * than trust the inferred shape. Anything unexpected reads as "no code yet", which the
 * inline preview handles as its empty state.
 */
function readAppSource(saved: unknown): string | null {
  if (!isRecord(saved)) return null
  const code = saved.code
  if (!isRecord(code)) return null
  const current = code.current
  if (!isRecord(current)) return null
  return typeof current.source === 'string' ? current.source : null
}

export default function ProjectPage() {
  const { projectId } = useParams()
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

  // The inline read-only preview's source — the app's stored current code. Null until it
  // loads (the App block still shows its badge + Continue building meanwhile) and for a
  // project with no app.
  const [appPreviewCode, setAppPreviewCode] = useState<string | null>(null)

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

  // The inline preview of the project's ONE app. Its current code lives on the newest
  // builder conversation's header (the last build to write into the app), so we read it
  // from there via `getBuild`. Only when the project actually has an app; a project with
  // none shows no App block and no preview.
  useEffect(() => {
    if (!project?.appId) {
      setAppPreviewCode(null)
      return undefined
    }
    const newestBuild = chats
      .filter((c) => c.kind === 'builder' && c.id !== '')
      .reduce<ChatSummary | null>((newest, c) => (newest === null || c.updatedAt > newest.updatedAt ? c : newest), null)
    if (!newestBuild) return undefined
    let active = true
    void (async () => {
      try {
        const saved: unknown = await getBuild(newestBuild.id)
        if (active) setAppPreviewCode(readAppSource(saved))
      } catch {
        // A preview that can't load is not fatal — the App block still shows its badge
        // and Continue building. Leave the preview in its empty state.
      }
    })()
    return () => {
      active = false
    }
  }, [project?.appId, chats])

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

  // Continue building opens a fresh build chat filed under THIS project. A project owns one
  // app and provisioning is idempotent per project, so the new chat continues the same
  // codebase — the server seeds it from the app's current code. Same picker-less handoff as
  // the builder: mint the id, carry the project in a transient query.
  const openBuildChat = () => {
    if (!project) return
    const id = crypto.randomUUID()
    navigate(`/chat/${id}?projectId=${encodeURIComponent(project.id)}&kind=builder`)
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
      <div className="min-h-screen font-manrope flex flex-col bg-bial-bg">
        <Navbar />
        <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-10">
          <div className="h-6 w-48 bg-gray-100 rounded animate-pulse mb-4" />
          <div className="h-24 bg-gray-100 rounded-2xl animate-pulse" />
        </main>
      </div>
    )
  }

  if (loadError || !project) {
    return (
      <div className="min-h-screen font-manrope flex flex-col bg-bial-bg">
        <Navbar />
        <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-10">
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
        </main>
      </div>
    )
  }

  return (
    <div className="min-h-screen font-manrope flex flex-col bg-bial-bg">
      <Navbar />
      <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-10">
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
          {/* Main column: (app block if built) + the always-on builder + conversations */}
          <div className="space-y-6 min-w-0">
            {/* App block — only when the project has an app. NEVER substitutes for the
                builder; it sits above it. */}
            {project.appId !== null && (
              <section className="bg-white border border-bial-border rounded-2xl p-5">
                <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
                  <div className="flex items-center gap-3 flex-wrap">
                    <p className="text-[10px] font-bold uppercase tracking-wider text-neutral">App</p>
                    <span
                      className={`text-[11px] font-bold uppercase tracking-wide px-2.5 py-1 rounded-full ${
                        project.appStatus ? STATUS_STYLES[project.appStatus] : 'bg-surface-muted text-neutral'
                      }`}
                    >
                      {project.appStatus ?? 'unknown'}
                    </span>
                    {project.appStatus === 'approved' && (
                      // Plain anchor, NOT a router <Link>: this leaves the SPA for the
                      // backend runner served at /apps/ by nginx. See the file header.
                      <a
                        href={`/apps/${project.appId}`}
                        className="flex items-center gap-1.5 text-sm font-semibold text-primary hover:underline"
                      >
                        <ExternalLink size={14} /> Open app
                      </a>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={openBuildChat}
                    className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary-dark transition"
                  >
                    <Wrench size={15} /> Continue building
                  </button>
                </div>
                <div className="h-[420px] rounded-xl border border-bial-border overflow-hidden">
                  {/* Read-only: no `config`/token/user means no data-service wiring, so this
                      passive landing preview issues no writes into the app's real store. */}
                  <LivePreview
                    previewCode={appPreviewCode}
                    generating={false}
                    generationStage={0}
                    config={undefined}
                    accessToken={undefined}
                    user={undefined}
                  />
                </div>
              </section>
            )}

            {/* The builder — ALWAYS present, every project, both states. */}
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
                    const isBuild = chat.kind === 'builder'
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
                          className={`w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0 ${
                            isBuild ? 'bg-secondary/10' : 'bg-primary/10'
                          }`}
                        >
                          {isBuild ? (
                            <Wrench size={15} className="text-secondary" />
                          ) : (
                            <MessageSquare size={15} className="text-primary" />
                          )}
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

          {/* Right rail: the description, text-only (Save + Generate, no attach). */}
          <aside data-testid="description-rail" className="lg:sticky lg:top-20 self-start">
            <div className="bg-white border border-bial-border rounded-2xl p-5">
              <ProjectDescriptionEditor
                projectId={project.id}
                description={project.description}
                onProjectUpdate={setProject}
              />
            </div>
          </aside>
        </div>
      </main>
    </div>
  )
}
