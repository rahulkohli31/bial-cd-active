/**
 * THE RAIL'S COMPOSER — the mint-and-navigate protocol and the kind picker. Two things are under
 * test, and they fail differently.
 *
 * THE PROTOCOL is inherited from a now-deleted component, and the deletion is exactly how it gets
 * lost: the id's version, the query/state split, and the `freshlyMinted` flag are all invisible in
 * a render and only wrong later — a v4 id becomes a badly-ordered primary key, a kind carried in
 * router state dies on reload, a missing flag costs four guaranteed-404 requests per new chat.
 *
 * THE PICKER is new: a chat's kind is fixed at creation, and the retired composer hardcoded the
 * build kind, so without this control the rail could only ever mint Build chats.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import RailComposer from '../RailComposer'
import AppPane from '../AppPane'
import { WORKSPACE_RAIL_ID } from '../railId'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  type WorkspaceReport,
} from '../workspaceChannel'
import { resolveWorkspaceState } from '../workspaceState'
import type { PreviewState } from '../../../utils/buildSessionApi'

// THE START THIS RAIL ASKS FOR, held by the test rather than answered by the network. The rail
// AWAITS `relaunchPreview` and navigates only afterwards, so a mock that resolves immediately would
// close the very window the last block below is about.
const api = vi.hoisted(() => ({ relaunchPreview: vi.fn() }))
vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  relaunchPreview: api.relaunchPreview,
}))

// The words a citizen reads come from the bootstrap catalogue, not from this file — so a suite that
// does not stand one up gets the honest "Chat" fallback on every option and every label assertion
// fails for a reason that has nothing to do with this component.
vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

/** Where a navigation actually landed, plus the router state it carried. */
function LocationProbe() {
  const loc = useLocation()
  return (
    <div>
      <span data-testid="path">{loc.pathname + loc.search}</span>
      <span data-testid="state">{JSON.stringify(loc.state)}</span>
    </div>
  )
}

function renderComposer(projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={['/projects/p1']}>
      <Routes>
        <Route path="/projects/:projectId" element={<RailComposer projectId={projectId} />} />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

// BY TESTID, NOT BY PLACEHOLDER. The hint now follows the picked kind (a Plan chat changes
// nothing, so it does not ask for "the change you need"), and a helper keyed on one kind's
// wording cannot reach the box in the other — which is the very case the ★ test below drives.
const composer = () => screen.getByTestId('composer-input')
const path = () => screen.getByTestId('path').textContent ?? ''
const routerState = () => JSON.parse(screen.getByTestId('state').textContent || 'null') as unknown

const send = (text = 'a visitor log') => {
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.click(screen.getByTestId('composer-send'))
}

beforeEach(() => {
  // Radix needs these in jsdom; the toggle group's items are focusable roving-tabindex controls.
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  cleanup()
  // The draft store is `sessionStorage`, so a test that types leaves a draft behind for the next
  // one — which would hydrate a box some other assertion expects to be empty.
  sessionStorage.clear()
})

// THE READ, HELD OPEN. `fileToBase64` is the boundary where the browser hands bytes back, and
// holding it is the only way to stand inside the window the next block is about. Everything else in
// `attachmentInput` is the real module. (`vi.mock` and `vi.hoisted` are hoisted, so their position
// here is only for the reader.)
const reads = vi.hoisted(() => ({ fileToBase64: vi.fn() }))
vi.mock('../../../utils/attachmentInput', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/attachmentInput')>()),
  fileToBase64: reads.fileToBase64,
}))

describe('★ the rail holds Send while a file is still being read', () => {
  it('does not start a chat mid-read, and says a file is arriving', async () => {
    // The rail binds the same adapter as the chat composer, stages the same files and renders the
    // same box — but it never mounted the pending-read provider, so the count it read was the
    // context default 0 and Send went straight through. A workbook dropped here and sent before its
    // read finished started the chat from the sentence alone, and the file landed nowhere.
    //
    // Mutation receipt: mount `RefusalSinkProvider` + `StagedAttachmentsBinding` by hand again,
    // without `PendingReadsProvider`, and this navigates.
    reads.fileToBase64.mockImplementation(() => new Promise<string>(() => {}))
    renderComposer()

    fireEvent.drop(screen.getByTestId('composer-dropzone'), {
      dataTransfer: {
        types: ['Files'],
        files: [new File(['id,name\n1,Priya'], 'roster.csv', { type: 'text/csv' })],
      },
    })
    await waitFor(() => expect(reads.fileToBase64).toHaveBeenCalledTimes(1))

    send('what is in this roster?')

    // No navigation: the chat was not started without its file.
    expect(screen.queryByTestId('path')).toBeNull()
    // And the box says why it is waiting.
    expect(screen.getByTestId('composer-pending').textContent).toMatch(/adding your file/i)
  })
})

describe('the mint-and-navigate protocol, carried through the deletion', () => {
  it('mints a UUIDv7, not a v4 — this id becomes a primary key', () => {
    // The id must be a sortable primary key. Two sites each kept a private `crypto.randomUUID()`
    // and both went on producing v4 long after the store's mint moved on. Nothing about the screen
    // looks different when this is wrong.
    renderComposer()
    send()

    const id = path().split('?')[0].replace('/chat/', '')
    // The version nibble: the 15th hex digit of a UUID is its version.
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })

  it('carries the project and the kind as QUERY, and the draft as router STATE', () => {
    // The split is deliberate: router state dies on reload and never travels in a shared link, so a
    // bookmarked `/chat/{id}` must still be able to take its kind from somewhere. The draft is the
    // opposite — this navigation's payload, with no business surviving a reload or sitting in a URL.
    renderComposer()
    send('a visitor log')

    expect(path()).toMatch(/\?projectId=p1&kind=plan$/)
    expect(routerState()).toMatchObject({ prompt: 'a visitor log', freshlyMinted: true })
  })

  it('sets `freshlyMinted`, so the route skips a GET that can only 404', () => {
    // The row does not exist until the first message, so the chat route's hydration fetch is a
    // guaranteed 404 — doubled by two hydration fetches and doubled again by StrictMode in dev.
    renderComposer()
    send()

    expect(routerState()).toMatchObject({ freshlyMinted: true })
  })

  it('encodes a project id that would otherwise break the query', () => {
    renderComposer('p 1&kind=plan')
    send()

    expect(path()).toContain('projectId=p%201%26kind%3Dplan')
    // The REAL kind is the trailing one the picker wrote; the encoded literal above is part of the
    // project id and must not be mistaken for it. That is the whole point of this test.
    expect(path()).toMatch(/&kind=plan$/)
  })

  it('navigates nowhere on an empty or whitespace-only draft', () => {
    renderComposer()
    fireEvent.change(composer(), { target: { value: '   ' } })
    fireEvent.click(screen.getByTestId('composer-send'))

    expect(screen.queryByTestId('path')).toBeNull()
  })

  it('blocks a guard-railed prompt before any navigation, and says why', () => {
    renderComposer()
    // A prompt the shared guardrails reject — `spy on` is one of the harmful-content keywords.
    // If it ever stops being rejected this goes red rather than silently proving nothing.
    fireEvent.change(composer(), { target: { value: 'an app to spy on the ground crew' } })
    fireEvent.click(screen.getByTestId('composer-send'))

    expect(screen.queryByTestId('path')).toBeNull()
    expect(screen.getByRole('dialog', { name: /prompt blocked/i })).toBeTruthy()
  })
})

describe('the guardrail hands focus back when it closes', () => {
  // THE DIALOG IS HAND-ROLLED — no Radix `DialogContent`, so no `FocusScope` capturing the
  // element that had focus and restoring it on unmount. Both routes out of it dropped focus on
  // `<body>`, where the next Tab restarts at the top of the document: a keyboard citizen who
  // pressed Send had to tab back through the whole shell to reach the message they had just been
  // told to edit. The box is the target because the refusal keeps everything typed.
  const BLOCKED = 'an app to spy on the ground crew'

  const openGuardRail = () => {
    renderComposer()
    send(BLOCKED)
    // LIVENESS FIRST. Every assertion below is about a dialog closing; if it never opened, an
    // assertion that it is gone passes on nothing at all.
    expect(screen.getByRole('dialog', { name: /prompt blocked/i })).toBeTruthy()
  }

  it('“Edit My Prompt” puts the caret back in the message', () => {
    openGuardRail()

    fireEvent.click(screen.getByRole('button', { name: 'Edit My Prompt' }))

    expect(screen.queryByRole('dialog')).toBeNull()
    // PAIRED LIVENESS: the composer is still mounted and still holds the refused message, so
    // "focus is on the box" is a statement about a live box with the text in it — not about a
    // component that unmounted or emptied itself under the assertion.
    expect((composer() as HTMLTextAreaElement).value).toBe(BLOCKED)
    expect(document.activeElement).toBe(composer())
  })

  it('dismissing it with the corner control lands focus in the same place', () => {
    // The other way out. A citizen who closes rather than accepts the advice is in exactly the
    // same position — the message is still there and still needs editing — so both routes lead
    // to the box, and neither may leave focus on `<body>`.
    openGuardRail()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    expect(screen.queryByRole('dialog')).toBeNull()
    expect((composer() as HTMLTextAreaElement).value).toBe(BLOCKED)
    expect(document.activeElement).toBe(composer())
  })
})

describe('the cold return leg — a draft that outlives the trip to a chat', () => {
  /** The two moves a citizen makes with the rail: into a chat, and back to the project. */
  function Trip() {
    const navigate = useNavigate()
    return (
      <>
        <button type="button" data-testid="to-chat" onClick={() => navigate('/chat/c-1')}>
          open a chat
        </button>
        <button type="button" data-testid="to-project" onClick={() => navigate('/projects/p1')}>
          back to the project
        </button>
      </>
    )
  }

  const roundTrip = () =>
    render(
      <MemoryRouter initialEntries={['/projects/p1']}>
        <Trip />
        <Routes>
          <Route path="/projects/:projectId" element={<RailComposer projectId="p1" />} />
          <Route path="/chat/:chatId" element={<div data-testid="a-chat" />} />
        </Routes>
      </MemoryRouter>,
    )

  it('★ gives back the half-written description after a chat and back', () => {
    // THE LONGEST MESSAGE ANYBODY WRITES lives in this box — the one describing the whole app —
    // and the project surface is an Outlet child, so every move to a chat address unmounts it.
    // The text is kept in the runtime this component creates per mount, so without the draft
    // store behind it a citizen who stepped into a chat and came back found an empty box and
    // nothing to tell them why.
    roundTrip()
    fireEvent.change(composer(), { target: { value: 'a visitor log with an out-time column' } })

    fireEvent.click(screen.getByTestId('to-chat'))
    // The composer really did go away — so the restore below is a restore, not a box that never
    // unmounted.
    expect(screen.queryByTestId('composer-input')).toBeNull()
    expect(screen.getByTestId('a-chat')).toBeTruthy()

    fireEvent.click(screen.getByTestId('to-project'))

    expect((composer() as HTMLTextAreaElement).value).toBe('a visitor log with an out-time column')
  })

  it('keeps each project’s draft to itself', () => {
    // The store is keyed by conversation, and this surface stamps the PROJECT as that key. A
    // shared key would hand one project's description to another project's rail.
    roundTrip()
    fireEvent.change(composer(), { target: { value: 'a visitor log' } })
    cleanup()

    render(
      <MemoryRouter initialEntries={['/projects/p2']}>
        <Routes>
          <Route path="/projects/:projectId" element={<RailComposer projectId="p2" />} />
        </Routes>
      </MemoryRouter>,
    )

    expect((composer() as HTMLTextAreaElement).value).toBe('')
  })
})

describe('the kind picker — the control that makes the other half of the product reachable', () => {
  it('offers both kinds, with the words from the shared catalogue', () => {
    renderComposer()

    expect(screen.getByRole('radio', { name: 'Build' })).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Plan' })).toBeTruthy()
  })

  it('★ mints the kind that was PICKED, not the one that was hardcoded', () => {
    // The whole point. The retired composer wrote `kind=build` into the address unconditionally,
    // so a Plan chat could not be created from a project at all.
    renderComposer()
    fireEvent.click(screen.getByRole('radio', { name: 'Plan' }))
    send()

    expect(path()).toMatch(/&kind=plan$/)
  })

  it('★ defaults to PLAN, so a first prompt is planned rather than built from', () => {
    // CHANGED BY DECISION, 2026-09-10. It used to default to Build, inherited from the retired
    // composer, and the argument for keeping it was that changing it would silently change what
    // the control does for anyone who never touches the picker. That is exactly what it now does,
    // deliberately: a rough first sentence gets a plan to read and a `Build this plan` button
    // instead of a container and several minutes of the model spent on a guess.
    renderComposer()
    send()

    expect(path()).toMatch(/&kind=plan$/)
  })

  it('reads its one line of explanation from the catalogue, never from this file', () => {
    // One source for what a kind IS. A second wording here would drift the first time the
    // server's changed, and nothing would notice.
    renderComposer()
    expect(screen.getByTestId('kind-description').textContent).toBe('Shape a plan first.')

    fireEvent.click(screen.getByRole('radio', { name: 'Build' }))
    expect(screen.getByTestId('kind-description').textContent).toBe('Change the live app.')
  })

  it('★ cannot reach a third, empty state by re-pressing the active option', () => {
    // Radix hands back `''` when a single-select item is pressed while already on. A chat is always
    // one kind or the other, so an empty value is not a state this control may reach — and the mint
    // below it would put `kind=` in the address with nothing after it.
    renderComposer()
    const build = screen.getByRole('radio', { name: 'Build' })
    fireEvent.click(build)
    fireEvent.click(build)
    send()

    expect(path()).toMatch(/&kind=build$/)
  })
})

/**
 * ★ ONE ANNOUNCEMENT FOR ONE WAIT — and this file owns the FIRST of the three that used to fire.
 *
 * WHAT WAS MEASURED ON 2026-09-10: a citizen who sends their first message from this rail got
 * THREE announcements inside two seconds, for one continuous wait. This rail raises the
 * workspace's start flag (the pane then says "Getting your app ready."), `ChatRoute` opens and
 * closes a second polite region on the way to the chat, and the surface publishes the same state
 * a third time on arrival.
 *
 * ★ THE FIRST ONE WAS PROPOSED FOR DELETION, ON A READING OF THIS FILE THAT IS FALSE. The argument
 * was that `onStartPending(true)` "fires on a ProjectWorkspace that navigation is about to unmount
 * … for a page nobody sees". The ORDER is the proof that it does not: the flag goes up, then
 * `relaunchPreview` is AWAITED, and only then does the navigate happen. That await is the attach
 * or the cold restore — bounded server-side at `_COLD_READY_BUDGET_SECONDS` — and the citizen
 * spends every second of it on THIS page watching THIS pane. Delete the flag and the whole wait is
 * silent: the pane goes on saying "Your app is saved." over a start that is already running, and a
 * screen reader is told nothing at all.
 *
 * SO BOTH HALVES ARE PINNED: the sentence exists for the whole wait, and there is exactly ONE of
 * it. The middle announcement's suppression is pinned in `ChatRoute.test.tsx` ("paints the wait
 * board but does not ANNOUNCE it on a freshly minted arrival").
 */
describe('★ the rail is the pane`s only narrator for the whole start, and says it once', () => {
  /** A promise this test opens and closes by hand, so the MIDDLE of the wait is observable. */
  function deferred<T>() {
    let settle!: (value: T) => void
    const promise = new Promise<T>((res) => {
      settle = res
    })
    return { promise, settle }
  }

  const ASLEEP: PreviewState = {
    state: 'asleep',
    alive: false,
    previewUrl: null,
    occupyingProjectName: null,
    occupyingProjectId: null,
    restorable: true,
  }

  /**
   * The rail and the pane on one channel, with `onStartPending` wired through the REAL map —
   * exactly as both production publishers wire it (the project hook's `reportStartPending` and the
   * chat surface's `setStartPending`).
   *
   * WITHOUT THAT WIRING THIS WOULD BE VACUOUS: a harness holding one frozen state cannot show a
   * flag moving a pane, so every assertion below would pass against a rail that raised none.
   */
  function railAndPane() {
    const channel = createWorkspaceChannel()
    channel.visible.set(true)
    const stateFor = (startInFlight: boolean) =>
      resolveWorkspaceState({
        preview: ASLEEP,
        lastDecidedPreview: null,
        projectHasSavedBuild: null,
        startOutcome: null,
        startInFlight,
      })
    const report: WorkspaceReport = {
      state: stateFor(false),
      projectId: 'p1',
      onStarted: vi.fn(),
      onStartPending: vi.fn((pending: boolean) => {
        act(() => channel.workspace.set({ ...report, state: stateFor(pending) }))
      }),
      onStartOutcome: vi.fn(),
      onRefresh: vi.fn(),
      onReclaimRefusal: vi.fn(),
    }
    channel.workspace.set(report)
    return {
      report,
      ...render(
        <MemoryRouter initialEntries={['/projects/p1']}>
          <WorkspaceChannelProvider value={channel}>
            <Routes>
              <Route
                path="/projects/:projectId"
                element={
                  <>
                    <div id={WORKSPACE_RAIL_ID} />
                    <RailComposer projectId="p1" />
                    <AppPane device="Desktop" reloadNonce={0} />
                  </>
                }
              />
              <Route path="*" element={<LocationProbe />} />
            </Routes>
          </WorkspaceChannelProvider>
        </MemoryRouter>,
      ),
    }
  }

  const paneSays = () => screen.queryByTestId('app-pane-empty')?.textContent ?? ''

  it('★ says "Getting your app ready." for the WHOLE wait, before the navigate and not after', async () => {
    // Mutation receipt: delete `report.onStartPending(true)` from `startChat` and this goes red on
    // the mid-wait assertion — the pane sits on "Your app is saved." for the length of the restore.
    const hold = deferred<{ previewUrl: string; ready: boolean }>()
    api.relaunchPreview.mockReturnValue(hold.promise)
    railAndPane()

    // BEFORE: the honest at-rest sentence, with the one press that starts the app.
    expect(paneSays()).toContain('Your app is saved.')

    send('a visitor log')

    // ★ MID-WAIT, WHICH IS THE WHOLE POINT. The request is still in flight and the citizen is
    // still on this page — `open()` sits BELOW the await, so no navigation has happened yet.
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p1' }))
    expect(paneSays()).toContain('Getting your app ready.')
    expect(screen.queryByTestId('path')).toBeNull()

    // ★ AND EXACTLY ONE ELEMENT SAYS IT. Three authors for one wait is the measured defect, and
    // this is the half of it that lives on this screen: a second polite region under the composer
    // repeating the pane's sentence would be the same duplication in another spelling.
    expect(screen.getAllByText('Getting your app ready.')).toHaveLength(1)

    // …and the navigate lands only once the server has answered.
    await act(async () => {
      hold.settle({ previewUrl: 'https://app.example/', ready: true })
      await Promise.resolve()
    })
    await waitFor(() => expect(screen.getByTestId('path').textContent).toMatch(/^\/chat\//))
  })

  it('★ and the wait names no duration, however long it runs', async () => {
    // The rule the pane's copy is written under, asserted from the surface that OPENS the wait:
    // nobody has measured a cold start, so no sentence may name one.
    const hold = deferred<{ previewUrl: string; ready: boolean }>()
    api.relaunchPreview.mockReturnValue(hold.promise)
    railAndPane()
    send('a visitor log')
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalled())

    const board = screen.getByTestId('app-pane-empty')
    // The elapsed counter is exempt and deliberately so: it is read off the pane's own clock as it
    // passes, which is a measured fact rather than a duration claimed in advance.
    const sentences = [...board.querySelectorAll('p')]
      .filter((el) => el.getAttribute('data-testid') !== 'app-pane-elapsed')
      .map((el) => el.textContent ?? '')
      .join(' ')
    expect(sentences).not.toMatch(/\d/)
    expect(sentences).not.toMatch(
      /\b(second|seconds|minute|minutes|moment|moments|about|roughly|soon|shortly)\b/i,
    )
    // LIVENESS: the wait really is on screen, so the absences above are about a rendered board.
    expect(sentences).toContain('Getting your app ready.')

    await act(async () => {
      hold.settle({ previewUrl: 'https://app.example/', ready: true })
      await Promise.resolve()
    })
  })
})
