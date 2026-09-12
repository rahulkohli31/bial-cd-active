/**
 * The build outcome, portal side.
 *
 * The durable record is the server's — builds run for minutes and users close tabs. This page
 * renders the same outcome locally, off the `turn_ended` frame (`status`, `reason`, tri-state
 * `snapshotCommitted`), and writes nothing itself.
 *
 * A build IS its turn, so `turnId` is the identity a record is keyed by, and the test with teeth
 * here is that DEDUPE: after a reload the transcript already holds the server's row, and a
 * replayed terminal would stack a second copy on top of it. Both turn watchers on this page,
 * `fireRelayTurn` and `reattachToTurn`, call `showBuildOutcome` once their stream settles.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { FakeEventSource, makeClient, primeClient, waitForGateOpen, PREVIEW_URL, T_STEP, T_WORKSPACE, T_PREVIEW, T_BUILD_END } from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  relaunchPreview: vi.fn(), stop: vi.fn(), getStatus: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` is GONE — a chat's kind is fixed at creation, so there is no per-thread setting
// left to switch. `resolvePlanOptions` is a real export, kept mocked only because
// the surface reaches for it when a plan offer is answered — never exercised here, since this
// suite never renders an offer.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  // A build's Stop is the TURN stop now — there is no session-level stop left to reach for.
  stopTurn: (...a) => h.stopTurn(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import ConversationSurface from '../../components/chat/ConversationSurface'
import { OUTCOME_COPY, outcomeSummary } from '../../utils/messageTypes'

function renderThread(chatId = 'thread-1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<ConversationSurface projectId="p1" buildSessionDeps={deps} />} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

const composer = () => screen.getByPlaceholderText(/ask for another change/i)
async function send(text) {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/** The consolidating snapshot every subscribe gets FIRST (`backend/src/api/v1/conversations/turns.py` owns that rule),
 *  carrying the `turnId` this page reads into `liveTurnIdRef` AND `sink.turnId` — the fact the
 *  Stop test below depends on (Stop needs `liveTurnIdRef` populated WHILE the turn is still
 *  running, not only at its terminal). */
const T_SNAPSHOT = (turnId, seq = 1) => ({
  type: 'snapshot', seq, turnId, turnStatus: 'running', items: [], parts: [], working: false,
})

/**
 * Script an ordinary send's own turn stream as an OPEN socket a test can push frames into by
 * hand. Not `_builderSession.jsx`'s `scriptBuildTurn`, which branches on whether `readTurnStream`
 * was called WITH a `turnId`: `fireRelayTurn` never passes one, and never asks the chat's kind
 * either — every send on this page opens the one plain subscription, and that IS the build.
 */
function scriptTurn(turnId, opening) {
  const live = { emit: null, close: null }
  const frames = opening ?? [T_SNAPSHOT(turnId), T_WORKSPACE(undefined, 2)]
  const impl = async ({ onFrame }) => {
    live.emit = onFrame
    for (const frame of frames) onFrame(frame)
    return new Promise((resolve) => { live.close = resolve })
  }
  return {
    impl,
    /** Push more frames into the open turn (wrapped in act, so effects flush between). */
    frame: async (...more) => {
      await act(async () => { for (const frame of more) live.emit?.(frame) })
    },
    /** Close the socket. The TRANSPORT outcome only; the frames decide the semantic one. */
    end: async (outcome = 'completed') => {
      await act(async () => { live.close?.(outcome); await Promise.resolve() })
    },
  }
}

/**
 * Drive a build to running: an ordinary send opens the write turn directly — no plan text, no
 * card, no `Build it` press. `readTurnStream` having been called is what "the build is
 * underway" means now, and it is the socket every frame below is pushed into.
 */
async function runBuild(turn, text = 'a visitor app') {
  await send(text)
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
  await turn.frame(T_STEP('Scaffolding your app…'))
}

/**
 * THE OUTCOME AS A CITIZEN READS IT NOW — prose in the transcript, not a card.
 *
 * The summary sentence is the message's own TEXT (`outcomeSummary` on the surface,
 * `outcome.py::_summary` on the server — the two are written to match so a live render and a
 * reloaded row read identically), so the queries below match the SENTENCE rather than a test id:
 * the test id proved a box existed, this proves the citizen was told.
 */
/**
 * EXACT SENTENCES, NOT A LOOSE MATCHER.
 *
 * This used to be `/build finished\.|the build failed|the build stopped/i`, and the substring
 * `the build failed` is what let the Stop test below pass on the WRONG copy for as long as the
 * bug existed: a stopped build announced "The build failed: stopped_by_user", the matcher
 * shrugged, and the suite stayed green while the platform called a citizen's own deliberate
 * action a failure. Membership of the real copy table replaces it — a sentence either IS one the
 * table produces or it is not, and no near-miss squeaks through.
 *
 * The table is IMPORTED rather than retyped so this file cannot drift from the shipped copy; the
 * five reasons and their wording are asserted against explicitly in `the copy table` below, which
 * is where a silent edit to the source would be caught.
 */
const FINISHED = 'Build finished.'
const NEUTRAL_FAILED = 'The build failed.'
const NEUTRAL_STOPPED = 'This build was stopped before it finished.'
const EVERY_OUTCOME_SENTENCE = [FINISHED, NEUTRAL_FAILED, NEUTRAL_STOPPED, ...Object.values(OUTCOME_COPY)]
const saysAnOutcome = (text) => EVERY_OUTCOME_SENTENCE.some((sentence) => (text || '').includes(sentence))
const outcomeCards = () =>
  screen.queryAllByTestId('assistant-message').filter((m) => saysAnOutcome(m.textContent))
const findOutcome = async () => {
  await waitFor(() => expect(outcomeCards().length).toBeGreaterThan(0))
  return outcomeCards()[outcomeCards().length - 1]
}

/** Drive one ordinary send to a terminal and hand back the outcome message it produced. */
async function buildEndingWith(over) {
  const turn = scriptTurn('t1')
  h.readTurnStream.mockImplementation(turn.impl)
  renderThread()
  await runBuild(turn)
  await turn.frame(T_BUILD_END({ turnId: 't1', ...over }))
  await turn.end()
  return findOutcome()
}

/**
 * Everything the page actually PUT ON THE WIRE — one JSON string per send. The send path makes
 * exactly one server call, `startTurn`, and narrows the composer's parts through
 * `wireMessageFromParts` into `{text, attachmentTexts, attachmentIds}`, so a build part cannot
 * ride it by construction. Asserting on the serialized payload keeps the claim honest against
 * both ways it could stop being true: a parts-carrying body coming back, or an outcome sentence
 * written into `text`.
 */
const wireSends = () => h.startTurn.mock.calls.map((call) => JSON.stringify(call))

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.stopTurn.mockResolvedValue('stopping')
})
afterEach(cleanup)

describe('showing the outcome', () => {
  it('does NOT present a dead preview link on the ended-build card', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_PREVIEW()) // the url the record will carry
    // A STOPPED ending, not a completed one, and that is now load-bearing: a build that simply
    // finished no longer draws a card at all (the assistant's own closing paragraph already said
    // what it built). The guarantee here is about the CARD — that it never surfaces a preview URL
    // whose sandbox is gone — so it has to be driven by an ending that still produces one.
    await turn.frame(T_BUILD_END({ turnId: 't1', status: 'stopped' }))
    await turn.end()

    const card = await findOutcome()
    expect(card.textContent).toContain(NEUTRAL_STOPPED)
    // The per-build preview URL died with its sandbox the moment the build ended, so the
    // permanent record must never surface it as a working link. The guarantee is structural,
    // not conditional: no renderer for this link exists on the card any more.
    expect(card.querySelector(`a[href="${PREVIEW_URL}"]`)).toBeNull()
  })

  it('never writes the outcome itself — that is the server’s job', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // Stopped rather than completed, purely so there is a card to wait on — see the note in the
    // dead-preview-link test above. What is asserted below is about the WIRE, not the card.
    await turn.frame(T_PREVIEW(), T_BUILD_END({ turnId: 't1', status: 'stopped' }))
    await turn.end()
    await findOutcome()

    // Two writers would mean two records for one build (the server's row and this one), and the
    // server's is the one that survives a closed tab.
    //
    // LIVENESS FIRST, because this is an assert-absence test: zero sends would satisfy the
    // absence below while proving nothing, which is precisely how its predecessor passed for
    // as long as it existed.
    const sends = wireSends()
    expect(sends.length).toBeGreaterThan(0)
    for (const payload of sends) {
      expect(payload).not.toMatch(/"type"\s*:\s*"build"/)
      for (const sentence of EVERY_OUTCOME_SENTENCE) expect(payload).not.toContain(sentence)
    }
  })

  it('a genuine failure still reads as a failure — and still does not print its token', async () => {
    // THE OTHER HALF OF THAT FIX, and the one it could most easily have broken. Teaching the
    // surface that `stopped` is not a failure must not teach it that NOTHING is: a build that
    // really did fall over has to say so, or the fix has simply moved the lie.
    //
    // IT USED TO ASSERT THE REASON WAS PRINTED (`/tsc failed after 3 attempts/`), on a fixture
    // whose reason was prose. No producer emits prose: every `reason` on this wire is a
    // `_WriteEndedError` token or a session end reason (`self_heal_budget_exhausted`,
    // `wall_clock_deadline_exceeded`, `sandbox_unavailable`…), so that assertion was pinning a
    // shape the server cannot send while blessing the interpolation that printed tokens at
    // citizens. The real token is used here, and the assertion is inverted.
    const card = await buildEndingWith({ status: 'failed', reason: 'self_heal_budget_exhausted' })

    expect(card.textContent).toContain(NEUTRAL_FAILED)
    expect(card.textContent).not.toContain('self_heal_budget_exhausted')
    // The failure must not be dressed up as a stop by the widened status union.
    expect(card.textContent).not.toContain(NEUTRAL_STOPPED)
    expect(card.textContent).not.toContain(FINISHED)
  })

  it('a failure with no reason at all still reads as a failure', async () => {
    // The generic crash arm: `except Exception` never sets `end_reason`, so the frame carries a
    // null reason. The neutral sentence is the whole message here.
    const card = await buildEndingWith({ status: 'failed', reason: null })
    expect(card.textContent).toContain(NEUTRAL_FAILED)
  })

  it('warns when a build ran but its code was not saved', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_BUILD_END({ turnId: 't1', snapshotCommitted: false }))
    await turn.end()

    // A build that did not save is not a success: the next build will not start from it, and the
    // user has to know that before building on top of it. This lives in the banner slot above
    // the composer now (moved from the outcome card), and it survives a reload the same way.
    expect((await screen.findByTestId('turn-banner')).textContent).toMatch(/wasn’t saved/i)
  })

  it('a terminal that never reports the save does not claim the code was thrown away', async () => {
    // UNKNOWN IS NOT FALSE: `null`/absent means the terminal never spoke about the save, `false`
    // means the save ran and did not land. Saying nothing is the only honest render of the first,
    // and the server's durable row replaces this one on reload anyway.
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // Stopped, and silent about the snapshot. It has to be an ending that draws a card, because
    // the card IS the liveness anchor for the absence below — and a completed build no longer
    // draws one. The property under test is unchanged: a terminal that said nothing about the save
    // must not be rendered as one that threw the code away.
    await turn.frame(T_BUILD_END({ turnId: 't1', status: 'stopped' }))
    await turn.end()
    // LIVENESS FIRST: the outcome has to actually be on screen for the absence below to mean
    // anything — `queryByText(...).toBeNull()` also passes on a surface that rendered nothing.
    await findOutcome()

    expect(screen.queryByText(/wasn’t saved/i)).toBeNull()
  })

  it('a user Stop stops the TURN, and its terminal is still recorded', async () => {
    // A build has no session-level stop any more: one working indicator, one way to interrupt it,
    // and it is the same `stopTurn` an ordinary reply uses. The stop is a REQUEST — the terminal
    // still arrives as a frame, and it is that frame the record is written from.
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // `stop-turn` is the RELOCATED control on the composer, not the one inside the build card.
    // Both are on screen for now and both stop the same turn the same way; this one is addressed
    // by test id because it is the one that survives the card's deletion, so this assertion keeps
    // meaning the same thing afterwards.
    fireEvent.click(await screen.findByTestId('stop-turn'))
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith('thread-1', 't1'))
    expect(h.stop).not.toHaveBeenCalled() // never a session-level stop

    await turn.frame(T_BUILD_END({ turnId: 't1', status: 'stopped', reason: 'stopped_by_user' }))
    await turn.end('completed')

    // THE ASSERTION THIS TEST USED TO MAKE WAS `expect(await findOutcome()).toBeTruthy()`, and it
    // could not fail: `findOutcome`'s old matcher accepted `the build failed`, which is precisely
    // the sentence the bug produced. The exact copy is asserted now, and the two things that bug
    // let through are rejected by name — the word "failed", and the raw token.
    const card = await findOutcome()
    expect(card.textContent).toContain(OUTCOME_COPY.stopped_by_user)
    expect(card.textContent).not.toMatch(/failed/i)
    expect(card.textContent).not.toContain('stopped_by_user')
  })

  it('the activity pill and the outcome sentence say the same true thing', async () => {
    // The contradiction this reproduces, exactly as it reached a citizen's screen: the pill read
    // "1 step · stopped before it finished" while the sentence directly beneath it read "The
    // build failed: stopped_by_user". One view, one build, two answers. Asserted TOGETHER on one
    // screen, because each half passing on its own is exactly the state the bug shipped in.
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn) // pushes the one step the pill counts
    // Settle it. A step left `pending` converts to `running`, and a RUNNING group reports only
    // its count — "a count of problems while the run is still going describes something that may
    // yet be recovered from" (ActivityGroup's own rule). The citizen presses Stop between two
    // steps, not mid-write, so the sealed group is the shape this contradiction actually appears
    // in — and it is the only shape where the pill has a verdict to contradict.
    await turn.frame(T_STEP('Scaffolding your app…', { state: 'ok' }))

    await turn.frame(T_BUILD_END({ turnId: 't1', status: 'stopped', reason: 'stopped_by_user' }))
    await turn.end('completed')

    const card = await findOutcome()
    const pill = await screen.findByTestId('activity-group-trigger')
    expect(pill.textContent).toContain('stopped before it finished')
    expect(card.textContent).toContain(OUTCOME_COPY.stopped_by_user)
    // Neither half may call it a failure. The pill never did; the sentence is what changed.
    expect(pill.textContent).not.toMatch(/failed/i)
    expect(card.textContent).not.toMatch(/failed/i)
  })

  it('still warns when the terminal explicitly says the snapshot did not commit', async () => {
    // The other half of the tri-state: `false` from the server is a real answer and must keep
    // warning. Only the ABSENCE of an answer is what stops being read as one.
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_BUILD_END({ turnId: 't1', snapshotCommitted: false }))
    await turn.end()

    expect((await screen.findByTestId('turn-banner')).textContent).toMatch(/wasn’t saved/i)
  })

  it('shows nothing while the build is still running', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_PREVIEW())

    // LIVENESS: the pane's cover is driven by `turnPhase` off the frames, and this harness mounts
    // no pane at all. What the absence below needs is proof the build is genuinely still running,
    // and the composer's stop control is present for exactly and only that.
    await waitFor(() => expect(screen.getByTestId('stop-turn')).toBeTruthy())
    expect(outcomeCards()).toHaveLength(0)
  })
})

/**
 * THE REASON → COPY TABLE.
 *
 * The sentences are TYPED OUT HERE rather than read from the source, on purpose: importing them
 * and comparing them to themselves would pass whatever the source said, which is not a test of
 * copy. This suite is the thing that goes red when somebody edits a citizen-facing sentence, so
 * the edit has to be deliberate.
 *
 * `outcomeSummary` is exercised directly for the table because two of the five reasons —
 * `force_ended` and `idle_teardown` — reach the browser on the LEGACY session path (the `ended`
 * envelope) rather than on a turn terminal, and inventing a `turn_ended` frame carrying them would
 * pin a wire shape the server cannot produce. The three that DO ride a turn terminal are driven
 * through the real surface below, which is what proves the table is actually wired to the screen.
 */
describe('the copy table', () => {
  const TABLE = [
    ['quota_exceeded', 'failed', 'The build stopped: you reached your daily limit.'],
    ['stopped_by_user', 'stopped', 'You stopped this build before it finished.'],
    ['force_ended', 'ended', 'This build was force-stopped before it finished, and its work was discarded.'],
    ['idle_teardown', 'ended', 'This build was stopped because it sat idle.'],
    [
      'workspace_restored',
      'failed',
      'This build stopped so your workspace could be put back from the last saved copy. Send your message again once your workspace is back.',
    ],
    // The turn engine's own bounded endings (2026-09-11): one sentence for the two internal
    // ceilings, and a named ending for a model service that stayed down past every retry.
    ['request_limit', 'failed', 'This build stopped after doing as much as it does in one go.'],
    [
      'wall_clock_deadline_exceeded',
      'failed',
      'This build stopped after doing as much as it does in one go.',
    ],
    [
      'model_unavailable',
      'failed',
      'The assistant could not get an answer from its service, so this build stopped.',
    ],
  ]

  it.each(TABLE)('%s says its own sentence, and says it whatever status carries it', (reason, status, sentence) => {
    expect(OUTCOME_COPY[reason]).toBe(sentence)
    expect(outcomeSummary({ status, reason })).toBe(sentence)
    // THE REASON BEATS THE STATUS, which is the one ordering difference from the server's own
    // table and the reason the bug existed. `_WriteEndedError` finishes as `failed` for every
    // named graceful end there is, so answering the status first is exactly what printed
    // "The build failed: quota_exceeded" at someone who had merely used up their day.
    expect(outcomeSummary({ status: 'failed', reason })).toBe(sentence)
  })

  it('names every arm the server names, and the one it does not', () => {
    // Mirrors `outcome.py::_summary`'s four reasons plus `workspace_restored`, plus the turn
    // engine's three bounded endings (`request_limit`, `wall_clock_deadline_exceeded`,
    // `model_unavailable` — none has a legacy row). An arm appearing in `OUTCOME_COPY` without a
    // row in this table is the drift this pins.
    expect(Object.keys(OUTCOME_COPY).sort()).toEqual(TABLE.map(([reason]) => reason).sort())
  })

  it('an unknown reason gets the neutral fallback and never the token itself', () => {
    const TOKEN = 'reaped_by_the_kraken'
    for (const [status, fallback] of [
      ['failed', NEUTRAL_FAILED],
      ['stopped', NEUTRAL_STOPPED],
      ['ended', FINISHED],
    ]) {
      const line = outcomeSummary({ status, reason: TOKEN })
      // Presence first: `not.toContain` on an empty string passes and proves nothing.
      expect(line).toBe(fallback)
      expect(line).not.toContain(TOKEN)
    }
  })
})

describe('the table, on the screen', () => {
  it('a workspace restore is not announced as a failure', async () => {
    // The turn ends `failed` here because that is genuinely what `_WriteEndedError` finishes as —
    // and the restore SUCCEEDED. This is the exact frame that once rendered as
    // "The build failed: workspace_restored" after the platform had just saved the citizen's app.
    const card = await buildEndingWith({ status: 'failed', reason: 'workspace_restored' })

    expect(card.textContent).toContain(OUTCOME_COPY.workspace_restored)
    expect(card.textContent).not.toMatch(/failed/i)
    expect(card.textContent).not.toContain('workspace_restored')
  })

  it('a spent daily limit is not announced as a failure', async () => {
    const card = await buildEndingWith({ status: 'failed', reason: 'quota_exceeded' })

    expect(card.textContent).toContain(OUTCOME_COPY.quota_exceeded)
    expect(card.textContent).not.toMatch(/failed/i)
    expect(card.textContent).not.toContain('quota_exceeded')
  })

  it('an unknown reason reaches the transcript as the fallback, with no token in it', async () => {
    // THIS IS ALSO THE TEST THAT GUARDS `announceTerminal`'S COLLAPSE, and it is the only one
    // that can be. Restore `: 'failed'` there and every NAMED reason still reads correctly — the
    // table is consulted before the status, so `stopped_by_user` produces its sentence either way.
    // The stop whose reason this client does not recognise is the one case where the terminal
    // itself is the only thing left saying what happened, so it is the case that goes red.
    const card = await buildEndingWith({ status: 'stopped', reason: 'reaped_by_the_kraken' })

    expect(card.textContent).toContain(NEUTRAL_STOPPED)
    expect(card.textContent).not.toContain('reaped_by_the_kraken')
    expect(card.textContent).not.toMatch(/failed/i)
  })
})

/**
 * THE THIRD COLLAPSE SITE, not named by the fix above.
 *
 * The fix looks like two lines. It is three: the reload path has its own fold, in
 * `conversationApi`'s `banner` projection, and it folds the other way — a stopped build came back
 * from a reload as `ended`, i.e. as a build that finished normally, sitting directly beneath the
 * server's own stored sentence saying the citizen stopped it. Fixing only the live path would have
 * left the contradiction intact for anyone who reloaded, which is everyone who comes back
 * tomorrow.
 *
 * The real module is reached through `importActual` because this file mocks `conversationApi`
 * wholesale for the surface's own conversation-list read.
 */
describe('the stored banner, on reload', () => {
  const buildPartFor = async (banner) => {
    const { messagesFromProjection } = await vi.importActual('../../utils/conversationApi')
    const [message] = messagesFromProjection([
      { type: 'banner', seq: 4, banner, text: 'the stored sentence', sessionId: 's1', previewUrl: null },
    ])
    return message.parts.find((p) => p.type === 'build')
  }

  // `projection.py::_banner_kind`'s whole vocabulary. `quota` pairs with `stopped` for the same
  // reason the live path treats it as one: nothing broke, the day ran out.
  it.each([
    ['completed', 'ended'],
    ['failed', 'failed'],
    ['stopped', 'stopped'],
    ['quota', 'stopped'],
  ])('a %s banner comes back as %s', async (banner, status) => {
    const part = await buildPartFor(banner)
    expect(part).toBeTruthy() // presence, so the status read below cannot be vacuous
    expect(part.status).toBe(status)
  })

  it('an unrecognised banner degrades to ended rather than inventing a failure', async () => {
    // A client behind its server is a deployment order, not a broken build.
    const part = await buildPartFor('a_kind_this_client_has_never_heard_of')
    expect(part.status).toBe('ended')
  })
})

// (The 'seq follows the server' suite is retired: the client persists nothing, so
// there is no seq to negotiate — the server owns transcript ordering outright.)

describe('dedupe on the build TURN', () => {
  it('does not double-show a replayed terminal frame', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // A resubscribe (resume-once on a dropped socket) re-delivers the terminal it already saw.
    await turn.frame(
      T_BUILD_END({ turnId: 't1', status: 'stopped' }),
      T_BUILD_END({ turnId: 't1', status: 'stopped' }),
    )
    await turn.end()

    // Counted in CARDS, so the ending must be one that draws them.
    await findOutcome()
    await waitFor(() => expect(outcomeCards()).toHaveLength(1))
  })

  it('does not re-show after a reload, where the server’s row is already in the transcript', async () => {
    // The case an `_id`/seq guard cannot catch: both are fresh after a reload, so only matching on
    // the BUILD TURN tells us this outcome is already recorded.
    //
    // Driven through the REATTACH path (`activeTurn`), not a second ordinary send — an ordinary
    // send always mints a brand-new turn id, so it can never reproduce the one case this guard
    // exists for: the read projection still names `t1` as the live turn (a race — the server had
    // not yet cleared it when this GET ran), the transcript ALREADY holds `t1`'s persisted row,
    // and the reattach's own stream then reports the very same turn ending again.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      activeTurn: { turnId: 't1', lastSeq: 5 },
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'a visitor app' }], seq: 0 },
        {
          id: 'm1',
          role: 'assistant',
          seq: 1,
          parts: [
            { type: 'text', text: 'Build finished.' },
            { type: 'build', status: 'ended', turnId: 't1', previewUrl: PREVIEW_URL },
          ],
        },
      ],
    })
    const turn = scriptTurn('t1', [])
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    // The stored row renders immediately from the seeded transcript, before the reattach's
    // stream says anything at all. It is the message's TEXT part that carries the sentence — the
    // `build` part beside it maps to no rendered element, which is exactly why the fixture's two
    // parts still produce one readable outcome.
    await findOutcome()

    await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
    await turn.frame(T_BUILD_END({ turnId: 't1' }))
    await turn.end()

    expect(outcomeCards()).toHaveLength(1)
  })

  it('a build that simply FINISHED draws no card — the assistant already said what it built', async () => {
    // ★★ THE NEW CONTRACT, and the reason five tests above now drive a stopped ending.
    //
    // Every turn in a Build chat writes a terminal, so the neutral "Build finished." landed after
    // every single exchange — under an assistant message that had just described the same build in
    // its own words, and carrying a second copy button of its own. The reload path in
    // `conversationApi.ts` had always withheld it and said so at length; the live path did not, so
    // the same build read one way as it happened and another way after a refresh.
    //
    // LIVENESS FIRST: the build genuinely ran and genuinely ended. Without that, the absence below
    // would pass on a turn that never started — the false-green this file guards against elsewhere.
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)
    await turn.frame(T_PREVIEW(), T_BUILD_END({ turnId: 't1' }))
    await turn.end()

    await waitFor(() => expect(wireSends().length).toBeGreaterThan(0))
    // …and nothing anywhere on the surface says it finished.
    await waitFor(() => expect(outcomeCards()).toHaveLength(0))
    expect(screen.queryByText(FINISHED)).toBeNull()
  })

  it('shows a SECOND build separately — dedupe is per build turn, not per thread', async () => {
    const first = scriptTurn('t1')
    h.readTurnStream.mockImplementation(first.impl)
    renderThread()
    await runBuild(first)
    await first.frame(T_BUILD_END({ turnId: 't1', status: 'stopped' }))
    await first.end()
    await findOutcome()

    // An iteration is a NEW turn, and its outcome is its own record — the whole reason the record
    // is keyed by the build rather than by the thread.
    const second = scriptTurn('t2')
    h.readTurnStream.mockImplementation(second.impl)
    await runBuild(second, 'add a chart')
    await second.frame(T_BUILD_END({ turnId: 't2', status: 'stopped' }))
    await second.end()

    await waitFor(() => expect(outcomeCards()).toHaveLength(2))
  })
})
