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
 *
 * The ONE request we do skip is the one that cannot succeed: a chat this session just
 * minted has no row yet (U7 defers creation to the send path), so its GET is a
 * guaranteed 404 — doubled by the two hydration fetches and doubled again by StrictMode
 * in dev. See `freshlyMinted` below for why the skip is keyed on router state and not on
 * the query.
 */
import { useEffect, useRef, useState } from 'react'
import { Navigate, useLocation, useParams, useSearchParams } from 'react-router-dom'
import ConversationSlot from '../components/workspace/ConversationSlot'
import { getConversation } from '../utils/conversationApi'
import { getProject } from '../utils/projectApi'
import { markChatOpened } from '../utils/observe'
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

  // "THIS session just minted this id", set by every mint site (ChatPage's New Chat and Launch
  // Builder, ProjectBuilder's Start Chat). Its row does not exist until the send path creates it,
  // so its `getConversation` is a guaranteed 404 — the only request worth skipping.
  //
  // WHY ROUTER STATE AND NOT THE QUERY. `?kind=` is user-controllable, and a saved chat's URL is
  // rewritten to the bare `/chat/{id}` only after its FIRST append — so a shared or bookmarked
  // `/chat/{id}?kind=builder` for an already-saved chat is a perfectly ordinary URL, and skipping
  // on "the URL has query params" would hand it its kind from that attacker- or accident-supplied
  // string instead of from the server. Router state cannot do that: it does not survive a reload
  // and it does not travel in a link, so the marker can only ever be present on the one navigation
  // that actually minted the id. Absence of the marker means "ask the server", which is the safe
  // default in every ambiguous case.
  const location = useLocation()
  const freshlyMintedRef = useRef(false)
  freshlyMintedRef.current = (location.state as { freshlyMinted?: unknown } | null)?.freshlyMinted === true

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
      // R105's numerator, marked at THE one seam every arm passes through — freshly-minted,
      // server-resolved, query fallback and load failure alike — rather than on the three
      // handlers that navigate here. Those live in two components that other work is mid-rewrite
      // of, and three chances to drop one is three too many for a counter whose whole purpose is
      // a before/after comparison across those same rewrites. This seam also knows the
      // SERVER-AUTHORITATIVE project id, and it fires exactly once per chat open, deep links
      // included — which is precisely the case `markChatOpened` refuses, because a project this
      // load never opened has no denominator to be the numerator of.
      markChatOpened(projectId)
      setResolution({ status: 'ready', chatId, kind, projectId })
    }

    void (async () => {
      const { projectId: queryProjectId, kind: queryKind } = queryRef.current
      // Read the marker synchronously, before any await, for the same reason the query is read
      // here: the page nulls the router state out from under us once the first append lands.
      const freshlyMinted = freshlyMintedRef.current
      // Both conditions, not just the marker: without a project in the query there is nothing to
      // resolve the chat FROM, so fall through to the fetch rather than resolve to 'gone'. The
      // skip only ever removes a request whose answer we already have.
      if (freshlyMinted && queryProjectId) {
        ready(kindFromQuery(queryKind), queryProjectId)
        return
      }
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
  // THE KIND BRANCH MOVED, IT DID NOT GO AWAY. This route still resolves the conversation and now
  // hands the resolution — kind included — to the one slot that mounts a body for it, so the
  // largest kind comparison in the product has exactly one home instead of being the reason two
  // page components exist. Plan D deletes it from the slot when the unified surface lands.
  return (
    <ConversationSlot
      conversation={{
        chatId: resolution.chatId,
        kind: resolution.kind,
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
        //
        // R18: the server now answers this from the recovery copy OR the saved bundle (the pair a
        // restore actually consults), so the builder who never pressed Save is offered their work
        // back. This is the COLD-LOAD value; once the preview poll lands, BuilderPage prefers its
        // `restorable`, which is the same predicate asked more recently.
        projectHasSavedBuild: resolved?.hasRelaunchableSnapshot ?? null,
      }}
    />
  )
}
