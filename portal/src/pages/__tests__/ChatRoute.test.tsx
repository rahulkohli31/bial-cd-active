/**
 * ChatRoute — the flat `/chat/:chatId` kind dispatcher.
 *
 * The SLOT is stubbed so this file tests exactly one thing: what the route resolves — which
 * conversation, of which kind, in which project — and when it bails to /projects instead. It used
 * to stub two pages and assert which one mounted; there is one surface now (Plan D U17), so the
 * resolution IS the contract.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom'

const h = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getProject: vi.fn(),
  authFetch: vi.fn(),
}))

// The REAL `observe` module runs here — its deep-link guard IS what these tests are about, and a
// mocked module would prove only that a function was called. Only the transport is replaced.
// Each test uses its OWN project id: module state is per page load, so a shared id would let one
// test's mark silence the next one's.
vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  authFetch: h.authFetch,
}))

vi.mock('../../utils/conversationApi.js', () => ({ getConversation: h.getConversation }))
vi.mock('../../utils/projectApi', () => ({ getProject: h.getProject }))

/**
 * ONE STUB, AND IT IS THE SLOT (Plan D U17).
 *
 * This file used to stub two PAGES, because the route chose between them and "which one mounts"
 * was the contract. There is one surface now, so the route's contract is the VALUE it hands the
 * slot — a resolved conversation, kind included — rather than a component it selects. Stubbing
 * the slot is what lets that value be asserted directly; stubbing the surface underneath it would
 * leave the kind invisible, since the slot deliberately does not pass it down.
 *
 * Named, not an anonymous arrow: this stub calls `useNavigate`, and a hook is only legal inside
 * something lint can SEE is a component. As `default: () => …` the rule reads the function's name
 * as "default" — lowercase, so "not a component" — and errors.
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
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  useWorkspaceHeading,
} from '../../components/workspace/workspaceChannel'
import { beaconsFrom } from './_observeBeacons'
import { markProjectOpened } from '../../utils/observe'

/**
 * WHAT THE TOOLBAR ROW WOULD NAME, read straight off the channel (plan 002, U2).
 *
 * The row itself is the shell's and has its own suite; what is only observable HERE is the value
 * this route PUBLISHES, which every one of those scenarios takes as a given. Rendered as one
 * string so a single assertion covers all four fields and a wrong one cannot hide behind a right
 * one.
 */
function HeadingProbe() {
  const { projectId, projectName, chatKind, chatTitle } = useWorkspaceHeading()
  return <span data-testid="heading">{`${projectId}|${projectName}|${chatKind}|${chatTitle}`}</span>
}


/**
 * `state` is the freshly-minted marker's carrier. Entries without one stay plain strings so the
 * existing cases exercise the exact same router input they always did.
 */
function renderRoute(entry: string, state?: unknown) {
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
          <Route path="/projects" element={<div data-testid="projects-index">projects</div>} />
        </Routes>
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
  // THE NAME CHANGED BECAUSE THE JOB DID (Plan D U17). These two used to assert which PAGE
  // mounted; there is one surface now, so what is left to be right about is the kind the route
  // RESOLVES and hands on. That is the whole of the route's remaining contract, and it is still
  // worth pinning — the value decides which toolset the server gives the model.
  it('resolves a plan conversation as plan', async () => {
    h.getConversation.mockResolvedValue(conversation({ kind: 'plan' }))
    renderRoute('/chat/c1')
    expect((await screen.findByTestId('conversation-slot')).getAttribute('data-kind')).toBe('plan')
  })

  // Was "renders BuilderPage for a builder conversation": `builder` was the OLD three-valued
  // ConversationKind's word. It collapsed into the two-valued ChatKind's `build` (U1).
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
})

describe('ChatRoute — a conversation whose row does not exist yet', () => {
  it('takes the kind from ?kind= when the id 404s but a ?projectId= is present', async () => {
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new-uuid?projectId=p1&kind=build')
    // The row appears on the first appendMessage; until then only the query knows the project.
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
    // A bare word like "new" landing at /chat/new is not a real conversation and carries
    // no project query, so it resolves to "gone" and bounces to the projects index.
    h.getConversation.mockResolvedValue(null)
    renderRoute('/chat/new')
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
  })
})

describe('ChatRoute — the GET that cannot succeed', () => {
  // A chat this session just minted has no row until the send path creates it (U7), so its
  // `getConversation` is a guaranteed 404 on every cold new-chat open — and it is not the only
  // one: both pages keep their own hydration fetch, and StrictMode doubles the pair again in dev.
  it('a freshly-minted open issues NO getConversation and renders from the query', async () => {
    renderRoute('/chat/fresh-id?projectId=p1&kind=build', { freshlyMinted: true })

    expect(await screen.findByTestId('conversation-slot')).toBeTruthy()
    expect(screen.getByTestId('conversation-slot').textContent).toContain('|fresh-id|p1|')
    expect(h.getConversation).not.toHaveBeenCalled()
  })

  // THE IMPORTANT ONE. Keying the skip on "the URL has query params" would be a security
  // regression, not just a wrong optimisation: `?kind=` is user-controllable, and a saved chat's
  // URL only loses its query after the FIRST append — so a shared or bookmarked
  // `/chat/{id}?kind=build` for an already-saved plan chat is an ordinary URL that MUST
  // still be resolved by the server. Router state does not survive a reload and does not travel
  // in a link, which is exactly what makes the marker safe to trust.
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
 * WHAT THE ROW IS TOLD TO NAME (plan 002, U2), and the merge that fills in its title.
 *
 * The row's own rendering is covered in `WorkspaceToolbar.test.tsx`, but every scenario there
 * publishes a SYNTHETIC heading. This is the component that publishes the real one in production,
 * and nothing was asserting it — so the loading branch, the stale-project guard and the title merge
 * could all regress with the whole suite green and the row quietly naming the wrong project.
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
    // THE ORDINARY CASE, and the one the URL fallback above cannot reach. A chat's address is
    // rewritten to the bare `/chat/{id}` the instant its first message lands, so every reload,
    // bookmark and shared link into an existing chat arrives with nothing in the query — and for
    // the whole of the next `GET` the row had no project to name and a back control aimed at the
    // projects list, out of the project the citizen was working in.
    //
    // The memory is EARNED here rather than seeded: the first render is the visit that learns the
    // project, and the second is the reload that reads it back. Seeding storage directly would
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

    await waitFor(() => expect(screen.getByRole('status', { name: /loading chat/i })).toBeTruthy())
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
    // A chat's row is created by its first send and its title is derived from that message, so a
    // freshly minted chat legitimately has none until the surface works one out. Without the merge
    // the row goes on naming the kind for the whole life of the chat.
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

describe('ChatRoute — the chat-open mark (U4; R105)', () => {
  it('marks a chat open for a project whose page this load opened', async () => {
    // R105's numerator, taken at THE resolution seam rather than on the three handlers that
    // navigate here — those live in components other work is mid-rewrite of.
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
    // A bookmark, a shared link or a browser restore resolves a project whose page was never on
    // screen. Counting it would push R105's ratio above 1 — a denominator smaller than its
    // numerator is not a bias, it is a broken number. And it must not invent the denominator
    // either: no `project_opened` appears here.
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
