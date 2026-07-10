/**
 * `/projects/:projectId` — the home for one tool. It shows the project's name
 * (renamed inline), its single description surface, its one app, and every chat
 * filed under it, with the two entry points that start a plan chat or a build chat.
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
import { ArrowLeft, Pencil, Check, X, MessageSquare, Wrench, ExternalLink } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import ProjectDescriptionEditor from '../components/projects/ProjectDescriptionEditor'
import { getProject, patchProject } from '../utils/projectApi'
import type { Project, AppStatus } from '../utils/projectApi'
import { ApiError, isRecord } from '../utils/apiError'
import { listProjectConversations } from '../utils/conversationApi.js'

type ChatKind = 'planning' | 'builder'

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

export default function ProjectPage() {
  const { projectId } = useParams()
  const navigate = useNavigate()

  const [project, setProject] = useState<Project | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [chats, setChats] = useState<ChatSummary[]>([])
  const [chatsError, setChatsError] = useState<string | null>(null)

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
  useEffect(() => {
    if (!projectId) return
    let active = true
    void (async () => {
      try {
        const rows = await listProjectConversations(projectId)
        if (!active) return
        setChats(rows.map(narrowChat))
        setChatsError(null)
      } catch (err) {
        if (!active) return
        setChatsError(err instanceof ApiError ? err.message : 'Could not load this project’s chats.')
      }
    })()
    return () => {
      active = false
    }
  }, [projectId])

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

  const openNewChat = (kind: ChatKind) => {
    if (!project) return
    // Flat chat URL; the project rides along in a transient query until the first
    // message creates the row, after which the chat page rewrites to bare /chat/{id}.
    const id = crypto.randomUUID()
    navigate(`/chat/${id}?projectId=${encodeURIComponent(project.id)}&kind=${kind}`)
  }

  if (loading) {
    return (
      <div className="min-h-screen font-manrope flex flex-col bg-bial-bg">
        <Navbar />
        <main className="flex-1 max-w-4xl mx-auto w-full px-6 py-10">
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
        <main className="flex-1 max-w-4xl mx-auto w-full px-6 py-10">
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
      <main className="flex-1 max-w-4xl mx-auto w-full px-6 py-10">
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

        {/* App card — read from the project; never provision to learn state */}
        <div className="bg-white border border-bial-border rounded-2xl p-5 mb-6">
          <p className="text-[10px] font-bold uppercase tracking-wider text-neutral mb-2">App</p>
          {project.appId === null ? (
            <p className="text-sm text-neutral">No app yet — start a build chat.</p>
          ) : (
            <div className="flex items-center gap-3 flex-wrap">
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
          )}
        </div>

        {/* Description — author or generate */}
        <div className="bg-white border border-bial-border rounded-2xl p-5 mb-6">
          <ProjectDescriptionEditor
            projectId={project.id}
            description={project.description}
            onProjectUpdate={setProject}
          />
        </div>

        {/* Chats — both kinds, filed under this project */}
        <div className="bg-white border border-bial-border rounded-2xl p-5">
          <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
            <h2 className="text-sm font-bold text-tertiary">Chats</h2>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => openNewChat('planning')}
                className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-semibold text-primary border border-primary/30 rounded-lg hover:bg-primary/5 transition"
              >
                <MessageSquare size={15} /> New plan chat
              </button>
              <button
                type="button"
                onClick={() => openNewChat('builder')}
                className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary-dark transition"
              >
                <Wrench size={15} /> New build chat
              </button>
            </div>
          </div>

          {chatsError ? (
            <p className="text-xs text-danger" role="alert">{chatsError}</p>
          ) : chats.length === 0 ? (
            <p className="text-sm text-neutral">No chats yet — start a plan or build chat above.</p>
          ) : (
            <div className="space-y-2">
              {chats.map((chat) => {
                const isBuild = chat.kind === 'builder'
                return (
                  <button
                    type="button"
                    key={chat.id}
                    onClick={() => navigate(`/chat/${chat.id}`)}
                    className="group w-full flex items-center gap-3 bg-white border border-bial-border rounded-xl px-4 py-3 cursor-pointer hover:border-primary/40 hover:shadow-sm transition text-left"
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
                    <span className="flex-1 min-w-0 text-sm font-semibold text-tertiary truncate">
                      {chat.title || 'Untitled'}
                    </span>
                    <span
                      className={`text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full ${
                        isBuild ? 'bg-secondary/10 text-secondary' : 'bg-primary/10 text-primary'
                      }`}
                    >
                      {isBuild ? 'Build' : 'Plan'}
                    </span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
