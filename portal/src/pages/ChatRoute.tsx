/**
 * `/chat/:chatId` — one flat chat URL for both chat kinds.
 *
 * The project is NOT a path segment. It is a breadcrumb, resolved from the chat's
 * own `projectId`, so a chat keeps one stable address for its whole life.
 *
 * Resolution order, and why each arm exists:
 *
 *  1. The conversation exists → its `kind` picks the page and its `projectId` is
 *     authoritative. The SERVER wins over `?kind=`: a stale or hand-edited query
 *     must never render a builder over a planning transcript.
 *  2. It 404s but the URL carries `?projectId=` → this is a brand-new chat whose row
 *     does not exist yet. There is no header-only create endpoint; the row appears on
 *     the first `appendMessage`. So a new chat opens at
 *     `/chat/{clientMintedId}?projectId={pid}&kind={planning|builder}` and the page
 *     rewrites to the bare `/chat/{id}` once that first append lands. This is what
 *     lets a flat path survive a reload and a cold open.
 *  3. It 404s with no query → the chat is gone (or was never real). Back to /projects.
 *
 * The breadcrumb's project name is resolved separately and never gates rendering: a
 * chat whose project vanished still shows its transcript, with `projectName: null`.
 *
 * Both pages keep their own hydration fetch, so this route's `getConversation` is a
 * second GET on open. That is deliberate and cheap at pilot scale — collapsing it
 * would mean restructuring both pages' hydration effects, which this phase defers.
 */
import { useEffect, useRef, useState } from 'react'
import { Navigate, useParams, useSearchParams } from 'react-router-dom'
import ChatPage from './ChatPage'
import BuilderPage from './BuilderPage'
import { getConversation } from '../utils/conversationApi.js'
import { getProject } from '../utils/projectApi'
import type { Project } from '../utils/projectApi'

export type ChatKind = 'planning' | 'builder'

/** `?kind=` is user-controllable, so anything but the builder opt-in is a planning chat. */
function kindFromQuery(raw: string | null): ChatKind {
  return raw === 'builder' ? 'builder' : 'planning'
}

function kindFromServer(raw: unknown): ChatKind {
  return raw === 'builder' ? 'builder' : 'planning'
}

/** `chatId` is carried so a render can tell whether a resolution still describes the routed chat. */
type Resolution =
  | { status: 'loading' }
  | { status: 'ready'; chatId: string; kind: ChatKind; projectId: string | null }
  | { status: 'gone' }

export default function ChatRoute() {
  const { chatId } = useParams()
  const [search] = useSearchParams()

  // The transient query is read ONCE per chat, inside the effect below. It lives in refs, not
  // in the effect's deps, because the page rewrites `/chat/{id}?projectId=…` to `/chat/{id}`
  // the instant the first append lands — and a dep on the query would re-run this effect at
  // exactly that moment, tear the page down, and abort the very stream that append was for.
  const queryRef = useRef({ projectId: search.get('projectId'), kind: search.get('kind') })
  queryRef.current = { projectId: search.get('projectId'), kind: search.get('kind') }

  const [resolution, setResolution] = useState<Resolution>({ status: 'loading' })
  const [project, setProject] = useState<Project | null>(null)

  useEffect(() => {
    if (!chatId) return undefined
    let alive = true
    // Keep rendering the chat we already resolved while the next one loads. Falling back to a
    // spinner here would unmount the page — and an in-flight build turn lives in its state,
    // with useClaudeAPI aborting the stream on unmount. Only a cold open shows the spinner.
    setResolution((prev) => (prev.status === 'ready' ? prev : { status: 'loading' }))

    const ready = (kind: ChatKind, projectId: string | null): void => {
      setResolution({ status: 'ready', chatId, kind, projectId })
    }

    void (async () => {
      const { projectId: queryProjectId, kind: queryKind } = queryRef.current
      try {
        const conversation = await getConversation(chatId)
        if (!alive) return
        if (conversation) {
          ready(
            kindFromServer(conversation.kind),
            typeof conversation.projectId === 'string' ? conversation.projectId : queryProjectId,
          )
          return
        }
        // Absent row + a project in the query = a chat that has not saved its first
        // message yet. Absent row + no query = nothing to show.
        if (queryProjectId) {
          ready(kindFromQuery(queryKind), queryProjectId)
          return
        }
        setResolution({ status: 'gone' })
      } catch {
        // A genuine load failure (401 is handled by the auth gate, 403-suspended by the
        // interceptor). Fall back to the query if we have one, rather than stranding
        // the user on a spinner.
        if (!alive) return
        if (queryProjectId) ready(kindFromQuery(queryKind), queryProjectId)
        else setResolution({ status: 'gone' })
      }
    })()

    return () => {
      alive = false
    }
  }, [chatId])

  // Read the project once: it names the breadcrumb AND it is how the builder learns
  // whether the project already has an app (`project.appId`) without firing a mutating
  // provision call to find out. A 404 here means the project was deleted out from under
  // an open chat — show the transcript anyway, unnamed. Never redirect on this.
  const projectId = resolution.status === 'ready' ? resolution.projectId : null
  useEffect(() => {
    if (!projectId) return undefined
    let alive = true
    getProject(projectId)
      .then((loaded) => {
        if (alive) setProject(loaded)
      })
      .catch(() => {
        if (alive) setProject(null)
      })
    return () => {
      alive = false
    }
  }, [projectId])

  if (resolution.status === 'gone') return <Navigate to="/projects" replace />

  if (resolution.status === 'loading') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bial-bg">
        <div className="flex gap-1.5" role="status" aria-label="Loading chat">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className="w-2 h-2 bg-primary/60 rounded-full animate-bounce"
              style={{ animationDelay: `${i * 0.15}s` }}
            />
          ))}
        </div>
      </div>
    )
  }

  // Render the chat we have RESOLVED, not the one in the URL. While the next chat loads, the
  // page keeps showing — and keeps owning — the current one, so an in-flight build turn is
  // never torn out from under itself. Everything flips atomically when the fetch lands.
  //
  // `project` may still be resolving (or may have 404'd). The id check guards against handing
  // a chat the app id of a DIFFERENT project: this component stays mounted across chat
  // navigations, so a stale `project` would otherwise outlive the chat it was read for.
  const resolved = project !== null && project.id === resolution.projectId ? project : null
  const shared = {
    chatId: resolution.chatId,
    projectId: resolution.projectId,
    projectName: resolved?.name ?? null,
    // Finding #1: the builder's Relaunch affordance derives from PROJECT-level state, so a
    // fresh conversation in a project with a saved build can still restore its preview.
    //
    // N7: what travels is whether a Relaunch would actually FIND something — not `appId`.
    // The app row is minted by provision, before anything is built, so keying the claim on
    // its existence advertised a saved build for every project whose first build failed.
    // `null` while the project is still resolving is the same "cannot say" the server sends,
    // and it withholds the claim rather than guessing.
    projectHasSavedBuild: resolved?.hasRelaunchableSnapshot ?? null,
  }
  return resolution.kind === 'builder' ? (
    <BuilderPage {...shared} />
  ) : (
    <ChatPage {...shared} />
  )
}
