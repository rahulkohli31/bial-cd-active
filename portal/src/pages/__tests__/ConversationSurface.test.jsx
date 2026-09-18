/**
 * WHAT THIS FILE ITSELF CLAIMS — properties true of the surface as a WHOLE, not any one behaviour:
 * a running turn stays STOPPABLE now the old stop card is gone; exactly ONE control starts a
 * build; exactly ONE scroll container in the chat slot and no `calc(100vh - …)` anywhere; and no
 * chat list crept back during the rewrite.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, within, fireEvent } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import path from 'node:path'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  getStatus: vi.fn(), relaunchPreview: vi.fn(),
  fetchSaveState: vi.fn(), fetchPreviewState: vi.fn(), saveProject: vi.fn(),
  discardUnsavedChanges: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', async (orig) => ({
  ...(await orig()),
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  stopTurn: (...a) => h.stopTurn(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))
// The save-state read is the PRODUCER of the tri-state this file is about, so it is the one
// transport that must be controllable here.
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig()),
  fetchSaveState: (...a) => h.fetchSaveState(...a),
  // The workspace read, and the start `StartAppControl` imports DIRECTLY from this module rather
  // than through the injected client — both are what the failed-launch scenario at the bottom
  // drives, and leaving either real would put this suite on the network.
  fetchPreviewState: (...a) => h.fetchPreviewState(...a),
  relaunchPreview: (...a) => h.relaunchPreview(...a),
  // The WRITER of the bundle. It is the subject of the deployment-nudge scenario at the
  // bottom, and leaving it real would put this suite on the network there too.
  saveProject: (...a) => h.saveProject(...a),
  discardUnsavedChanges: (...a) => h.discardUnsavedChanges(...a),
}))

import {
  FakeEventSource, makeClient, primeClient, primeTurn, renderBuilder, send, waitForGateOpen,
  planReply, turnStreaming, PLAN_CARD_ID, findStartAppControl,
} from './_builderSession.jsx'
import { ApiError } from '../../utils/apiError'
import { discardNoticeText } from '../../utils/conversationApi'
import { DEFAULT_CONTEXT_SOFT } from '../../utils/contextLimits'

const deps = () => {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  primeClient(h)
  primeTurn(h)
  h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (t) => [{ type: 'text', text: t }])
  h.fetchSaveState.mockResolvedValue({ dirty: false })
  h.saveProject.mockResolvedValue({ appId: 'a1', headSha: 'ccc' })
  // Neither the workspace read nor the start is this file's subject by default: answered so
  // nothing reaches a real `fetch`, and re-primed by the two scenarios that are about them.
  h.fetchPreviewState.mockResolvedValue({
    state: 'unknown', alive: false, previewUrl: null,
    occupyingProjectName: null, occupyingProjectId: null, restorable: null,
  })
  h.relaunchPreview.mockResolvedValue({ appId: 'a1', previewUrl: 'https://app/', status: 'ready', ready: true, restoredFromFailedBuild: false })
})
afterEach(cleanup)

describe('a running turn is STILL stoppable now the card is gone', () => {
  it('the surface renders a stop control and pressing it calls the turn-stop path', async () => {
    // This checks the STOPPABLE claim AFTER the deletion, rather than trusting that the relocated
    // stop shipped before anything was removed.
    // THE SNAPSHOT IS WHAT CARRIES THE TURN ID, and every subscribe gets one first on cursor 0
    // (the server emits it before any model byte). Without it the control resolves no target and
    // correctly falls through to the legacy session stop — a real arm, but not the one under test.
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      onFrame({ type: 'snapshot', seq: 1, turnId: 'turn-7', turnStatus: 'running', items: [], parts: [], working: false })
      return new Promise(() => {}) // …and then the turn never lands
    })
    renderBuilder({ deps: deps().deps })
    await send('build me a thing')

    const stop = await screen.findByTestId('stop-turn')
    expect(stop.textContent).toMatch(/stop/i)
    fireEvent.click(stop)
    // The TURN stop, with the conversation and the turn read at PRESS time.
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith('build-X', 'turn-7'))

    // PAIRED WITH A LIVENESS ASSERTION, because a surface that rendered nothing would also have
    // no build card.
    expect(screen.getByTestId('composer-input')).toBeTruthy()
  })

  it('and nothing on the surface is a build-progress card any more', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
    renderBuilder({ deps: deps().deps })
    await send('build me a thing')
    await screen.findByTestId('stop-turn')

    for (const id of ['build-progress', 'build-bubble', 'build-activity', 'build-outcome', 'plan-options-card']) {
      expect(screen.queryByTestId(id), `${id} is still rendered`).toBeNull()
    }
  })
})

describe('exactly one control initiates a build', () => {
  it('counts the initiators across the WHOLE surface, not the absence of one in a top bar', () => {
    // Written this way deliberately. An assertion that a Build button is "not in the top bar"
    // would pass because the top bar is not in the rendered tree — and would keep passing if
    // someone added one there. Counting has teeth; querying for an element that was never going
    // to be there does not.
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Here is the plan.', PLAN_CARD_ID)))
    return (async () => {
      renderBuilder({ deps: deps().deps })
      await send('plan me a thing')

      const initiators = await screen.findAllByRole('button', { name: /^Build this plan$/ })
      expect(initiators).toHaveLength(1)
      // …and it is on the composer, where the offer lives — not in the transcript.
      expect(screen.getByTestId('composer').contains(initiators[0])).toBe(true)
    })()
  })
})

describe('one scroll container, and no viewport-height assertions', () => {
  it('exactly one `overflow-y-auto` inside the chat slot', async () => {
    renderBuilder({ deps: deps().deps })
    await waitForGateOpen()

    const panel = screen.getByTestId('chat-panel')
    const scrollers = panel.querySelectorAll('[class*="overflow-y-auto"]')
    // FOUR nested scrollers on the planning page and another on the builder is what this deletes.
    // `#chat-panel` itself is excluded by construction: it is `overflow-hidden`, not a scroller,
    // and it survives.
    expect(scrollers).toHaveLength(1)
    expect(panel.className).toContain('overflow-hidden')
  })

  it('no `calc(100vh - …)` anywhere in the chat surface’s source', () => {
    // The one in `ChatPage.tsx` was the only one in the repo and it died with that file. This is a
    // source-level guard because the failure is a layout that only misbehaves at certain heights —
    // something a jsdom render cannot see at all.
    const src = path.resolve(__dirname, '../..')
    for (const file of ['components/chat/ConversationSurface.tsx', 'components/chat/ChatThread.tsx', 'components/assistant-ui/thread.tsx']) {
      const source = readFileSync(path.join(src, file), 'utf8')
      const offending = source
        .split('\n')
        .filter((line) => /calc\(100vh/.test(line) && !line.trimStart().startsWith('*') && !line.trimStart().startsWith('//'))
      expect(offending, `${file} asserts a viewport height`).toEqual([])
    }
  })
})

describe('no chat list came back while the pages were being rewritten', () => {
  it('renders no list of conversations, in any state', async () => {
    // The chat list was removed earlier; this is the assertion that the rewrite around it did not
    // quietly restore one. Past conversations live on the project page the breadcrumb links to.
    h.listProjectConversations.mockResolvedValue([
      { id: 'other-1', kind: 'build', title: 'Another build', updatedAt: '2026-08-01T00:00:00Z' },
      { id: 'other-2', kind: 'plan', title: 'Some planning', updatedAt: '2026-08-02T00:00:00Z' },
    ])
    renderBuilder({ deps: deps().deps })
    await waitForGateOpen()

    const panel = within(screen.getByTestId('chat-panel'))
    expect(panel.queryByText('Another build')).toBeNull()
    expect(panel.queryByText('Some planning')).toBeNull()
    expect(panel.queryByRole('listbox')).toBeNull()
    // LIVENESS: the surface DID load those conversations — it reads them for the build-blocked
    // advisory — so their absence is a rendering decision rather than a failed fetch.
    await waitFor(() => expect(h.listProjectConversations).toHaveBeenCalled())
  })
})

describe('the per-conversation guardrail reaches the composer', () => {
  // ★ WHY THIS FILE AND NOT A UNIT TEST: `contextLimits.ts` and `Composer`'s rendering of the
  // prop are BOTH unit-tested, and both stayed green while the one line joining them was
  // deleted — covering the parts never covered the wiring.
  //
  // So this asserts the SEAM: a long conversation loaded into the surface puts the sentence on
  // the composer. Delete the `contextWarning` prop pass in `ConversationSurface.tsx`, or the
  // `useMemo` that feeds it, and this is what goes red.
  //
  // WHAT "A LONG CONVERSATION" MEANS, AND WHY THIS FIXTURE IS SHAPED THIS WAY
  //
  // It used to be a pile of characters: 600,000 of them, priced at four to the token by an
  // estimator this browser ran. That estimator is deleted on both sides — it read a 61-page
  // document as 1,600 tokens when it really cost 153,342 — so a transcript's LENGTH now tells
  // the browser nothing at all, and a fixture built out of characters would be asserting against
  // a guess nobody makes any more.
  //
  // The conversation is long because the SERVER says it is: `contextTokens` on the read is the
  // raw prompt count the provider reported, the same figure the send route refuses on. The wider
  // wiring — the send's own 202, the chat switch, the unmeasured case — is
  // `ConversationSurface-contextmeter.test.tsx`'s; what stays here is the seam this file exists
  // for, in the file that noticed it going missing the first time.
  const conversationOf = (contextTokens) => ({
    id: 'build-X',
    kind: 'build',
    messages: [{ id: 'm1', role: 'user', parts: [{ type: 'text', text: 'make it nicer' }] }],
    contextTokens,
  })

  it('a conversation past the soft threshold warns on the composer', async () => {
    // The default soft threshold, as the profile-less session resolves it.
    h.getBuild.mockResolvedValue(conversationOf(DEFAULT_CONTEXT_SOFT))
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
    renderBuilder({ deps: deps().deps })

    const warning = await screen.findByTestId('composer-context-warning')
    expect(warning.textContent).toMatch(/new chat/i)
  })

  it('and an ordinary conversation says nothing', async () => {
    h.getBuild.mockResolvedValue(conversationOf(1_000))
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
    renderBuilder({ deps: deps().deps })

    // PAIRED WITH A LIVENESS ASSERTION. `queryByTestId(...) === null` is also what a surface
    // that threw would produce, and this repo has been bitten by exactly that: the absence only
    // means something once the composer is proven to be on screen next to it.
    await waitForGateOpen()
    expect(screen.getByTestId('composer-input')).toBeTruthy()
    expect(screen.queryByTestId('composer-context-warning')).toBeNull()
  })

  it('★ and a conversation NOBODY has measured says nothing either', async () => {
    // The honest cost of reading a measurement instead of guessing one: a chat the provider has
    // never served has no figure, and the line stays off rather than being invented. Asserted so
    // that "silent" is a decision on this branch and not an accident of the fixture above.
    h.getBuild.mockResolvedValue(conversationOf(null))
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
    renderBuilder({ deps: deps().deps })

    await waitForGateOpen()
    expect(screen.getByTestId('composer-input')).toBeTruthy()
    expect(screen.queryByTestId('composer-context-warning')).toBeNull()
  })
})

describe('the offer\'s Build opens no question either', () => {
  it('★ reports a refusal in the server\'s own words, and puts nothing up to be answered', async () => {
    // THE THIRD DOOR: three presses can meet a refusal about the one workspace — a rail send, the
    // pane's start control, and this one — and this is the one of the three with no coverage
    // elsewhere. What it proves is that it degrades the same way they do: a sentence, not a
    // dialog, because the workspace follows whichever project asked for it.
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Here is the plan.', PLAN_CARD_ID)))
    h.buildFromPlan.mockRejectedValue(
      Object.assign(new Error('“Car pool” is open for a colleague right now.'), {
        code: 'sandbox_reclaim_blocked',
        details: { projectId: 'pA', projectName: 'Car pool', dirty: false, building: false, isSharedView: true },
      }),
    )
    renderBuilder({ deps: deps().deps })
    await send('plan me a thing')

    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))

    // The server's sentence reaches the citizen where they are standing…
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain('“Car pool” is open for a colleague right now.'),
    )
    // …and nothing was put to them to decide.
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('a failed launch INSIDE a chat says why', () => {
  it('puts the server\'s reason on the pane, not just a stopped spinner', async () => {
    // This surface's own launch path hands the shared map a real outcome now — a hardcoded `null`
    // here would silently swallow the pane's own failure and show only a stopped spinner.
    h.fetchPreviewState.mockResolvedValue({
      state: 'asleep', alive: false, previewUrl: null,
      occupyingProjectName: null, occupyingProjectId: null, restorable: true,
    })
    h.relaunchPreview.mockRejectedValue(
      new ApiError('Your app could not be brought back just now.', 503),
    )
    renderBuilder({ deps: deps().deps })

    fireEvent.click(await findStartAppControl())

    // ★ THE SERVER'S OWN WORDS, CARRIED VERBATIM — and that is the whole of what a failed press
    // changes on screen now.
    //
    // IT USED TO GET A CARD OF ITS OWN, headed "We could not start your app." That headline is
    // deleted: the situation, the honest headline and the next step were all identical to "Your
    // app is saved.", so a differently-shaped screen told the citizen something had changed that
    // had not. The reason rides in the map's `note`, which is also the one field the negative-copy
    // sweep exempts — so a refusal containing the words "not running" can no longer turn a green
    // suite red on a string this client does not control.
    expect(await screen.findByText('Your app could not be brought back just now.')).toBeTruthy()
    expect(screen.queryByText('We could not start your app.')).toBeNull()
    // LIVENESS, PAIRED WITH THAT ABSENCE: the pane is on the saved card with its one press still
    // offered, so the missing headline is a deleted card rather than a pane that stopped
    // rendering. Pressing Launch again is non-destructive by construction — the action union
    // contains no restore, rebuild or teardown verb.
    expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe('not-running')
    expect(screen.getByText('Your app is saved.')).toBeTruthy()
    expect(await findStartAppControl()).toBeTruthy()
  })
})

/**
 * ★ A SAVE FROM THE CHAT RAISES THE DEPLOYMENT NUDGE — the other half of the seam.
 *
 * `savedHead` and `savedAt` are fields of the DEPLOYMENT read, and a Save is what changes them.
 * The surface performing the save holds no such read: the row is drawn by `AppStatusPanel` and
 * the state by the toolbar's publish chip, each with its own `usePublishState`. So the save's only
 * way to correct them is the `bial:deployment-changed` nudge both already listen for.
 *
 * THE PROJECT SCREEN'S SAVE RAISES IT AND THE CHAT'S DID NOT, which is exactly the kind of gap
 * neither side's unit tests can see: `announceDeploymentChanged` is exported and tested, the hook's
 * listener is tested, and the chip stayed one read behind anyway because the one line joining them
 * was missing on this surface. Delete `announceDeploymentChanged(activeProjectId)` from
 * `handleSave` and this is what goes red.
 */
describe('★ a Save from the chat raises the deployment nudge', () => {
  /** Every nudge the window saw, in order. A CustomEvent is the whole mechanism, so listening for
   *  it is watching the real wire rather than a spy standing in for one. */
  const nudges = []
  const record = (event) => nudges.push(event.detail)
  beforeEach(() => {
    nudges.length = 0
    window.addEventListener('bial:deployment-changed', record)
  })
  afterEach(() => window.removeEventListener('bial:deployment-changed', record))

  it('names the project it saved, and the save itself goes through', async () => {
    // Dirty, or the toolbar's Save is a chip rather than a button and the press does nothing.
    h.fetchSaveState.mockResolvedValue({ dirty: true })
    renderBuilder({ deps: deps().deps })

    fireEvent.click(await screen.findByTestId('save-project'))

    // LIVENESS FIRST: the save actually happened. Without this the nudge assertion below would
    // stay green over a Save that never wrote anything — an event about nothing.
    await waitFor(() => expect(h.saveProject).toHaveBeenCalledWith('p1'))
    // …and the toolbar has taken the answer, so the press ran to completion rather than throwing.
    expect(await screen.findByText('Saved')).toBeTruthy()

    // THE NUDGE, ONCE, NAMING THIS PROJECT. The id is the whole payload a listener acts on: a
    // nudge for someone else's project is one every mount here correctly ignores.
    await waitFor(() => expect(nudges).toHaveLength(1))
    expect(nudges[0].projectId).toBe('p1')
  })

  it('★ says so on the assertive slot when the save does not land', async () => {
    // A Save that fails silently leaves the citizen believing their work is stored, and the small
    // alert beside the control is not where somebody mid-conversation is looking. The server's
    // own sentence, on the same slot every other failure here lands on.
    // Mutation check: point the catch arm back at the inline slot and the banner never appears.
    h.fetchSaveState.mockResolvedValue({ dirty: true })
    h.saveProject.mockRejectedValue(new Error('Your workspace is no longer running.'))
    renderBuilder({ deps: deps().deps })

    fireEvent.click(await screen.findByTestId('save-project'))

    const banner = await screen.findByTestId('urgent-banner')
    expect(banner.textContent).toMatch(/no longer running/i)
  })

  it('falls back to a plain sentence when the failure carries none', async () => {
    h.fetchSaveState.mockResolvedValue({ dirty: true })
    h.saveProject.mockRejectedValue('nope')
    renderBuilder({ deps: deps().deps })

    fireEvent.click(await screen.findByTestId('save-project'))

    const banner = await screen.findByTestId('urgent-banner')
    expect(banner.textContent).toBe('Your app was not saved. Try again.')
  })
})

/**
 * ★ THE SAVE CHIP ON A CHAT FOLLOWS THE WORKSPACE, NOT ONLY THE TURNS.
 *
 * A save-state read describes the tree it was taken against, and it went stale two ways here: a
 * read already on the wire when Save was pressed landed afterwards and lit Save again, and a
 * workspace that came up after the page loaded was never read at all.
 */
describe('★ the Save chip on a chat follows the workspace, not only the turns', () => {
  it('a read already on the wire when Save is pressed does not light Save again', async () => {
    // Mutation check: drop the sequence bump from `handleSave` and the late read relights Save.
    h.fetchSaveState.mockResolvedValue({ dirty: true })
    renderBuilder({ deps: deps().deps })
    await screen.findByTestId('save-project')

    let answerLate = null
    h.fetchSaveState.mockImplementation(() => new Promise((resolve) => { answerLate = resolve }))
    await send('add a date filter')
    await waitFor(() => expect(answerLate).toBeTypeOf('function'))

    fireEvent.click(screen.getByTestId('save-project'))
    await waitFor(() => expect(h.saveProject).toHaveBeenCalledWith('p1'))
    expect(await screen.findByText('Saved')).toBeTruthy()

    answerLate({ dirty: true })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.getByText('Saved')).toBeTruthy()
    expect(screen.queryByText(/^Save$/)).toBeNull()
  })

  it('a workspace that comes up after the page loaded is read again', async () => {
    // Mutation check: remove the re-read on arrival and only the mount read ever happens.
    h.fetchPreviewState.mockResolvedValue({
      state: 'asleep', alive: false, previewUrl: null,
      occupyingProjectName: null, occupyingProjectId: null, restorable: true,
    })
    h.fetchSaveState.mockResolvedValue({ dirty: null })
    renderBuilder({ deps: deps().deps })
    const launch = await findStartAppControl()
    await waitFor(() => expect(h.fetchSaveState).toHaveBeenCalledTimes(1))

    h.fetchPreviewState.mockResolvedValue({
      state: 'alive', alive: true, previewUrl: 'https://app/',
      occupyingProjectName: null, occupyingProjectId: null, restorable: null,
    })
    h.fetchSaveState.mockResolvedValue({ dirty: true })
    fireEvent.click(launch)

    await waitFor(() => expect(h.fetchSaveState).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('save-project')).toBeTruthy()
  })

  it('★ a Discard sends this chat, shows the line its next reply reads, and settles Save', async () => {
    // Mutation check: drop the chat's id, the appended line, or the returned save state.
    const SAVED = 'a'.repeat(40)
    h.fetchSaveState.mockResolvedValue({ dirty: true, savedHead: SAVED })
    h.discardUnsavedChanges.mockResolvedValue({
      saveState: { appId: 'a1', dirty: false, containerHead: SAVED, savedHead: SAVED },
      notice: { seq: 9, savedAt: '2026-09-13T14:32:00Z' },
    })
    renderBuilder({ deps: deps().deps })
    await send('add a date filter')

    const control = await screen.findByTestId('discard-changes')
    await waitFor(() => expect(control.getAttribute('aria-disabled')).toBe('false'))
    fireEvent.click(control)
    fireEvent.click(await screen.findByTestId('discard-dialog-confirm'))

    await waitFor(() => expect(h.discardUnsavedChanges).toHaveBeenCalledWith('p1', 'build-X'))
    expect(await screen.findByText(discardNoticeText('2026-09-13T14:32:00Z'))).toBeTruthy()
    expect(await screen.findByText('Saved')).toBeTruthy()
    expect(screen.getByTestId('discard-changes').getAttribute('aria-disabled')).toBe('true')
  })

  it('a from-scratch chat showing only the welcome greeting discards with no conversation', async () => {
    // Mutation check: revert the `!m.ephemeral` filter in `handleDiscard` and this sends the
    // chat's buildId instead of null.
    const SAVED = 'a'.repeat(40)
    h.fetchSaveState.mockResolvedValue({ dirty: true, savedHead: SAVED })
    h.discardUnsavedChanges.mockResolvedValue({
      saveState: { appId: 'a1', dirty: false, containerHead: SAVED, savedHead: SAVED },
      notice: null,
    })
    renderBuilder({ deps: deps().deps })
    await screen.findByText(/Tell me what you'd like to build/i)

    const control = await screen.findByTestId('discard-changes')
    await waitFor(() => expect(control.getAttribute('aria-disabled')).toBe('false'))
    fireEvent.click(control)
    fireEvent.click(await screen.findByTestId('discard-dialog-confirm'))

    await waitFor(() => expect(h.discardUnsavedChanges).toHaveBeenCalledWith('p1', null))
    expect(await screen.findByText('Saved')).toBeTruthy()
    expect(screen.queryByText(/you discarded the unsaved changes/i)).toBeNull()
  })

  it('a Discard waits while this chat is replying', async () => {
    // Mutation check: publish `replying: false` from this page and the control stays pressable.
    h.fetchSaveState.mockResolvedValue({ dirty: true, savedHead: 'a'.repeat(40) })
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
    renderBuilder({ deps: deps().deps })
    await send('add a date filter')

    const control = await screen.findByTestId('discard-changes')
    await waitFor(() => expect(control.getAttribute('title')).toBe('Wait for the reply to finish'))
    expect(control.getAttribute('aria-disabled')).toBe('true')
  })

  it('a page that opens on a running workspace reads it once', async () => {
    // Mutation check: let the first preview answer re-read too and every page load asks twice.
    h.fetchPreviewState.mockResolvedValue({
      state: 'alive', alive: true, previewUrl: 'https://app/',
      occupyingProjectName: null, occupyingProjectId: null, restorable: null,
    })
    h.fetchSaveState.mockResolvedValue({ dirty: true })
    renderBuilder({ deps: deps().deps })

    // Liveness: the running answer has landed and framed the app, so the count below is final.
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    expect(await screen.findByTestId('save-project')).toBeTruthy()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(h.fetchSaveState).toHaveBeenCalledTimes(1)
  })
})
