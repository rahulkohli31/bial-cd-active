/**
 * `/chat/:chatId` — one flat URL for the two PROJECT-SCOPED chat kinds. The project is a
 * breadcrumb, resolved from the chat's own `projectId`, never a path segment — so a chat keeps
 * one stable address for its whole life. A generic conversation has no project and no business
 * at this address at all — see arm 1a.
 *
 * WHY THIS EXISTS — resolution order, and why each arm matters:
 *  1. Conversation exists → server's `kind`/`projectId` win over `?kind=`, always: a stale or
 *     hand-edited query must never render a build chat over a planning transcript. Kind no
 *     longer picks a PAGE — one surface renders both, and `kind` is a single declaration inside
 *     it (`ConversationSlot`) — resolved here only because the surface needs it.
 *  1a. Conversation exists and its kind is `generic` → this address redirects to
 *     `/assistant/{chatId}` instead, before anything below runs. A bookmarked or pasted builder
 *     link to a generic chat used to render the builder surface anyway, inside the workspace
 *     shell, with a breadcrumb for a project that does not exist.
 *  2. 404 + `?projectId=` → a brand-new chat: its row is written inside the FIRST TURN's own
 *     transaction (no separate create round-trip), so it opens at
 *     `/chat/{clientId}?projectId=…&kind=…` and rewrites to the bare path once that turn commits.
 *  3. 404 + no query → the chat is gone (or never real). Back to /projects, saying so on arrival
 *     — but only when the platform actually knows it is gone; see `goneNoticeFor`.
 *
 * The breadcrumb's project name never gates rendering — a chat whose project vanished still
 * shows its transcript, with `projectName: null`. Both pages keep their own hydration fetch, so
 * this route's `getConversation` is a deliberate second GET, cheap at pilot scale; collapsing it
 * would mean restructuring both pages' hydration effects, deferred past this phase.
 *
 * The one skipped request is the one guaranteed to fail: a freshly minted chat has no row yet.
 * See `freshlyMinted` below for why the skip is keyed on router state, not the query.
 *
 * TWO THINGS THIS FILE DELIBERATELY DOES NOT DO, so neither gets re-proposed once a third kind
 * exists: it does not filter the bootstrap's `chat_kinds` catalogue down to two entries — that
 * catalogue feeds LABELS for a kind already in hand (`utils/chatKind.ts`'s `chatKindFor`), and
 * nothing walks it to build a list of choices; the rail's kind picker enumerates the portal's
 * own two-valued `ChatKind` union, so a third backend label never reaches it. And it adds no
 * presentation entry for `generic` anywhere a kind is looked up for display — both lookups live
 * inside the workspace shell (the toolbar pill, the rail's picker), which a generic conversation
 * never enters; arm 1a above is what keeps it out.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useLocation, useParams, useSearchParams } from 'react-router-dom'
import ConversationSlot from '../components/workspace/ConversationSlot'
import { usePublishHeading } from '../components/workspace/workspaceChannel'
import { getConversation } from '../utils/conversationApi'
import { getProject } from '../utils/projectApi'
import { markChatOpened } from '../utils/observe'
import { recallChatProject, rememberChatProject } from '../utils/chatProjectMemory'
import { ApiError } from '../utils/apiError'
import { PROJECT_GONE_NOTICE } from './ProjectsPage'
import type { Project } from '../utils/projectApi'

export type ChatKind = 'plan' | 'build'

/** `?kind=` is user-controllable, so anything but the build opt-in is a plan chat. */
function kindFromQuery(raw: string | null): ChatKind {
  return raw === 'build' ? 'build' : 'plan'
}

/**
 * THE ONE PLACE AN UNRECOGNISED KIND BECOMES SOMETHING — `plan`, the least-privileged surface,
 * for the same reason `kindFromQuery` picks it: the value is wire data, not a checked union.
 * `ConversationSurface`'s `kind` prop used to carry its OWN, disagreeing fallback (`build`) for
 * whichever caller omitted it, so a mount that skipped this resolution landed on the more
 * capable surface by accident. That prop is now required — resolving a kind lives here alone,
 * so there is nothing left to disagree with it. (A value of `generic` reaches here in principle
 * — the server's `ChatKind` enum has a third member — but never in practice: `chatId` resolves
 * to `redirectToAssistant` below before this function is ever called on it.)
 */
function kindFromServer(raw: unknown): ChatKind {
  return raw === 'build' ? 'build' : 'plan'
}

/** `chatId` is carried so a render can tell whether a resolution still describes the routed chat. */
type Resolution =
  | { status: 'loading' }
  | { status: 'ready'; chatId: string; kind: ChatKind; projectId: string | null; title: string | null }
  // `notice` is what the bounce SAYS, and `null` is a real answer rather than a missing one — see
  // `goneNoticeFor`. Carried on the resolution instead of decided at the redirect, because the
  // redirect cannot see which of the three failures got it here.
  | { status: 'gone'; notice: string | null }
  // A generic conversation opened at this builder address — see the mount effect's `generic`
  // arm. Carries only `chatId`: no project, no title, nothing this address is about to leave.
  | { status: 'redirectToAssistant'; chatId: string }

/**
 * THE FAILURE THAT EARNS THE SENTENCE, AND THE TWO THAT DO NOT.
 *
 * The catch below is reached by three different things and treats them alike, correctly: they all
 * bounce, because leaving a citizen on a spinner with no answer is worse than moving them somewhere
 * that works. What they do NOT share is whether the platform actually knows anything. A 400 is the
 * server saying that id is not an id — the mangled-link case, and the one status
 * this catch actually sees. A 500 is the server failing to look. A DROPPED CONNECTION never reached
 * it at all — `fetch` rejects with a plain `TypeError`, which is not an `ApiError` and carries no
 * status, and telling someone their chat is gone because their wifi blinked asserts a deletion that
 * never happened.
 *
 * So the notice is narrowed to the arm that knows, and the other two bounce in silence.
 *
 * 400 IS THE ONE THAT REACHES HERE, and this predicate once named two statuses that could not.
 * `GET /v1/conversations/{id}` validates the id by hand against `_ID_RE`
 * (`backend/src/api/v1/conversations/router.py`) and answers a malformed token with **400**; it
 * declares the path param as a plain `str`, so FastAPI never validates it and **422 is unreachable**.
 * A **404** never arrives either — `getConversation` answers one with `null` (`conversationApi.ts`),
 * which the arm above this catch handles. So the original `404 || 422` matched nothing a citizen
 * could actually produce: a chat link a mail client had wrapped (a space, a `<`, a trailing `.`)
 * bounced to the list in SILENCE — exactly the failure this predicate exists to catch. 404 is
 * kept beside 400 because it is the same class of answer and costs nothing if that null-ing
 * ever changes; the dead 422 is gone.
 */
function goneNoticeFor(err: unknown): string | null {
  return err instanceof ApiError && (err.status === 400 || err.status === 404) ? PROJECT_GONE_NOTICE : null
}

/**
 * THE ARRIVAL THAT RESOLVES WITHOUT ASKING ANYBODY — a chat this session just minted, carrying the
 * project it belongs to.
 *
 * Both halves are needed and neither is enough. The marker says the row does not exist yet, so the
 * GET can only 404; the query is the only place the answer can then come from without a request.
 * With the marker and no project there IS nothing to render from, so the fetch happens after all.
 *
 * NAMED ONCE BECAUSE TWO PLACES HAVE TO AGREE ABOUT IT: the effect that skips the guaranteed-404
 * GET, and the wait board, which must not narrate a load that is never going to happen. They were
 * the same condition written twice for about ten minutes and that is exactly long enough for them
 * to drift — and the expensive direction of a drift is this one, a real request left unnarrated.
 */
function resolvesWithoutAsking(freshlyMinted: boolean, queryProjectId: string | null): boolean {
  return freshlyMinted && queryProjectId !== null
}

export default function ChatRoute() {
  const { chatId } = useParams()
  const [search] = useSearchParams()

  // The transient query is read ONCE per chat, inside the effect below. It lives in refs, not
  // in the effect's deps, because the page rewrites `/chat/{id}?projectId=…` to `/chat/{id}`
  // the instant the first append lands — and a dep on the query would re-run this effect at
  // exactly that moment, tear the page down, and abort the very stream that append was for.
  const queryRef = useRef({ projectId: search.get('projectId'), kind: search.get('kind') })
  queryRef.current = { projectId: search.get('projectId'), kind: search.get('kind') }

  // "THIS session just minted this id", set by every mint site (planning's new-chat control,
  // Launch Builder, ProjectBuilder's Start Chat). Its row doesn't exist until the send path
  // creates it, so its `getConversation` is a guaranteed 404 — the only request worth skipping.
  // ROUTER STATE, NOT THE QUERY: `?kind=` is user-controllable and a saved chat's URL is only
  // rewritten to the bare path after its first append, so a bookmarked `/chat/{id}?kind=build`
  // for an already-saved chat is ordinary — skipping on "the URL has query params" would hand it
  // an attacker- or accident-supplied kind instead of the server's. Router state cannot survive a
  // reload or travel in a link, so absence always means "ask the server", the safe default.
  const location = useLocation()
  const freshlyMintedRef = useRef(false)
  freshlyMintedRef.current = (location.state as { freshlyMinted?: unknown } | null)?.freshlyMinted === true

  const [resolution, setResolution] = useState<Resolution>({ status: 'loading' })
  const [project, setProject] = useState<Project | null>(null)

  useEffect(() => {
    if (!chatId) return undefined
    let alive = true
    // Keep rendering the chat we already resolved while the next one loads. Falling back to a
    // spinner here would unmount the surface — and an in-flight turn's subscription lives in
    // its state, aborted on unmount. Only a cold open shows the spinner.
    setResolution((prev) => (prev.status === 'ready' ? prev : { status: 'loading' }))

    // THE TITLE IS ALREADY STORED AND ALREADY RETURNED — it was simply never read back.
    // It is set when the row is created, derived from the chat's first message, so a
    // freshly minted chat legitimately has none until that message lands. `null` is that case and
    // the row names the kind instead; it is never an error and never a spinner.
    const ready = (kind: ChatKind, projectId: string | null, title: string | null = null): void => {
      // The chat-open count that feeds the project-to-chat drop-off ratio tracked for
      // observability (`1 − project_opened_chat / project_opened`), marked at THE one seam
      // every arm passes through — freshly-minted, server-resolved, query fallback and load
      // failure alike — rather than on the three handlers that navigate here. Those live in two
      // components that other work is mid-rewrite of, and three chances to drop one is three too
      // many for a counter whose whole purpose is a before/after comparison across those same
      // rewrites. This seam also knows the SERVER-AUTHORITATIVE project id, and it fires exactly
      // once per chat open, deep links included — which is precisely the case `markChatOpened`
      // refuses, because a project this load never opened has no denominator to be the
      // numerator of.
      markChatOpened(projectId)
      // AND REMEMBERED FOR THE NEXT LOAD WINDOW, at the same one seam and for the same reason it
      // is the right seam: every arm passes through here knowing the chat's project, and this is
      // the only place that is true. A reload of this chat arrives as a bare `/chat/{id}` — the
      // query is rewritten away the moment the first message lands — so without this the row
      // spends the whole of the next `GET` with no project to name and a back control pointed out
      // of the project the citizen is working in.
      rememberChatProject(chatId, projectId)
      setResolution({ status: 'ready', chatId, kind, projectId, title })
    }

    void (async () => {
      const { projectId: queryProjectId, kind: queryKind } = queryRef.current
      // Read the marker synchronously, before any await, for the same reason the query is read
      // here: the page nulls the router state out from under us once the first append lands.
      const freshlyMinted = freshlyMintedRef.current
      // Both conditions, not just the marker: without a project in the query there is nothing to
      // resolve the chat FROM, so fall through to the fetch rather than resolve to 'gone'. The
      // skip only ever removes a request whose answer we already have.
      if (resolvesWithoutAsking(freshlyMinted, queryProjectId)) {
        ready(kindFromQuery(queryKind), queryProjectId)
        return
      }
      try {
        const conversation = await getConversation(chatId)
        if (!alive) return
        if (conversation) {
          // THE SERVER-RESOLVED KIND, CHECKED BEFORE `ready()` EVER RUNS — never `?kind=`, which
          // a citizen controls and which this route already refuses to trust for anything else.
          // A generic conversation has no project and no toolset this address's surface
          // understands, so it belongs at `/assistant/{chatId}` and never reaches `ready()`: no
          // heading is published, `resolution.projectId` never exists to feed the `getProject`
          // effect below, and `ConversationSlot` — the workspace shell's one child here — never
          // mounts for it.
          if (conversation.kind === 'generic') {
            setResolution({ status: 'redirectToAssistant', chatId })
            return
          }
          ready(
            kindFromServer(conversation.kind),
            typeof conversation.projectId === 'string' ? conversation.projectId : queryProjectId,
            conversation.title || null,
          )
          return
        }
        // Absent row + a project in the query = a chat that has not saved its first
        // message yet. Absent row + no query = nothing to show.
        if (queryProjectId) {
          ready(kindFromQuery(queryKind), queryProjectId)
          return
        }
        // The row is genuinely absent — `getConversation` answers a 404 with `null` rather than by
        // throwing, so this arm, not the catch below, is the ordinary dead-bookmark case.
        setResolution({ status: 'gone', notice: PROJECT_GONE_NOTICE })
      } catch (err) {
        // A genuine load failure (401 is handled by the auth gate, 403-suspended by the
        // interceptor). Fall back to the query if we have one, rather than stranding
        // the user on a spinner.
        if (!alive) return
        if (queryProjectId) ready(kindFromQuery(queryKind), queryProjectId)
        else setResolution({ status: 'gone', notice: goneNoticeFor(err) })
      }
    })()

    return () => {
      alive = false
    }
  }, [chatId])

  // Read the project once: it names the breadcrumb AND tells the builder whether the project
  // already has an app (`project.appId`), without a mutating provision call. A 404 means the
  // project was deleted out from under an open chat — show the transcript anyway, unnamed, never
  // redirect. While resolving, the URL's `?projectId=` stands in when present (an unsaved chat
  // carries it); once nothing is in the URL — the ordinary case, since the query is rewritten
  // away after the first message — this tab's own `remembered` value stands in instead, so the
  // back control isn't sent to the projects list out of the project it's actually in. A chat this
  // browser has never seen has neither, and keeps the neutral shape: a memory, never a guess.
  const remembered = useMemo(() => recallChatProject(chatId), [chatId])
  const projectId =
    resolution.status === 'ready'
      ? resolution.projectId
      : (queryRef.current.projectId ?? remembered)

  // Published from the ROUTE, not the surface below it — this component stays mounted for the
  // address's whole life, loading branch included, where the row still needs full height and a
  // working back control before anything resolves. The row's SHAPE comes from the address (the
  // shell reads the pathname), not from here — every field below is an ANSWER that may
  // legitimately be null (a freshly minted chat has no title, a deleted project has no name)
  // without changing that shape. Kind is never guessed from the query the way the project is:
  // `?kind=` is user-controllable and can be stale, so publishing it risks flashing the wrong
  // pill — null just costs the row its pill for one fetch.
  usePublishHeading({
    projectId,
    projectName: project !== null && project.id === projectId ? project.name : null,
    chatTitle: resolution.status === 'ready' ? resolution.title : null,
    chatKind: resolution.status === 'ready' ? resolution.kind : null,
  })

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

  // REPLACED, NOT PUSHED, so Back from `/assistant/{chatId}` lands wherever opened THIS address,
  // never bounces back here. Nothing below this ever runs for a generic chat: no wait board, no
  // `ConversationSlot`.
  if (resolution.status === 'redirectToAssistant') {
    return <Navigate to={`/assistant/${resolution.chatId}`} replace />
  }

  // The bounce is unconditional; only the sentence it carries is not. `undefined` leaves the
  // history entry stateless, which is what a silent arrival looks like on the other side.
  if (resolution.status === 'gone') {
    return (
      <Navigate
        to="/projects"
        replace
        state={resolution.notice === null ? undefined : { notice: resolution.notice }}
      />
    )
  }

  if (resolution.status === 'loading') {
    // `flex-1 min-h-0`, NOT `min-h-screen`. This arm renders inside the workspace shell's outlet
    // column, which is 100vh minus the navbar and `overflow-hidden`; a child asserting a full
    // viewport height cannot shrink into it, so it overflows and the spinner is clipped low by the
    // navbar's height on every cold chat open. The shell owns the one height model now — surfaces
    // fill the column they are given.
    // AND IT SAYS SO IN WORDS. The three dots are `animate-bounce`, which the reduce-motion
    // block now freezes — so for a citizen who asked their operating system to stop motion this
    // arm was three static dots and nothing to read. EVERY WAIT IN THIS PRODUCT CARRIES A
    // SENTENCE, not motion alone: motion is the decoration, the words are the answer, and a
    // surface whose only wait indicator can be frozen away must have both.
    //
    // The sentence IS the announcement: `role="status"` wraps it rather than an `sr-only` copy
    // sitting beside it, because two elements carrying one sentence is that sentence read twice
    // (`Announcer.tsx` records it breaking three tests). The old `aria-label` is gone with it —
    // a label on a region whose text says the same thing is the same duplication in another
    // spelling, and the visible words are what a reader should get.
    //
    // ON ONE ARRIVAL IT IS NOT ANNOUNCED AT ALL, AND THE WORDS STILL ARE.
    //
    // A freshly minted chat resolves inside the mount effect with no request (see
    // `resolvesWithoutAsking`), so this board is one committed frame on the way to the surface
    // rather than a wait — and it lands in the middle of a sentence somebody else is already
    // saying. `RailComposer` raises the workspace's start flag BEFORE its request and navigates
    // AFTER it, so the pane in the next column has been announcing "Getting your app ready." for
    // this whole moment. A second polite region opening and closing inside that frame makes two
    // sentences for one wait, and the surface publishing the same state again makes three — the
    // three-in-two-seconds relay measured on 2026-09-10. This is the middle one, and it is the
    // one with nothing behind it: there is no load here to report on.
    //
    // ONLY THE INTERRUPTION IS DROPPED. The dots and the sentence stay exactly as they are — a
    // citizen who does see that frame should be able to read what it is, and the reduced-motion
    // rule above is about words existing, not about them being announced. `aria-busy` goes with
    // the region because it is a claim about the same absent load.
    const announced = !resolvesWithoutAsking(freshlyMintedRef.current, queryRef.current.projectId)
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center bg-bial-bg">
        <div
          className="flex flex-col items-center gap-3"
          role={announced ? 'status' : undefined}
          aria-live={announced ? 'polite' : undefined}
          aria-busy={announced ? 'true' : undefined}
          data-testid="chat-wait"
        >
          <div className="flex gap-1.5" aria-hidden="true">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="w-2 h-2 bg-primary/60 rounded-full animate-bounce"
                style={{ animationDelay: `${i * 0.15}s` }}
              />
            ))}
          </div>
          <p className="text-sm font-medium text-neutral">Loading this chat…</p>
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
  // THE KIND BRANCH IS GONE. This route resolves the conversation and hands the resolution —
  // kind included — to the slot, which mounts ONE surface for both kinds and never compares them.
  // `kind` still travels because the surface's placeholder and the breadcrumb say which chat this
  // is; nothing branches on it.
  return (
    <ConversationSlot
      // THE ROW'S TITLE, LEARNED FROM THE SURFACE THAT DERIVES IT. `resolution` is this route's
      // own state, so the heading published above still has exactly one author.
      onTitleDerived={(title) =>
        setResolution((held) => (held.status === 'ready' && !held.title ? { ...held, title } : held))
      }
      conversation={{
        chatId: resolution.chatId,
        kind: resolution.kind,
        projectId: resolution.projectId,
        projectName: resolved?.name ?? null,
        // Relaunch affordance derives from PROJECT-level state, so a fresh conversation in a
        // project with a saved build can still restore its preview. What travels is whether a
        // Relaunch would actually FIND something — not `appId`, which is minted at provision
        // before anything is built, so keying on its existence would advertise a saved build for
        // every project whose first build failed. `null` while resolving is the server's own
        // "cannot say", not a guess. The server answers from the recovery copy OR the saved
        // bundle (what a restore actually consults) — this is the COLD-LOAD value; once the
        // preview poll lands, the surface prefers `restorable`, the same predicate asked fresher.
        projectHasSavedBuild: resolved?.hasRelaunchableSnapshot ?? null,
      }}
    />
  )
}
