/**
 * BIAL CHAT — the screen, mounted whole.
 *
 * THE COMPOSER AND THE RUNTIME UNDER TEST ARE THE REAL ONES. Nothing about the library, the
 * runtime or the box is doubled here, because the claim this page makes is precisely that it is a
 * second consumer of the builder's own chat stack rather than a parallel one. A test against a
 * stub would pass on a drawing. What IS doubled is the network: the calls this surface makes, so
 * that a create, an upload, a turn, a load and their failures can each be driven.
 *
 * ONE COMPONENT SERVES BOTH ADDRESSES, so `mount()` takes the address rather than a page.
 *
 * THE GREETING IS RANDOM, AND THE ASSERTIONS ARE WRITTEN FOR THAT rather than around it: most
 * check membership of the set the page is allowed to draw from, and the two that need one exact
 * line pin the clock and the source of chance instead of hoping.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { MotionGlobalConfig } from 'motion/react'

const h = vi.hoisted(() => ({
  getStoredUser: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  startTurn: vi.fn(),
  readTurnStream: vi.fn(),
  stopTurn: vi.fn(),
  buildUserParts: vi.fn(),
  releaseUploadedAttachments: vi.fn(),
}))
vi.mock('../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getStoredUser: h.getStoredUser,
}))
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  createConversation: h.createConversation,
  getConversation: h.getConversation,
}))
vi.mock('../../utils/turnStreamApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  startTurn: h.startTurn,
  readTurnStream: h.readTurnStream,
  stopTurn: h.stopTurn,
}))
// The upload half is doubled for the same reason the network is: a refused turn has to be shown
// releasing what it uploaded, and a real upload cannot be made to have happened here.
vi.mock('../../utils/attachmentStore', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  buildUserParts: h.buildUserParts,
  releaseUploadedAttachments: h.releaseUploadedAttachments,
}))

import AssistantPage from '../AssistantPage'
import { GREETINGS, headlineParts } from '../../components/assistant/greetings'
import { TurnStartError } from '../../utils/turnStreamApi'

MotionGlobalConfig.skipAnimations = true

const CITIZEN = { email: 'asha@bial.aero', display_name: 'Asha Rao', isAdmin: false }

/**
 * The navigation entry, exactly as the sidebar draws it: a bare navigation to `/assistant` and
 * nothing else. It is mounted OUTSIDE the routes because that is where it lives — a link that
 * cannot reach the page's own reset is the whole of what makes leaving a conversation hard.
 */
function SidebarEntry() {
  const navigate = useNavigate()
  return (
    <button type="button" data-testid="nav-bial-chat" onClick={() => navigate('/assistant')}>
      BIAL Chat
    </button>
  )
}

/** Both addresses, through the real router, because the page reads its own.
 *
 *  The builder address is mounted as a PROBE rather than as the real surface: what this file
 *  asserts about it is that a chat belonging to a project is sent there, not what it draws when
 *  it arrives. */
function mount(at = '/assistant') {
  return render(
    <MemoryRouter initialEntries={[at]}>
      <SidebarEntry />
      <Routes>
        <Route path="/assistant" element={<AssistantPage />} />
        <Route path="/assistant/:chatId" element={<AssistantPage />} />
        <Route path="/chat/:chatId" element={<div data-testid="builder-address" />} />
      </Routes>
    </MemoryRouter>,
  )
}

const heading = () => screen.getByTestId('assistant-greeting')
const box = () => screen.getByTestId('composer-input') as HTMLTextAreaElement
const send = () => screen.getByTestId('composer-send')
const sky = () => screen.getByTestId('assistant-backdrop')

const aConversation = (over: Record<string, unknown> = {}) => ({
  id: 'c1',
  kind: 'generic',
  projectId: null,
  title: '',
  createdAt: '',
  updatedAt: '',
  messages: [],
  activeTurn: null,
  contextTokens: null,
  ...over,
})

/** Type into the real box and press Enter — the door every composer suite drives. */
function type(text: string) {
  fireEvent.change(box(), { target: { value: text } })
  fireEvent.keyDown(box(), { key: 'Enter' })
}

/** Every headline the page may draw, already rendered for this name — which is what "it came from
 *  the set, and the placeholder was filled" means as one assertion. */
const rendered = (name: string | null) =>
  GREETINGS.filter((g) => name !== null || !g.headline.includes('{name}')).map((g) =>
    headlineParts(g.headline, name)
      .map((segment) => segment.text)
      .join(''),
  )

beforeEach(() => {
  vi.clearAllMocks()
  h.getStoredUser.mockReturnValue(CITIZEN)
  h.createConversation.mockResolvedValue(aConversation())
  h.getConversation.mockResolvedValue(aConversation())
  h.startTurn.mockResolvedValue({ turnId: 't1', contextTokens: null })
  h.readTurnStream.mockResolvedValue('completed')
  h.stopTurn.mockResolvedValue('stopping')
  // The shape the real one returns for a message with nothing attached: the prose, last.
  h.buildUserParts.mockImplementation(async (text: string) => [{ type: 'text', text }])
  sessionStorage.clear()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('the heading is a greeting, and it is the only colour on the screen', () => {
  it('draws one of the thirty, with the placeholder filled and the markers gone', () => {
    mount()
    const text = heading().textContent ?? ''
    expect(rendered('Asha')).toContain(text)
    expect(text).not.toContain('{name}')
    expect(text).not.toContain('*')
  })

  it('paints exactly one run, and it is a real element rather than markup', () => {
    mount()
    const accents = heading().querySelectorAll('em')
    expect(accents).toHaveLength(1)
    expect(accents[0].className).toContain('text-primary')
    expect((accents[0].textContent ?? '').length).toBeGreaterThan(0)
  })

  it('uses the first word of the display name, at the hour the citizen is actually at', () => {
    // The one place the clock and the source of chance are both pinned, so the exact line is
    // known: 09:00 is the morning band, and roll 0 is its first candidate.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 8, 21, 9, 30, 0))
    vi.spyOn(Math, 'random').mockReturnValue(0)

    mount()
    expect(heading().textContent).toBe('Morning, Asha.')
    expect(heading().querySelector('em')?.textContent).toBe('Morning')
  })

  it.each([
    ['a profile with no display name', { ...CITIZEN, display_name: null }],
    ['no cached profile at all', null],
  ])('★ %s is never given a line that needs one', (_case, user) => {
    // The failure this guards is not a crash — it is the heading reading "Morning, ." or
    // "Morning, {name}.", which looks like the product does not know who signed in.
    h.getStoredUser.mockReturnValue(user)
    mount()
    const text = heading().textContent ?? ''
    expect(rendered(null)).toContain(text)
    expect(text).not.toMatch(/,\s*\./)
  })

  it('★ does not repeat itself on the next visit in the same sitting', () => {
    // SIX MOUNTS, NOT TWO. With nine candidates a single pair agrees by chance eight times in
    // nine even with the exclusion deleted, so a two-mount version of this test passes on the
    // broken code almost always — it was checking nothing.
    let previous: string | null = null
    for (let visit = 0; visit < 6; visit += 1) {
      mount()
      const current = heading().textContent ?? ''
      expect(current, `visit ${visit} repeated the previous line`).not.toBe(previous)
      // Liveness: each mount drew a real heading, which "not the same" alone would not prove.
      expect(rendered('Asha')).toContain(current)
      previous = current
      cleanup()
    }
  })
})

describe('the composer is the library own, and it sends', () => {
  it('mounts the real box, its dropzone and its attachment control', () => {
    mount()
    expect(screen.getByTestId('composer-dropzone')).toBeTruthy()
    expect(screen.getByTestId('composer-attach')).toBeTruthy()
    expect(box().tagName).toBe('TEXTAREA')
  })

  it('★ sends — the preview gate is gone, and nothing stands in its place', async () => {
    // The inverse of the assertion this replaces. That one pinned a hardcoded refusal on every
    // send; pinning its absence in both directions is what stops the behaviour being merely
    // unpinned.
    mount()
    expect(screen.queryByTestId('composer-gate-note')).toBeNull()
    expect(send().getAttribute('aria-label')).not.toMatch(/sending switches on/i)

    type('how many stands are free tonight')

    await waitFor(() => expect(h.createConversation).toHaveBeenCalledTimes(1))
    expect(h.createConversation.mock.calls[0][0]).toEqual({
      id: expect.any(String),
      kind: 'generic',
    })
  })

  it('★ the notes under the box are painted in the grey that passes contrast on this ground', async () => {
    // `text-neutral` is 4.37:1 on #F0F4F8 and fails AA; it passes everywhere else because the
    // composer almost always sits on white. This screen is the first to put it on the platform
    // ground, and the swap is the whole reason `noteClassName` exists. The gate note is gone, so
    // the assertion moves to the meter — the note this surface adds, and the one the quota
    // decision committed to showing here.
    h.getConversation.mockResolvedValue(aConversation({ contextTokens: 480_000 }))
    mount('/assistant/c1')

    const line = await screen.findByTestId('composer-context-warning')
    expect(line.className).toContain('text-status-grey-fg')
    expect(line.className).not.toContain('text-neutral')
  })

  it('★ keeps its urgent region mounted and empty, rather than inserting it with its text', () => {
    // A live region added to the DOM together with its first message is missed outright by several
    // reader-and-browser combinations — three places in this project already record it. The region
    // has to be sitting in the accessibility tree before there is anything to announce, so it is
    // present and blank on a screen where nothing has gone wrong.
    mount()
    const region = screen.getByTestId('assistant-urgent')
    expect(region.getAttribute('role')).toBe('alert')
    expect(region.textContent).toBe('')
    // #EF4444 is 3.40:1 on this page's ground and fails AA; #B91C1C is 5.85:1.
    expect(region.className).toContain('text-status-red-fg')
    expect(region.className).not.toContain('text-danger')
  })

  it('★ never really disables anything — a real `disabled` blurs the focused element', () => {
    // The defect the whole composer is built around avoiding: `disabled` moves focus out of the
    // box mid-sentence. Every unavailable state here is `aria-disabled`.
    const { container } = mount()
    fireEvent.change(box(), { target: { value: 'how many stands are free tonight' } })
    expect(container.querySelector('[disabled]')).toBeNull()
  })
})

describe('the sky behind it is decoration and nothing else', () => {
  it('★ never traps its own content — the page grows rather than clipping it', () => {
    // jsdom lays nothing out, so this is asserted on the class list. A fixed height plus an
    // overflow clip here centred the stack in a box it could outgrow: measured at 1024x300 with a
    // full composer, the greeting was cut off above and the gate note below, with no scrollbar to
    // reach either. The shell above this element is what scrolls, and it can only do that if this
    // one is free to grow.
    mount()
    const page = screen.getByTestId('assistant-page')
    expect(page.className).toContain('min-h-full')
    expect(page.className).not.toMatch(/(^|\s)h-full(\s|$)/)
    expect(page.className).not.toContain('overflow-hidden')
  })

  it('★ the greeting column can grow, which is the whole of why it sits in the middle', () => {
    // jsdom lays nothing out, so this pins the MECHANISM and `e2e/assistant-geometry.spec.ts`
    // measures the pixels.
    //
    // A percentage minimum resolves against the parent's HEIGHT, and this page's height is `auto`
    // — so a column asking for `min-h-full` gets nothing, shrinks to its own content, and centres
    // inside a box exactly the size of what it holds. That is not a subtle miss: measured at
    // 1440x900 the column came out 299px tall in a 900px pane, putting the greeting 48px from the
    // top of an otherwise empty screen. What actually holds it open is `flex-1` against a parent
    // that is a flex column, so both halves are pinned here and neither is load-bearing alone.
    mount()
    const page = screen.getByTestId('assistant-page')
    expect(page.className).toMatch(/(^|\s)flex(\s|$)/)
    expect(page.className).toContain('flex-col')

    const column = screen.getByTestId('assistant-column')
    expect(column.className).toContain('flex-1')
    expect(column.className).toContain('justify-center')
    // The dead class, kept out on purpose: it resolves to zero here and reads as if it were
    // holding the box open, which is how the collapse hid in plain sight.
    expect(column.className).not.toContain('min-h-full')
    // Liveness: the column is the one that actually holds the greeting, so a page that rendered
    // an empty shell cannot satisfy the three assertions above.
    expect(column.contains(screen.getByTestId('assistant-greeting'))).toBe(true)
  })

  it('says nothing to a screen reader and catches no pointer', () => {
    mount()
    expect(sky().getAttribute('aria-hidden')).toBe('true')
    expect(sky().className).toContain('pointer-events-none')
  })

  it('draws the whole field: the corner, the motes and two aircraft', () => {
    mount()
    expect(sky().querySelector('.chat-sky-corner')).toBeTruthy()
    expect(sky().querySelectorAll('.chat-mote')).toHaveLength(51)
    expect(sky().querySelectorAll('.chat-plane')).toHaveLength(2)
  })

  it('is the same field on every mount, not a reshuffle', () => {
    const positions = () =>
      [...sky().querySelectorAll('.chat-mote')].map((node) => (node as HTMLElement).style.left)

    mount()
    const first = positions()
    cleanup()
    mount()

    expect(positions()).toEqual(first)
    // Liveness: a generator that returned one value for everything would also be "identical".
    expect(new Set(first).size).toBeGreaterThan(40)
  })

  it('★ never reaches for Math.random — which is the only mutation the test above cannot see', () => {
    // The field is built ONCE at module scope, so swapping the seeded generator for `Math.random`
    // still yields one field per bundle and every mount-to-mount comparison keeps passing. The
    // defect it would let through is real — a reshuffle on any re-render once that call moved into
    // the component — so the guard has to watch the import itself.
    const spy = vi.spyOn(Math, 'random')
    vi.resetModules()
    return import('../../components/assistant/Backdrop').then((mod) => {
      expect(typeof mod.default, 'the module really did re-execute').toBe('function')
      expect(spy).not.toHaveBeenCalled()
    })
  })
})

describe('the first message takes an address, and nothing is lost taking it', () => {
  it('creates the conversation with no project key at all, then starts the turn', async () => {
    mount()
    type('what does this document say')

    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    // THE KEY IS OMITTED, not sent as an absence — the server refuses a project on this kind by
    // the key being present, and an omission is what says the chat has no parent.
    expect(Object.keys(h.createConversation.mock.calls[0][0]).sort()).toEqual(['id', 'kind'])
    expect(h.createConversation.mock.calls[0][0].kind).toBe('generic')
    // The create fires BEFORE the turn: an upload names a conversation that must already exist.
    expect(h.createConversation.mock.invocationCallOrder[0]).toBeLessThan(
      h.startTurn.mock.invocationCallOrder[0],
    )
  })

  it('★ replaces the greeting rather than carrying it into the transcript', async () => {
    mount()
    expect(heading()).toBeTruthy()
    type('hello')

    await waitFor(() => expect(screen.queryByTestId('assistant-greeting')).toBeNull())
    // Liveness: the transcript took its place rather than the page rendering nothing.
    expect(screen.getByTestId('assistant-transcript')).toBeTruthy()
  })

  it('★ the backdrop is the same element across the first send — it never remounts', async () => {
    mount()
    const before = sky()
    type('hello')

    await waitFor(() => expect(screen.queryByTestId('assistant-greeting')).toBeNull())
    // NODE IDENTITY, not presence. A sibling route component for the addressed state would
    // unmount this one and restart the animation; a second element that merely exists would
    // satisfy a presence check while doing exactly that.
    expect(sky()).toBe(before)
  })

  it('★ a create that fails keeps the typed text, the greeting and the address', async () => {
    h.createConversation.mockRejectedValue(new Error('The chat could not be started. Try again.'))
    mount()
    type('how many stands are free tonight')

    await waitFor(() =>
      expect(screen.getByTestId('assistant-urgent').textContent).toMatch(/could not be started/i),
    )
    expect(box().value).toBe('how many stands are free tonight')
    expect(h.startTurn).not.toHaveBeenCalled()
    // Paired with a liveness assertion, so a crashed render cannot read as a pass.
    expect(heading()).toBeTruthy()
  })

  it('★ retrying after a failed create reuses the minted id and creates no second conversation', async () => {
    h.createConversation.mockRejectedValueOnce(new Error('nope'))
    mount()
    type('first try')
    await waitFor(() => expect(h.createConversation).toHaveBeenCalledTimes(1))

    fireEvent.keyDown(box(), { key: 'Enter' })
    await waitFor(() => expect(h.createConversation).toHaveBeenCalledTimes(2))

    // ONE ID ACROSS BOTH PRESSES. Creation is idempotent only per client-minted id, so a fresh
    // id per press would leave an unreachable second conversation behind — there is no list to
    // find it in.
    const [first, second] = h.createConversation.mock.calls
    expect(second[0].id).toBe(first[0].id)
  })

  it('★ two presses in quick succession issue at most one create and one turn', async () => {
    mount()
    fireEvent.change(box(), { target: { value: 'double pressed' } })
    fireEvent.keyDown(box(), { key: 'Enter' })
    fireEvent.keyDown(box(), { key: 'Enter' })

    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    expect(h.createConversation).toHaveBeenCalledTimes(1)
  })
})

describe('coming back to an address', () => {
  it('loads the full transcript', async () => {
    h.getConversation.mockResolvedValue(
      aConversation({
        messages: [{ id: 'm1', role: 'user', parts: [{ type: 'text', text: 'earlier question' }], seq: 0 }],
      }),
    )
    mount('/assistant/c1')

    expect(await screen.findByText('earlier question')).toBeTruthy()
    expect(screen.queryByTestId('assistant-greeting')).toBeNull()
  })

  it('★ shows a placeholder while loading — never the greeting', async () => {
    let release: (value: unknown) => void = () => {}
    h.getConversation.mockReturnValue(new Promise((resolve) => (release = resolve)))
    mount('/assistant/c1')

    expect(screen.getByTestId('assistant-loading')).toBeTruthy()
    expect(screen.queryByTestId('assistant-greeting')).toBeNull()
    release(aConversation())
    await waitFor(() => expect(screen.queryByTestId('assistant-loading')).toBeNull())
  })

  it('★ a load that fails for a network reason keeps the address, offers a retry, and never shows the greeting', async () => {
    h.getConversation.mockRejectedValue(new TypeError('Failed to fetch'))
    mount('/assistant/c1')

    const retry = await screen.findByTestId('assistant-load-retry')
    expect(screen.queryByTestId('assistant-greeting')).toBeNull()
    expect(screen.queryByTestId('assistant-gone')).toBeNull()
    // "Has resolved" is its own bit and is not "which address is current": a failed load must
    // never be reported as a destroyed conversation.
    expect(screen.getByTestId('assistant-load-failed').textContent).not.toMatch(/no longer here/i)

    h.getConversation.mockResolvedValue(aConversation())
    fireEvent.click(retry)
    await waitFor(() => expect(screen.queryByTestId('assistant-load-failed')).toBeNull())
  })

  it('★ a confirmed absence says so, and offers the one way forward', async () => {
    h.getConversation.mockResolvedValue(null)
    mount('/assistant/c1')

    expect((await screen.findByTestId('assistant-gone')).textContent).toMatch(/no longer here/i)
    fireEvent.click(screen.getByTestId('assistant-start-new'))
    // The same destination the navigation entry reaches — nothing new is invented to begin with.
    await waitFor(() => expect(screen.getByTestId('assistant-greeting')).toBeTruthy())
  })

  it('★ sending is refused with a stated reason while loading and while a failure is on screen', async () => {
    h.getConversation.mockRejectedValue(new TypeError('Failed to fetch'))
    mount('/assistant/c1')
    await screen.findByTestId('assistant-load-retry')

    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/before sending/i)
    type('into a transcript I cannot see')
    expect(h.startTurn).not.toHaveBeenCalled()

    h.getConversation.mockResolvedValue(aConversation())
    fireEvent.click(screen.getByTestId('assistant-load-retry'))
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
  })

  it('★ the live region announces a turn starting and a turn failing', async () => {
    const announcer = () => screen.getByTestId('activity-announcer')
    mount()
    type('hello')
    await waitFor(() => expect(announcer().textContent).toMatch(/reply started/i))

    h.readTurnStream.mockRejectedValue(new Error('the stream died'))
    fireEvent.change(box(), { target: { value: 'again' } })
    fireEvent.keyDown(box(), { key: 'Enter' })
    await waitFor(() => expect(announcer().textContent).toMatch(/failed/i))
  })

  it('★ a turn that fails mid-stream shows a retry, and taking it re-runs the turn', async () => {
    h.readTurnStream.mockRejectedValueOnce(new Error('the stream died'))
    mount()
    type('hello')

    const retry = await screen.findByTestId('assistant-turn-retry')
    expect(h.startTurn).toHaveBeenCalledTimes(1)
    fireEvent.click(retry)
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(2))
    // The same conversation, not a second one.
    expect(h.createConversation).toHaveBeenCalledTimes(1)
  })

  it('renders no suggestion UI', async () => {
    mount()
    expect(screen.queryByTestId('composer-suggestions')).toBeNull()
    expect(screen.queryByRole('button', { name: /suggest/i })).toBeNull()
  })
})

describe('the first attachment, and the turn that can be stopped', () => {
  it('★ creates the conversation before the upload is attempted', async () => {
    const order: string[] = []
    h.createConversation.mockImplementation(async () => {
      order.push('create')
      return aConversation()
    })
    const file = new File(['%PDF-1.4'], 'roster.pdf', { type: 'application/pdf' })
    mount()

    fireEvent.drop(screen.getByTestId('composer-dropzone'), {
      dataTransfer: { types: ['Files'], files: [file] },
    })
    await waitFor(() =>
      expect(screen.getByTestId('composer-chips').textContent).toContain('roster.pdf'),
    )
    fireEvent.change(box(), { target: { value: 'what is in this' } })
    fireEvent.keyDown(box(), { key: 'Enter' })

    await waitFor(() => expect(order).toContain('create'))
    // An upload names the conversation it belongs to, and that conversation has to be written
    // already — so the create fires before the upload rather than beside it.
    expect(h.createConversation).toHaveBeenCalledTimes(1)
  })

  it('★ stopping a running turn leaves the partial reply in the transcript', async () => {
    let releaseStream: (value: unknown) => void = () => {}
    h.readTurnStream.mockImplementation(async ({ onFrame }: { onFrame: (f: unknown) => void }) => {
      onFrame({ type: 'text_delta', seq: 1, text: 'Half an answer', newBlock: true })
      await new Promise((resolve) => (releaseStream = resolve))
      return 'aborted'
    })
    mount()
    type('tell me about stand 42')

    expect(await screen.findByText('Half an answer')).toBeTruthy()
    const stop = await screen.findByTestId('stop-turn')
    fireEvent.click(stop)

    await waitFor(() => expect(h.stopTurn).toHaveBeenCalled())
    releaseStream('aborted')
    // THE PARTIAL REPLY STAYS. Removing what the model had already written would make a stop
    // look like a turn that never happened, and lose words the citizen had already read.
    await waitFor(() => expect(screen.getByText('Half an answer')).toBeTruthy())
  })

  it('★ the navigation entry returns to the greeting with an empty composer and no carried draft', async () => {
    h.getConversation.mockResolvedValue(
      aConversation({
        messages: [{ id: 'm1', role: 'user', parts: [{ type: 'text', text: 'earlier' }], seq: 0 }],
      }),
    )
    h.getConversation.mockResolvedValueOnce(null)
    mount('/assistant/c1')

    fireEvent.click(await screen.findByTestId('assistant-start-new'))
    await waitFor(() => expect(screen.getByTestId('assistant-greeting')).toBeTruthy())
    expect(box().value).toBe('')
    expect(screen.queryByText('earlier')).toBeNull()
  })
})

describe('a chat that belongs to a project does not belong at this address', () => {
  it.each([['plan'], ['build']])(
    '★ a %s conversation opened here is sent to the builder address',
    async (kind) => {
      // Mutation receipt: drop the kind check from `applyLoaded` and this goes red — the chat
      // renders here instead, on a surface with no app pane and no breadcrumb.
      h.getConversation.mockResolvedValue(aConversation({ kind, projectId: 'p1' }))
      mount('/assistant/c1')

      expect(await screen.findByTestId('builder-address')).toBeTruthy()
      expect(screen.queryByTestId('assistant-transcript')).toBeNull()
      expect(screen.queryByTestId('assistant-greeting')).toBeNull()
    },
  )

  it('★ a generic conversation stays, which is what makes the redirect a branch', async () => {
    h.getConversation.mockResolvedValue(aConversation({ kind: 'generic' }))
    mount('/assistant/c1')

    expect(await screen.findByTestId('assistant-transcript')).toBeTruthy()
    expect(screen.queryByTestId('builder-address')).toBeNull()
  })
})

describe('a turn the server refuses leaves nothing behind that says it was sent', () => {
  const refused = () =>
    new TurnStartError(429, 'You have used your daily allowance.', 'quota_exceeded', null)

  it('★ takes the bubble back, because the database is holding nothing', async () => {
    h.startTurn.mockRejectedValue(refused())
    mount()
    type('how many stands are free tonight')

    await waitFor(() =>
      expect(screen.getByTestId('turn-banner').textContent).toMatch(/daily allowance/i),
    )
    // It looked sent and would have vanished at the next reload — a transcript disagreeing with
    // the database about a message nobody ever received. Asserted on the BUBBLE rather than on
    // the words: the composer is still holding them, which is the next assertion.
    //
    // AWAITED, even though the banner above has already settled. The rollback and the banner are
    // two separate state updates, and whether React lands them in ONE commit or two is a
    // scheduling detail that differs by runtime — so a synchronous assertion here passes on the
    // runtime that batches them and fails on the one that does not. The invariant is that the
    // bubble goes, not that it goes in the same paint as the banner. Safe as an absence check
    // because the banner above and the composer below are this test's liveness: a screen that
    // failed to render at all cannot satisfy either.
    await waitFor(() => expect(screen.queryAllByTestId('user-message')).toHaveLength(0))
    // AND THE WORDS ARE NOT LOST WITH IT. The bubble that was holding them is gone, so the
    // composer has to still be holding them instead.
    expect(box().value).toBe('how many stands are free tonight')
  })

  it('★ an accepted turn keeps its bubble, which is what makes the rollback a branch', async () => {
    mount()
    type('how many stands are free tonight')

    expect(await screen.findByText('how many stands are free tonight')).toBeTruthy()
    expect(screen.queryByTestId('turn-banner')).toBeNull()
  })

  it('★ releases the files it uploaded, so three refusals do not leave three orphans', async () => {
    const parts = [
      {
        type: 'file',
        attachmentId: 'att-1',
        key: 'k1',
        kind: 'document',
        name: 'roster.pdf',
        mediaType: 'application/pdf',
        size: 8,
      },
      { type: 'text', text: 'what is in this' },
    ]
    h.buildUserParts.mockResolvedValue(parts)
    h.startTurn.mockRejectedValue(refused())
    mount()
    type('what is in this')

    await waitFor(() => expect(h.releaseUploadedAttachments).toHaveBeenCalledTimes(1))
    // The very parts the refused turn named: nothing will ever reference them, and unreleased
    // they still count against this conversation's attachment allowance.
    expect(h.releaseUploadedAttachments.mock.calls[0][0]).toBe(parts)
  })

  it('★ releases nothing when the server takes the turn', async () => {
    h.buildUserParts.mockResolvedValue([
      {
        type: 'file',
        attachmentId: 'att-1',
        key: 'k1',
        kind: 'document',
        name: 'roster.pdf',
        mediaType: 'application/pdf',
        size: 8,
      },
      { type: 'text', text: 'what is in this' },
    ])
    mount()
    type('what is in this')

    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    expect(h.releaseUploadedAttachments).not.toHaveBeenCalled()
  })

  it('★ offers no retry for a message the server never took', async () => {
    // The composer is still holding the words, so a second way to send them would send them twice.
    h.startTurn.mockRejectedValue(refused())
    mount()
    type('how many stands are free tonight')

    await waitFor(() => expect(screen.getByTestId('turn-banner')).toBeTruthy())
    expect(screen.queryByTestId('assistant-turn-retry')).toBeNull()
  })
})

describe('the sentence a refusal was written with is the sentence the citizen reads', () => {
  it('★ a failed create is not overwritten by the composer own generic line', async () => {
    // The composer keeps `err.message` only for a refusal and overwrites anything else — so a
    // page-limit or a cap arrives as "that message did not send", and trying again cannot work.
    h.createConversation.mockRejectedValue(
      new Error('That file runs to 61 pages; the limit is 40.'),
    )
    mount()
    type('what is in this')

    await waitFor(() =>
      expect(screen.getByTestId('assistant-urgent').textContent).toMatch(/61 pages/i),
    )
    expect(screen.getByTestId('assistant-urgent').textContent).not.toMatch(/did not send/i)
  })

  it('★ and a failed upload keeps its own, at an address already taken', async () => {
    h.buildUserParts.mockRejectedValue(new Error('That file type cannot be attached.'))
    mount()
    type('what is in this')

    await waitFor(() =>
      expect(screen.getByTestId('assistant-urgent').textContent).toMatch(/cannot be attached/i),
    )
    expect(screen.getByTestId('assistant-urgent').textContent).not.toMatch(/did not send/i)
    expect(box().value).toBe('what is in this')
  })
})

describe('a reload while the reply is still being written', () => {
  const withEarlier = (over: Record<string, unknown> = {}) =>
    aConversation({
      messages: [
        { id: 'm1', role: 'user', parts: [{ type: 'text', text: 'earlier question' }], seq: 0 },
      ],
      ...over,
    })

  it('★ rejoins the running turn instead of sitting on a frozen transcript', async () => {
    h.getConversation.mockResolvedValue(withEarlier({ activeTurn: { turnId: 't9', lastSeq: 4 } }))
    let releaseStream: (value: unknown) => void = () => {}
    h.readTurnStream.mockImplementation(async ({ onFrame }: { onFrame: (f: unknown) => void }) => {
      onFrame({
        type: 'snapshot',
        seq: 4,
        turnId: 't9',
        turnStatus: 'running',
        items: [],
        parts: [{ type: 'text', text: 'half of the answer' }],
        working: false,
        errorMessage: null,
      })
      await new Promise((resolve) => (releaseStream = resolve))
      return 'completed'
    })
    mount('/assistant/c1')

    expect(await screen.findByText('half of the answer')).toBeTruthy()
    expect(h.readTurnStream.mock.calls[0][0].turnId).toBe('t9')
    // NO CURSOR: the catch-up snapshot IS the turn so far, and a cursor counts frames this tab
    // never received — passing it would tail past everything already written.
    expect(h.readTurnStream.mock.calls[0][0].cursor).toBeUndefined()
    // The reply is running HERE, so it can be stopped — the control a frozen transcript lacked.
    expect(await screen.findByTestId('stop-turn')).toBeTruthy()
    releaseStream('completed')
  })

  it('★ opens no stream at all for a conversation with nothing running', async () => {
    h.getConversation.mockResolvedValue(withEarlier())
    mount('/assistant/c1')

    expect(await screen.findByText('earlier question')).toBeTruthy()
    expect(h.readTurnStream).not.toHaveBeenCalled()
    expect(screen.queryByTestId('stop-turn')).toBeNull()
  })
})

describe('leaving a conversation by the navigation entry', () => {
  const withEarlier = () =>
    aConversation({
      messages: [
        { id: 'm1', role: 'user', parts: [{ type: 'text', text: 'earlier question' }], seq: 0 },
      ],
    })

  it('★ the address dropping its conversation returns the surface to the greeting', async () => {
    h.getConversation.mockResolvedValue(withEarlier())
    mount('/assistant/c1')
    expect(await screen.findByText('earlier question')).toBeTruthy()

    fireEvent.click(screen.getByTestId('nav-bial-chat'))

    await waitFor(() => expect(screen.getByTestId('assistant-greeting')).toBeTruthy())
    expect(screen.queryByText('earlier question')).toBeNull()
  })

  it('★ and the next message starts a new chat rather than appending to the one just left', async () => {
    // REACHED BY SENDING, which is the case that goes wrong: the address is claimed by the
    // minted id, so leaving without a reset leaves that id in hand and the next send lands in
    // the conversation the citizen thought they had left.
    mount()
    type('the first question')
    await waitFor(() => expect(h.createConversation).toHaveBeenCalledTimes(1))
    const firstId = h.createConversation.mock.calls[0][0].id

    fireEvent.click(screen.getByTestId('nav-bial-chat'))
    await waitFor(() => expect(screen.getByTestId('assistant-greeting')).toBeTruthy())
    type('a second, unrelated question')

    await waitFor(() => expect(h.createConversation).toHaveBeenCalledTimes(2))
    expect(h.createConversation.mock.calls[1][0].id).not.toBe(firstId)
    expect(h.startTurn.mock.calls[1][0]).not.toBe(firstId)
  })

  it('★ a conversation that is gone is left behind too', async () => {
    h.getConversation.mockResolvedValue(null)
    mount('/assistant/c1')
    expect(await screen.findByTestId('assistant-gone')).toBeTruthy()

    fireEvent.click(screen.getByTestId('nav-bial-chat'))

    await waitFor(() => expect(screen.getByTestId('assistant-greeting')).toBeTruthy())
    expect(screen.queryByTestId('assistant-gone')).toBeNull()
  })
})

describe('what the composer is told while a conversation cannot be sent to', () => {
  it('★ a gone conversation is not told to load itself — there is nothing to load', async () => {
    h.getConversation.mockResolvedValue(null)
    mount('/assistant/c1')
    await screen.findByTestId('assistant-gone')

    const note = screen.getByTestId('composer-gate-note')
    expect(note.textContent).toMatch(/start a new chat/i)
    expect(note.textContent).not.toMatch(/load this conversation/i)
  })

  it('★ a failed load still is, because that one genuinely can be', async () => {
    h.getConversation.mockRejectedValue(new TypeError('Failed to fetch'))
    mount('/assistant/c1')
    await screen.findByTestId('assistant-load-retry')

    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/load this conversation/i)
  })
})

describe('the thinking-versus-hung status', () => {
  it('★ says the agent has the floor before the first word, and stops saying it at the terminal', async () => {
    let releaseStream: (value: unknown) => void = () => {}
    h.readTurnStream.mockImplementation(async ({ onFrame }: { onFrame: (f: unknown) => void }) => {
      onFrame({ type: 'working', seq: 1, working: true })
      await new Promise((resolve) => (releaseStream = resolve))
      onFrame({ type: 'text_delta', seq: 2, text: 'Six stands are free.', newBlock: true })
      onFrame({ type: 'turn_ended', seq: 3, turnId: 't1', status: 'completed' })
      return 'completed'
    })
    mount()
    type('how many stands are free tonight')

    // BEFORE ANY PROSE — the moment the question "is this thinking or hung?" is asked hardest.
    expect(await screen.findByTestId('working-status')).toBeTruthy()

    releaseStream('go on')
    expect(await screen.findByText('Six stands are free.')).toBeTruthy()
    // The flag is edge-triggered, so a turn whose last act was thinking sends no falling edge:
    // the terminal is what brings the status down.
    await waitFor(() => expect(screen.queryByTestId('working-status')).toBeNull())
  })

  it('★ carries the status across a reload, so a tab that rejoined mid-thought is not still', async () => {
    h.getConversation.mockResolvedValue(
      aConversation({
        messages: [
          { id: 'm1', role: 'user', parts: [{ type: 'text', text: 'earlier question' }], seq: 0 },
        ],
        activeTurn: { turnId: 't9', lastSeq: 4 },
      }),
    )
    let releaseStream: (value: unknown) => void = () => {}
    h.readTurnStream.mockImplementation(async ({ onFrame }: { onFrame: (f: unknown) => void }) => {
      onFrame({
        type: 'snapshot',
        seq: 4,
        turnId: 't9',
        turnStatus: 'running',
        items: [],
        parts: [],
        working: true,
        errorMessage: null,
      })
      await new Promise((resolve) => (releaseStream = resolve))
      return 'completed'
    })
    mount('/assistant/c1')

    expect(await screen.findByTestId('working-status')).toBeTruthy()
    releaseStream('completed')
    await waitFor(() => expect(screen.queryByTestId('working-status')).toBeNull())
  })
})
