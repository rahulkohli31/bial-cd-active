/**
 * ChatRoute — the flat `/chat/:chatId` kind dispatcher.
 *
 * The SLOT is stubbed so this file tests exactly one thing: what the route resolves — which
 * conversation, of which kind, in which project — and when it bails to /projects instead.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useLayoutEffect, type ReactNode } from 'react'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useNavigate, useParams } from 'react-router-dom'

const h = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getProject: vi.fn(),
  authFetch: vi.fn(),
}))

// The REAL `observe` module runs here — its deep-link guard IS what these tests are about, and a
// mocked module would prove only that a function was called. Only the transport is replaced.
// Each test uses its OWN project id — module state is per page load, so a shared id would let
// one test's mark silence the next one's.
vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  authFetch: h.authFetch,
}))

vi.mock('../../utils/conversationApi.js', () => ({ getConversation: h.getConversation }))
// SPREAD FROM THE REAL MODULE, not listed. `ChatRoute` now imports the dead-address sentence from
// `ProjectsPage` (one string, three surfaces), and that page imports names this file has
// no opinion about; against a hand-written factory Vitest throws "No X export is defined on the
// mock" at IMPORT time and the whole file fails. `getProject` is still the only override.
vi.mock('../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/projectApi')>()),
  getProject: h.getProject,
}))

/**
 * ONE STUB, AND IT IS THE SLOT: the route's contract is the VALUE it hands the slot — a
 * resolved conversation, kind included — so stubbing the slot lets that value be asserted
 * directly; stubbing the surface underneath would leave the kind invisible.
 *
 * Named, not an anonymous arrow: this stub calls `useNavigate`, and a hook is only legal inside
 * something lint can SEE is a component — `default: () => …` reads as "default", lowercase, so
 * "not a component", and errors.
 */
vi.mock('../../components/workspace/ConversationSlot', () => ({
  default: function ConversationSlotStub({
    conversation,
    onTitleDerived,
  }: {
    conversation: { chatId: string; kind: string; projectId: string | null; projectName: string | null }
    // The surface derives a title from the first message of a chat whose row had none, and hands
    // it BACK to this route — see `onTitleDerived` in `ChatRoute`. Accepted here so the merge is
    // reachable from a test at all; the stub used to drop it silently.
    onTitleDerived?: (title: string) => void
  }) {
    const navigate = useNavigate()
    const { chatId, kind, projectId, projectName } = conversation
    return (
      <div data-testid="conversation-slot" data-kind={kind}>
        {`${kind}|${chatId}|${projectId}|${projectName}`}
        <button onClick={() => navigate(`/chat/${chatId}`, { replace: true })}>drop query</button>
        <button onClick={() => navigate('/chat/c2')}>go to c2</button>
        <button onClick={() => onTitleDerived?.('Add an out-time column')}>derive a title</button>
      </div>
    )
  },
}))

import ChatRoute from '../ChatRoute'
import { PROJECT_GONE_NOTICE } from '../ProjectsPage'
import { ApiError } from '../../utils/apiError'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  useWorkspaceHeading,
} from '../../components/workspace/workspaceChannel'
import { beaconsFrom } from './_observeBeacons'
import { markProjectOpened } from '../../utils/observe'

/**
 * What the toolbar row would name, read straight off the channel — the row itself has its own
 * suite; what's only observable HERE is the value this route PUBLISHES. Rendered as one string
 * so a single assertion covers all four fields and a wrong one cannot hide behind a right one.
 */
function HeadingProbe() {
  const { projectId, projectName, chatKind, chatTitle } = useWorkspaceHeading()
  return <span data-testid="heading">{`${projectId}|${projectName}|${chatKind}|${chatTitle}`}</span>
}


/**
 * WHERE A BOUNCE LANDS, AND WHAT IT SAID ON THE WAY.
 *
 * The real `ProjectsPage` is not mounted here — its own arrival rendering is pinned in
 * `ProjectPage.test.tsx`, which is where the two-page behaviour lives. What is only observable
 * from THIS side is the sentence the route hands the navigation, and `null` for the failures that
 * have not earned one. The `projects-index` testid is unchanged so the existing bail cases keep
 * asserting exactly what they always did.
 */
function ProjectsIndexProbe() {
  const carried = (useLocation().state as { notice?: unknown } | null)?.notice
  return (
    <div data-testid="projects-index">
      projects
      <span data-testid="arrival-notice">{typeof carried === 'string' ? carried : ''}</span>
    </div>
  )
}

/**
 * WHERE A GENERIC CONVERSATION'S REDIRECT LANDS. The real `AssistantPage` is not mounted here —
 * it belongs to a different suite — so this stands in for its address only, the one thing this
 * route is answerable for: DID it send the citizen there, not what that page then shows.
 */
function AssistantAddressProbe() {
  const { chatId } = useParams()
  return <div data-testid="assistant-address">{chatId}</div>
}

/** What the wait board looked like on some commit — see `FirstCommitSpy`. */
interface WaitFrame {
  present: boolean
  text: string
  role: string | null
  live: string | null
  busy: string | null
}

/**
 * THE WAIT BOARD AS IT WAS ON THE FIRST COMMITTED FRAME, read before anything could erase it.
 *
 * A freshly minted chat resolves inside `ChatRoute`'s MOUNT EFFECT with no request at all, and
 * React runs passive effects after the commit — so by the time `render()` returns, `act` has
 * flushed that effect and the board is already gone from the tree. `queryByTestId('chat-wait')`
 * therefore reads null whether or not the change under test exists: an assert-absence test that
 * false-greens, which is a shape this repo has already been burned by twice.
 *
 * A LAYOUT effect is the one hook that runs inside that commit — after every DOM mutation and
 * before any passive effect — so a sibling holding one sees the frame a screen reader would have
 * been handed. It records only the FIRST such frame; later commits are what the ordinary queries
 * already see.
 */
function FirstCommitSpy({ onto }: { onto: { seen: WaitFrame | null } }) {
  useLayoutEffect(() => {
    if (onto.seen !== null) return
    const el = document.querySelector('[data-testid="chat-wait"]')
    onto.seen = {
      present: el !== null,
      text: el?.textContent ?? '',
      role: el?.getAttribute('role') ?? null,
      live: el?.getAttribute('aria-live') ?? null,
      busy: el?.getAttribute('aria-busy') ?? null,
    }
  })
  return null
}

/**
 * `state` is the freshly-minted marker's carrier. Entries without one stay plain strings so the
 * existing cases exercise the exact same router input they always did. `spy` is opt-in for the
 * same reason: a case that does not pass one renders exactly the tree it always rendered.
 */
function renderRoute(entry: string, state?: unknown, spy?: ReactNode) {
  const [pathname, search = ''] = entry.split('?')
  const initial = state === undefined ? entry : { pathname, search: search ? `?${search}` : '', state }
  // UNDER A REAL CHANNEL, so the heading this route publishes is observable. The publishers all
  // no-op without one, which is exactly how this route's heading went untested.
  return render(
    <WorkspaceChannelProvider value={createWorkspaceChannel()}>
      <MemoryRouter initialEntries={[initial]}>
        <HeadingProbe />
        <Routes>
          <Route path="/chat/:chatId" element={<ChatRoute />} />
          <Route path="/projects" element={<ProjectsIndexProbe />} />
          <Route path="/assistant/:chatId" element={<AssistantAddressProbe />} />
        </Routes>
        {/* LAST, so its layout effect runs after the route's own subtree has been committed. */}
        {spy}
      </MemoryRouter>
    </WorkspaceChannelProvider>,
  )
}

const heading = () => screen.getByTestId('heading').textContent

const conversation = (over: Record<string, unknown> = {}) => ({
  id: 'c1',
  kind: 'plan',
  projectId: 'p1',
  title: 'T',
  messages: [],
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  // EACH TEST IS A FRESH PAGE LOAD. The route remembers a chat's project per tab so the toolbar
  // row has a back target during the next load window, and a memory left behind by the test
  // above would make "a chat this browser has never seen" quietly untrue.
  sessionStorage.clear()
  h.authFetch.mockResolvedValue({ ok: true } as Response)
  h.getProject.mockResolvedValue({ id: 'p1', name: 'VIP Movement', description: null, appId: null, appStatus: null, createdAt: '', updatedAt: '' })
})
afterEach(() => cleanup())

/** The observation bodies this render posted — see `_observeBeacons` for the mock contract. */
const beacons = () => beaconsFrom(h.authFetch)

describe('ChatRoute — kind RESOLUTION (it no longer dispatches)', () => {
  // THE KIND THE ROUTE RESOLVES is the whole remaining contract worth pinning here — the route
  // dispatches to no separate page per kind, and the resolved value decides the server's toolset.
  it('resolves a plan conversation as plan', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'plan' }))
    renderRoute('/chat/c1')
    expect((await screen.findByTestId('conversation-slot')).getAttribute('data-kind')).toBe('plan')
  })

  it('resolves a build conversation as build', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'build' }))
    renderRoute('/chat/c1')
    expect((await screen.findByTestId('conversation-slot')).getAttribute('data-kind')).toBe('build')
  })

  it('lets the SERVER win when its kind disagrees with ?kind=', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'plan' }))
    // The query has to name the REAL opt-in value to be a genuine disagreement — `?kind=build`
    // (the retired word) matches neither branch of `kindFromQuery`'s `raw === 'build'` check, so
    // both the query and the server would have resolved to `plan` regardless of which one won.
    renderRoute('/chat/c1?kind=build')
    // A stale or hand-edited query must never decide the toolset over a plan transcript.
    expect((await screen.findByTestId('conversation-slot')).getAttribute('data-kind')).toBe('plan')
  })

  it('issues exactly ONE getConversation to resolve the kind', async () => {
    h.getConversation.mockResolvedValue(conversation())
    renderRoute('/chat/c1')
    await screen.findByTestId('conversation-slot')
    expect(h.getConversation).toHaveBeenCalledTimes(1)
  })

  // THE TWO FALLBACK SITES NOW AGREE, because there is only one left. `ConversationSurface`'s
  // `kind` prop used to carry its own default (`build`) for whatever this route did not resolve;
  // it is required now, so `kindFromServer`'s `plan` is the only answer a caller can ever receive
  // for a value it does not recognise — proven here by reading it straight off what the slot
  // was actually handed, not by re-deriving it.
  //
  // Mutation receipt: swap `kindFromServer`'s `raw === 'build' ? 'build' : 'plan'` for `raw ===
  // 'build' ? 'build' : 'generic' as ChatKind` and this goes red on the `data-kind` assertion —
  // 'plan' vs 'generic' — while every other test in this describe block stays green.
  it('resolves a kind the server does not recognise to plan, the one remaining safe default', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'a-future-kind' }))
    renderRoute('/chat/c1')
    expect((await screen.findByTestId('conversation-slot')).getAttribute('data-kind')).toBe('plan')
  })
})

describe('ChatRoute — a generic conversation belongs at its assistant address', () => {
  // Covers AE8, reframed at the route level per the plan: the generic SURFACE is `AssistantPage`'s
  // own suite to pin; what this route is answerable for is whether it sends a generic conversation
  // there at all.
  it('a generic conversation opened at the builder address lands on its assistant address', async () => {
    h.getConversation.mockResolvedValue(conversation({ id: 'g1', kind: 'generic', projectId: null }))
    renderRoute('/chat/g1')
    expect((await screen.findByTestId('assistant-address')).textContent).toBe('g1')
  })

  // THE ROW CARRIES A `projectId` ON PURPOSE, even though a real generic conversation's is
  // always `null` (the server-side invariant this row's own factory otherwise pins) — this
  // fixture is what proves the guard fires BEFORE anything downstream ever reads `projectId`,
  // rather than proving something already true of an all-null row for an unrelated reason.
  //
  // Mutation receipt, two mutations for the three assertions:
  //  1. Delete the `conversation.kind === 'generic'` guard entirely (or keep it but drop its
  //     `return`, letting `ready()` also run — `setResolution` does not merge, so the LATER call
  //     wins either way). The LIVENESS assertion is what goes red:
  //     `findByTestId('assistant-address')` times out because the slot renders instead — exactly
  //     the failure liveness-first ordering exists to catch, since the two absences below would
  //     otherwise read as a pass on a page that never arrived.
  //  2. Keep the guard and its `return`, but add a stray `getProject(...)` call inside it (the
  //     shape a "prefetch it anyway" regression would take). Liveness and the slot assertion stay
  //     green; only the last line goes red.
  it('never mounts the workspace shell or attempts a project lookup on the way', async () => {
    h.getConversation.mockResolvedValue(conversation({ id: 'g1', kind: 'generic' }))
    renderRoute('/chat/g1')

    // LIVENESS FIRST: the redirect really did land, so the absences below describe a route that
    // went straight there rather than one that crashed before reaching either.
    expect(await screen.findByTestId('assistant-address')).toBeTruthy()
    expect(screen.queryByTestId('conversation-slot')).toBeNull()
    expect(h.getProject).not.toHaveBeenCalled()
  })
})

describe('ChatRoute — a conversation whose row does not exist yet', () => {
  it('takes the kind from ?kind= when the id 404s but a ?projectId= is present', async () => {
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new-uuid?projectId=p1&kind=build')
    // The row is written inside the first TURN's transaction; until then only the query knows
    // the project.
    const slot = await screen.findByTestId('conversation-slot')
    expect(slot.getAttribute('data-kind')).toBe('build')
    expect(slot.textContent).toContain('|new-uuid|p1|')
  })

  it('defaults an unrecognised ?kind= to plan rather than trusting it', async () => {
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new-uuid?projectId=p1&kind=wat')
    expect((await screen.findByTestId('conversation-slot')).getAttribute('data-kind')).toBe('plan')
  })

  it('redirects to /projects when the id 404s with NO query', async () => {
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/ghost')
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
  })

  it('redirects to /projects when the literal word "new" is routed as a chat id', async () => {
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new')
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
  })
})

describe('ChatRoute — the GET that cannot succeed', () => {
  // A chat this session just minted has no row until the send path creates it, so its
  // `getConversation` is a guaranteed 404 on every cold new-chat open.
  it('a freshly-minted open issues NO getConversation and renders from the query', async () => {
    renderRoute('/chat/fresh-id?projectId=p1&kind=build', { freshlyMinted: true })

    expect(await screen.findByTestId('conversation-slot')).toBeTruthy()
    expect(screen.getByTestId('conversation-slot').textContent).toContain('|fresh-id|p1|')
    expect(h.getConversation).not.toHaveBeenCalled()
  })

  // THE IMPORTANT ONE. Keying the skip on "the URL has query params" would be a security
  // regression: `?kind=` is user-controllable, and a saved chat's URL only loses its query
  // after the FIRST append — so a shared `/chat/{id}?kind=build` for an already-saved plan
  // chat is ordinary and MUST still be resolved by the server. Router state, unlike the query,
  // does not survive a reload or travel in a link, which is what makes the marker safe to trust.
  it('an open WITHOUT the marker still fetches, and the server beats a conflicting ?kind=', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'plan' }))
    renderRoute('/chat/c1?projectId=p1&kind=build')

    expect((await screen.findByTestId('conversation-slot')).getAttribute('data-kind')).toBe('plan')
    expect(h.getConversation).toHaveBeenCalledWith('c1')
  })

  it('falls back to the fetch when the marker arrives with no projectId to resolve from', async () => {
    // Fail-safe direction: the skip only ever removes a request whose answer the query already
    // holds. No project in the query means nothing to render from, so ask the server.
    h.getConversation.mockResolvedValue(conversation({ kind: 'plan' }))
    renderRoute('/chat/c1', { freshlyMinted: true })

    expect(await screen.findByTestId('conversation-slot')).toBeTruthy()
    expect(h.getConversation).toHaveBeenCalledTimes(1)
  })
})

describe('ChatRoute — the project breadcrumb', () => {
  it('passes projectName down once getProject resolves', async () => {
    h.getConversation.mockResolvedValue(conversation())
    renderRoute('/chat/c1')
    await waitFor(() => expect(screen.getByTestId('conversation-slot').textContent).toContain('VIP Movement'))
    expect(h.getProject).toHaveBeenCalledWith('p1')
  })

  it('passes projectName: null and does NOT redirect when the project 404s', async () => {
    // A chat whose project vanished should still render its transcript.
    h.getConversation.mockResolvedValue(conversation())
    h.getProject.mockRejectedValue(new Error('gone'))
    renderRoute('/chat/c1')
    await waitFor(() => expect(screen.getByTestId('conversation-slot').textContent).toContain('|null'))
    expect(screen.queryByTestId('projects-index')).toBeNull()
  })

  it('falls back to the query projectId when the conversation carries none', async () => {
    h.getConversation.mockResolvedValue(conversation({ projectId: undefined }))
    renderRoute('/chat/c1?projectId=p9')
    await waitFor(() => expect(screen.getByTestId('conversation-slot').textContent).toContain('|p9|'))
  })
})

/**
 * What the row is told to name, and the merge that fills in its title. `WorkspaceToolbar.test.tsx`
 * covers the row's own rendering, but every scenario there publishes a SYNTHETIC heading — this is
 * the component that publishes the real one, so the loading branch, the stale-project guard and
 * the title merge could all regress with the suite green and the row naming the wrong project.
 */
describe('ChatRoute — what it publishes for the toolbar row', () => {
  it('names the project, the kind and the title once the conversation resolves', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'build', title: 'Add an out-time column' }))
    renderRoute('/chat/c1')

    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|build|Add an out-time column'))
  })

  it('★ names the URL\'s project while the conversation is still resolving, and no kind', async () => {
    // The whole `GET /conversations/{id}` window. The kind is the SERVER's to answer, so nothing is
    // claimed about it; the project is the URL's own and is right whenever it is there, which is
    // what gives the row a breadcrumb and a back target from the first frame.
    let land: ((value: unknown) => void) | undefined
    h.getConversation.mockImplementation(() => new Promise((res) => { land = res }))

    renderRoute('/chat/c1?projectId=p1&kind=build')

    await waitFor(() => expect(heading()).toBe('p1|null|null|null'))
    // LIVENESS: the same render goes on to publish the server's answer, so the state above is a
    // load window rather than a route that never resolved.
    land?.(conversation({ kind: 'build', title: 'Add an out-time column' }))
    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|build|Add an out-time column'))
  })

  it('★ names the project this tab already learned, on a reload that carries no query', async () => {
    // THE ORDINARY CASE the URL fallback above cannot reach: a chat's address is rewritten to
    // the bare `/chat/{id}` the instant its first message lands, so every reload, bookmark and
    // shared link arrives with nothing in the query.
    //
    // The memory is EARNED here rather than seeded: the first render is the visit that learns
    // the project, the second is the reload that reads it back — seeding storage directly would
    // pass even if nothing ever wrote to it.
    h.getConversation.mockResolvedValue(conversation({ kind: 'build', title: 'Add an out-time column' }))
    renderRoute('/chat/c1')
    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|build|Add an out-time column'))
    cleanup()

    let land: ((value: unknown) => void) | undefined
    h.getConversation.mockImplementation(() => new Promise((res) => { land = res }))

    renderRoute('/chat/c1') // the reload: no ?projectId=, no ?kind=

    await waitFor(() => expect(heading()).toBe('p1|null|null|null'))
    // LIVENESS: the same render goes on to publish the server's answer, so the state above is a
    // load window and not a route that never resolved.
    land?.(conversation({ kind: 'build', title: 'Add an out-time column' }))
    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|build|Add an out-time column'))
  })

  it('★ and claims nothing for a chat this browser has never seen', async () => {
    // The memory is a memory, never a guess. This tab HAS learned a project — from a different
    // chat — so the neutral shape here is a real answer rather than an empty store: it says the
    // memory is per chat, and a chat nobody has opened inherits nothing from the one beside it.
    // Mutation receipt: drop the chat id from the storage key and this goes red while the reload
    // scenario above stays green.
    h.getConversation.mockResolvedValue(conversation())
    renderRoute('/chat/c1')
    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|plan|T'))
    cleanup()

    let land: ((value: unknown) => void) | undefined
    h.getConversation.mockImplementation(() => new Promise((res) => { land = res }))

    renderRoute('/chat/never-seen')

    // Found by the words it SHOWS. `getByRole('status', { name: /loading chat/i })` would be
    // satisfied by an `aria-label` on a region with NO visible text — a wordless wait this
    // guards against. The label is gone (a live region announces its CONTENT, and a
    // label repeating that content is the sentence read twice), and `role="status"` takes no name
    // from content, so the wait is now asserted by the sentence a citizen can actually read.
    await waitFor(() => expect(screen.getByText('Loading this chat…')).toBeTruthy())
    expect(heading()).toBe('null|null|null|null')
    // LIVENESS: this chat does resolve, so the neutral shape above is a load window and not a
    // route that never answered.
    land?.(conversation({ id: 'never-seen' }))
    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|plan|T'))
  })

  it('★ withholds a project NAME that belongs to a different project', async () => {
    // This route stays mounted across chat navigations, so a `project` read for the previous chat
    // outlives the chat it was read for. Naming it would put another project's name on this row.
    h.getConversation.mockResolvedValue(conversation({ projectId: 'p-other' }))
    h.getProject.mockResolvedValue({ id: 'p1', name: 'VIP Movement' })

    renderRoute('/chat/c1')

    await screen.findByTestId('conversation-slot')
    await waitFor(() => expect(h.getProject).toHaveBeenCalledWith('p-other'))
    expect(heading()).toBe('p-other|null|plan|T')
  })

  it('★ takes the title back from the surface when the chat had none', async () => {
    // A freshly minted chat legitimately has no title until the surface derives one; without
    // the merge the row goes on naming the kind for the whole life of the chat.
    h.getConversation.mockResolvedValue(conversation({ title: '' }))
    renderRoute('/chat/c1')
    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|plan|null'))

    fireEvent.click(screen.getByText('derive a title'))

    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|plan|Add an out-time column'))
  })

  it('★ and never lets a derived title overwrite the stored one', async () => {
    // The stored title is the one a citizen may have seen before; a surface re-deriving one from
    // the first message must not rename the chat under them.
    h.getConversation.mockResolvedValue(conversation({ title: 'What the row already says' }))
    renderRoute('/chat/c1')
    await waitFor(() => expect(heading()).toBe('p1|VIP Movement|plan|What the row already says'))

    fireEvent.click(screen.getByText('derive a title'))

    // Awaited on the FINAL shape rather than sampled once, so this cannot pass by reading the
    // heading before a merge that was going to happen anyway.
    await waitFor(() => expect(screen.getByTestId('conversation-slot')).toBeTruthy())
    expect(heading()).toBe('p1|VIP Movement|plan|What the row already says')
  })
})

describe('ChatRoute — load failure', () => {
  it('falls back to the query rather than stranding the user on a spinner', async () => {
    h.getConversation.mockRejectedValue(new Error('boom'))
    renderRoute('/chat/c1?projectId=p1&kind=build')
    expect(await screen.findByTestId('conversation-slot')).toBeTruthy()
  })

  it('bails to /projects when the load fails and there is no query to fall back on', async () => {
    h.getConversation.mockRejectedValue(new Error('boom'))
    renderRoute('/chat/c1')
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
  })
})

describe('ChatRoute — a dead address says something on the way out', () => {
  /* THE BOUNCE IS UNCHANGED. Every case above still bails to /projects, and should. What these
     pin is the sentence it carries — and, more importantly, the two failures that must NOT
     carry one. The catch this route hangs on is reached by a 400 (a malformed id — the only
     "the chat is not there" status that actually throws; a 404 is null-ed one arm above), by a
     500, and by a DROPPED CONNECTION, and treating all three as "the chat is gone" is the class
     of over-claiming this codebase keeps refusing. */

  const arrivalSaid = () => screen.getByTestId('arrival-notice').textContent

  it('an absent row with no query says the neutral line', async () => {
    // `getConversation` answers a real 404 with `null` rather than by throwing, so this — not
    // the catch — is the ordinary dead-bookmark path.
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/ghost-206')

    await screen.findByTestId('projects-index')
    expect(arrivalSaid()).toBe(PROJECT_GONE_NOTICE)
  })

  it('★ a mangled chat link says the same line — the status the server really sends', async () => {
    /* THE STATUS HERE IS LOAD-BEARING, and this test used to fabricate one the endpoint cannot
       send. `GET /v1/conversations/{id}` matches the id against `_ID_RE` by hand and answers a
       malformed token with **400**; the path param is a plain `str`, so FastAPI never validates
       it and the 422 this case once asserted is unreachable. Verified against the running server:
       `/chat/abc%20def` → 400 `Invalid conversation id.`

       So the old assertion passed while the real citizen path — a chat link a mail client wrapped
       with a space or a `<` — bounced to the list in SILENCE, the exact failure this guards
       against. */
    h.getConversation.mockRejectedValue(new ApiError('Invalid conversation id.', 400))
    renderRoute('/chat/abc def')

    await screen.findByTestId('projects-index')
    expect(arrivalSaid()).toBe(PROJECT_GONE_NOTICE)
  })

  it('★ a dropped connection bounces in silence — it does NOT say the chat is gone', async () => {
    /* A `fetch` that never reached the server rejects with a plain `TypeError`: no status, not an
       `ApiError`. The bounce stays (a spinner with no answer is worse), but the platform knows
       nothing here and must not claim otherwise.

       MUTATION CHECK — the named mutant for this whole describe block: widen `goneNoticeFor` to
       return the sentence unconditionally and this goes red while every other case in this file
       stays green. */
    h.getConversation.mockRejectedValue(new TypeError('Failed to fetch'))
    renderRoute('/chat/c-206-offline')

    // LIVENESS FIRST — the bounce genuinely happened, so the empty string below is a silent
    // arrival rather than a tree that never rendered.
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
    expect(arrivalSaid()).toBe('')
  })

  it('★ a 500 is not a deletion either', async () => {
    // The server failed to LOOK. Same silence, for the same reason.
    h.getConversation.mockRejectedValue(new ApiError('Internal Server Error', 500))
    renderRoute('/chat/c-206-boom')

    expect(await screen.findByTestId('projects-index')).toBeTruthy()
    expect(arrivalSaid()).toBe('')
  })
})

describe('ChatRoute — the page is never torn down mid-turn', () => {
  // A brand-new chat rewrites `/chat/{id}?projectId=…` to `/chat/{id}` the instant its first
  // append lands. If that rewrite re-runs the resolve effect, ChatRoute falls back to its
  // spinner, the surface unmounts, and its unmount cleanup ABORTS the very stream the append
  // was for — killing the first turn of every new chat.
  it('dropping the transient query does not re-resolve the conversation or unmount the page', async () => {
    h.getConversation.mockResolvedValue(conversation())
    render(
      <MemoryRouter initialEntries={['/chat/c1?projectId=p1&kind=plan']}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatRoute />} />
        </Routes>
      </MemoryRouter>,
    )
    const page = await screen.findByTestId('conversation-slot')
    expect(h.getConversation).toHaveBeenCalledTimes(1)

    // The page rewrites its own URL, exactly as ChatPage/BuilderPage do after the first append.
    fireEvent.click(screen.getByText('drop query'))
    await waitFor(() => expect(screen.getByTestId('conversation-slot')).toBe(page)) // same node: no remount

    expect(h.getConversation).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('status', { name: /loading chat/i })).toBeNull()
  })

  it('keeps the current chat rendered while the next one resolves', async () => {
    // Navigating build chat A → build chat B must not flash the spinner: A's in-flight turn
    // lives in the page's state, and the pages are reconciled, not remounted.
    let resolveSecond: ((value: unknown) => void) | undefined
    h.getConversation
      .mockResolvedValueOnce(conversation({ id: 'c1' }))
      .mockImplementationOnce(() => new Promise((res) => { resolveSecond = res }))

    render(
      <MemoryRouter initialEntries={['/chat/c1']}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatRoute />} />
        </Routes>
      </MemoryRouter>,
    )
    const page = await screen.findByTestId('conversation-slot')
    expect(page.textContent).toContain('|c1|')

    fireEvent.click(screen.getByText('go to c2'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledTimes(2))

    // c2 has not resolved. The page is still mounted, still showing c1.
    expect(screen.queryByRole('status', { name: /loading chat/i })).toBeNull()
    expect(screen.getByTestId('conversation-slot').textContent).toContain('|c1|')

    resolveSecond?.({ id: 'c2', kind: 'plan', projectId: 'p1', messages: [] })
    await waitFor(() => expect(screen.getByTestId('conversation-slot').textContent).toContain('|c2|'))
  })
})

describe('ChatRoute — the chat-open mark', () => {
  it('marks a chat open for a project whose page this load opened', async () => {
    // The chat-open ratio's numerator, taken at THE resolution seam rather than on the three
    // handlers that navigate here — those live in components other work is mid-rewrite of.
    markProjectOpened('p-open', { hasApp: false })
    h.authFetch.mockClear()
    h.getConversation.mockResolvedValue(conversation({ projectId: 'p-open' }))

    renderRoute('/chat/c1')

    await screen.findByTestId('conversation-slot')
    await waitFor(() => expect(beacons()).toEqual([{ name: 'project_opened_chat' }]))
  })

  it('counts one visit, not two chats', async () => {
    markProjectOpened('p-two', { hasApp: false })
    h.authFetch.mockClear()
    h.getConversation.mockResolvedValue(conversation({ id: 'c1', projectId: 'p-two' }))
    renderRoute('/chat/c1')
    await screen.findByTestId('conversation-slot')
    await waitFor(() => expect(beacons()).toHaveLength(1))

    cleanup()
    h.getConversation.mockResolvedValue(conversation({ id: 'c2', projectId: 'p-two' }))
    renderRoute('/chat/c2')
    await screen.findByTestId('conversation-slot')

    expect(beacons()).toEqual([{ name: 'project_opened_chat' }])
  })

  it('★ marks nothing for a deep link into a project this load never opened', async () => {
    // A bookmark or browser restore resolves a project whose page was never on screen. Counting
    // it would push the visit-count ratio above 1 — a broken number, not a bias — and it must
    // not invent the denominator either: no `project_opened` appears here.
    h.getConversation.mockResolvedValue(conversation({ projectId: 'p-deep-link' }))

    renderRoute('/chat/c1')

    await screen.findByTestId('conversation-slot')
    expect(beacons()).toEqual([])
  })

  it('marks nothing for a chat that resolves with no project behind it', async () => {
    h.getConversation.mockResolvedValue(conversation({ projectId: null }))

    renderRoute('/chat/c1')

    await screen.findByTestId('conversation-slot')
    expect(beacons()).toEqual([])
  })
})

describe('ChatRoute — the cold-load wait says what it is doing', () => {
  it('★ shows a visible sentence and one busy polite region while the chat resolves', async () => {
    // The rule binding the whole batch: suppressing an animation never leaves a wait silent. The
    // three dots here are `animate-bounce`, which the reduce-motion block freezes — so without
    // words this arm is three static dots for a citizen who asked for reduced motion. Four such
    // waits are pinned elsewhere in the suite; this is the fifth, and nothing else covers it.
    let settle: (v: unknown) => void = () => {}
    h.getConversation.mockImplementation(() => new Promise((r) => { settle = r }))

    renderRoute('/chat/c1')

    const wait = await screen.findByTestId('chat-wait')
    // The sentence, visible — not an sr-only copy and not an aria-label.
    expect(wait.textContent).toContain('Loading this chat…')
    expect(wait.getAttribute('aria-busy')).toBe('true')
    expect(wait.getAttribute('aria-live')).toBe('polite')
    // No label: a live region announces its content, and a label repeating it reads it twice.
    expect(wait.getAttribute('aria-label')).toBeNull()
    // EXACTLY ONE region says it. A second copy is the sentence read twice.
    expect(screen.getAllByText('Loading this chat…')).toHaveLength(1)
    // The dots are decoration now that the words carry the meaning.
    expect(wait.querySelector('[aria-hidden="true"]')).toBeTruthy()

    settle({ id: 'c1', projectId: 'p1', kind: 'build', title: 't' })
  })
})

describe('ChatRoute — the wait that is not a wait', () => {
  // WHAT THIS IS ABOUT. A citizen who sends their first message from the rail gets THREE
  // announcements inside two seconds for ONE continuous wait: the pane says "Getting your app
  // ready." (the rail raises the workspace's start flag before its request and navigates after
  // it), this board opens and closes a second polite region on the way to the chat, and the
  // surface then publishes the same state again. This pair pins the middle one — the only one of
  // the three describing a load that is not happening, since a freshly minted chat resolves with
  // no request at all.
  it('★ paints the wait board but does not ANNOUNCE it on a freshly minted arrival', async () => {
    const frame: { seen: WaitFrame | null } = { seen: null }
    renderRoute('/chat/fresh-id?projectId=p1&kind=build', { freshlyMinted: true }, <FirstCommitSpy onto={frame} />)

    // LIVENESS FIRST: the chat really does resolve, so everything below is a claim about a frame
    // this route actually rendered and not about a route that never got anywhere.
    expect(await screen.findByTestId('conversation-slot')).toBeTruthy()
    expect(h.getConversation).not.toHaveBeenCalled()

    // AND THE BOARD REALLY WAS ON SCREEN for that frame. Without this, the three assertions after
    // it would pass just as happily against a route that rendered nothing at all — which is the
    // false green the spy exists to close.
    expect(frame.seen?.present).toBe(true)
    expect(frame.seen?.text).toContain('Loading this chat…')

    // …and it was silent. Nothing here interrupts a reader who is already being told about this
    // same wait by the pane in the next column.
    expect(frame.seen?.role).toBeNull()
    expect(frame.seen?.live).toBeNull()
    expect(frame.seen?.busy).toBeNull()
  })

  it('★ still announces it when the route really does have to ask the server', async () => {
    // THE CONTROL, and the direction that costs more when it is wrong. The marker alone is not
    // the condition: with no project in the query there is nothing to resolve from, the GET
    // happens after all, and a citizen is owed the sentence for however long it takes.
    const frame: { seen: WaitFrame | null } = { seen: null }
    let settle: (v: unknown) => void = () => {}
    h.getConversation.mockImplementation(() => new Promise((r) => { settle = r }))

    renderRoute('/chat/c1', { freshlyMinted: true }, <FirstCommitSpy onto={frame} />)

    const wait = await screen.findByTestId('chat-wait')
    expect(wait.getAttribute('role')).toBe('status')
    expect(wait.getAttribute('aria-live')).toBe('polite')
    expect(wait.getAttribute('aria-busy')).toBe('true')
    // Read off the same first frame the case above reads, so the two are measured the same way
    // and neither can be green because the spy itself stopped working.
    expect(frame.seen?.live).toBe('polite')

    // LIVENESS: this is a load window, not a route that never answered.
    settle(conversation())
    expect(await screen.findByTestId('conversation-slot')).toBeTruthy()
  })
})
