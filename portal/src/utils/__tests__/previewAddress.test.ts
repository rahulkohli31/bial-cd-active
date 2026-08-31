/**
 * The preview address resolver (Plan A, U2).
 *
 * The whole precedence, every arm violated independently, and above all the ASYMMETRIC cell: the
 * project predicate false while the turn arm still frames. A resolver that "tidied" the two
 * predicates into one is caught there and nowhere else — every other cell in the table passes
 * under the merged version.
 *
 * The page-level counterpart is `pages/__tests__/BuilderPage-previewaddress.test.tsx`, which pins
 * the same asymmetry through the real iframe. This file is where the combinations live, because
 * driving sixteen of them through a page would be sixteen builds.
 */
import { describe, it, expect } from 'vitest'
import { resolvePreviewAddress, type PreviewAddressInputs } from '../previewAddress'

const TURN = 'https://turn.example.azurecontainerapps.io/'
const RELAUNCH = 'https://relaunch.example.azurecontainerapps.io/'
const SESSION = 'https://session.example.azurecontainerapps.io/'
const PROJECT = 'https://project.example.azurecontainerapps.io/'

/** Nothing qualifies. Every scenario states only the inputs it is actually about. */
const nothing: PreviewAddressInputs = {
  turnPreviewUrl: null,
  turnStatus: null,
  narratingChatIsOpenChat: false,
  relaunchedUrl: null,
  sessionUrl: null,
  sessionStatus: null,
  sessionId: null,
  projectPreviewUrl: null,
  sessionBelongsToOpenProject: false,
  transcriptHasBuildOutcome: false,
}

const resolve = (over: Partial<PreviewAddressInputs>) =>
  resolvePreviewAddress({ ...nothing, ...over })

/** All four sources populated at once, both predicates true — precedence tests narrow from here. */
const everything: Partial<PreviewAddressInputs> = {
  turnPreviewUrl: TURN,
  narratingChatIsOpenChat: true,
  relaunchedUrl: RELAUNCH,
  sessionUrl: SESSION,
  sessionStatus: 'ready',
  sessionId: 'sess-1',
  projectPreviewUrl: PROJECT,
  sessionBelongsToOpenProject: true,
}

describe('resolvePreviewAddress — the precedence', () => {
  it('a live turn preview outranks all three arms below it', () => {
    expect(resolve(everything).url).toBe(TURN)
  })

  it('a relaunched URL outranks the session URL and the project preview', () => {
    expect(resolve({ ...everything, turnPreviewUrl: null }).url).toBe(RELAUNCH)
  })

  it('the session URL outranks the project preview', () => {
    expect(resolve({ ...everything, turnPreviewUrl: null, relaunchedUrl: null }).url).toBe(SESSION)
  })

  it('the project preview resolves last, when nothing above it qualifies', () => {
    // Plan F's first screen: arrive at a project, see the app. There is no chat here at all, so
    // the three arms above are structurally unavailable — this arm is the only one that can
    // answer, and without it the project screen frames nothing.
    expect(
      resolve({
        projectPreviewUrl: PROJECT,
        sessionBelongsToOpenProject: true,
      }),
    ).toEqual({ url: PROJECT, status: 'ready' })
  })

  it('every source null resolves to nothing framed, and to a status that does not claim an ending', () => {
    expect(resolve({})).toEqual({ url: null, status: null })
  })
})

describe('resolvePreviewAddress — the chat predicate gates the turn arm, and nothing else', () => {
  it('a false chat predicate drops the turn arm even though its URL is non-null', () => {
    expect(resolve({ ...everything, narratingChatIsOpenChat: false }).url).toBe(RELAUNCH)
  })

  it('a false chat predicate does NOT disturb the three project-scoped arms', () => {
    // The sibling-chat case: another conversation in this project is mid-build. Its preview is not
    // this chat's, but the project's own relaunched app is.
    const { url } = resolve({
      turnPreviewUrl: TURN,
      narratingChatIsOpenChat: false,
      relaunchedUrl: RELAUNCH,
      sessionBelongsToOpenProject: true,
    })
    expect(url).toBe(RELAUNCH)
  })
})

describe('resolvePreviewAddress — the project predicate gates the three lower arms, and nothing else', () => {
  it('a false project predicate drops the relaunched URL, the session URL and the project preview', () => {
    expect(
      resolve({
        relaunchedUrl: RELAUNCH,
        sessionUrl: SESSION,
        sessionId: 'sess-1',
        projectPreviewUrl: PROJECT,
        sessionBelongsToOpenProject: false,
      }).url,
    ).toBeNull()
  })

  it('the same project preview URL for a DIFFERENT project does not resolve', () => {
    // The fourth arm is gated exactly as the two above it are. Without this it would be the one
    // arm that could frame another project's app, and it is the arm with no chat behind it to
    // make the mistake visible.
    expect(resolve({ projectPreviewUrl: PROJECT, sessionBelongsToOpenProject: false }).url).toBeNull()
  })

  it('BOTH predicates false resolves to nothing at all', () => {
    expect(
      resolve({
        ...everything,
        narratingChatIsOpenChat: false,
        sessionBelongsToOpenProject: false,
      }),
    ).toEqual({ url: null, status: null })
  })

  it('THE ASYMMETRY: the project predicate is false and the turn arm still wins', () => {
    // The one cell that fails under a resolver which merged the two predicates, and passes under
    // every other simplification. A citizen watching their build in a chat whose project this page
    // never stamped a session with must still see their app.
    expect(
      resolve({
        ...everything,
        sessionBelongsToOpenProject: false,
      }),
    ).toEqual({ url: TURN, status: null })
  })
})

describe('resolvePreviewAddress — a session id is not the project predicate', () => {
  it('no session id but a matching project: the relaunch arm resolves, the session arm does not', () => {
    // The "come back later" case, and the distinction a naive merge of the two lower gates loses.
    // There is no session at all — the tab was reloaded, or this chat never built anything — and a
    // relaunch is precisely what restores an app in that state.
    expect(
      resolve({
        relaunchedUrl: RELAUNCH,
        sessionUrl: SESSION,
        sessionStatus: 'ready',
        sessionId: null,
        sessionBelongsToOpenProject: true,
      }),
    ).toEqual({ url: RELAUNCH, status: 'ready' })
  })

  it('no session id and no relaunch: the session URL and its status are both withheld', () => {
    expect(
      resolve({
        sessionUrl: SESSION,
        sessionStatus: 'ended',
        sessionId: null,
        sessionBelongsToOpenProject: true,
      }),
    ).toEqual({ url: null, status: null })
  })
})

describe('resolvePreviewAddress — the status is resolved independently of the URL', () => {
  it('a running turn has a status before it has a URL — the loading state', () => {
    // Tying the status to whichever arm won the URL would collapse this into an empty pane, and
    // the citizen would watch nothing happen for the length of a provision.
    expect(
      resolve({ turnStatus: 'provisioning', narratingChatIsOpenChat: true }),
    ).toEqual({ url: null, status: 'provisioning' })
  })

  it('the live turn\'s status outranks every lower source', () => {
    expect(resolve({ ...everything, turnStatus: 'building' }).status).toBe('building')
  })

  it('the turn\'s status carries the chat predicate too — a sibling chat\'s build says nothing here', () => {
    // The caller today hands this in already gated, so this asserts the module does not DEPEND on
    // that. Without it, the one thing keeping a sibling conversation's "building…" off this pane
    // would be a derivation order two files away.
    expect(
      resolve({
        turnStatus: 'building',
        narratingChatIsOpenChat: false,
        sessionUrl: SESSION,
        sessionStatus: 'ended',
        sessionId: 'sess-1',
        sessionBelongsToOpenProject: true,
      }),
    ).toEqual({ url: SESSION, status: 'ended' })
  })

  it('a relaunched URL resolves the status to ready — it is a restore, not a build', () => {
    // A relaunch has no lifecycle: no feed, no keep-alive, no lock. Reading the ENDED session's
    // status here is what left "the preview is no longer running" painted over a restored app.
    expect(
      resolve({
        relaunchedUrl: RELAUNCH,
        sessionUrl: SESSION,
        sessionStatus: 'ended',
        sessionId: 'sess-1',
        sessionBelongsToOpenProject: true,
      }),
    ).toEqual({ url: RELAUNCH, status: 'ready' })
  })

  it('an ended session keeps its terminal status even when the project read says a container is up', () => {
    // Deliberately NOT overridden by the fourth arm. The session's own terminal is the more
    // recent, more specific fact about what this tab was watching; letting a background read
    // overrule it would resurrect a build the user watched end.
    expect(
      resolve({
        sessionStatus: 'ended',
        sessionId: 'sess-1',
        projectPreviewUrl: PROJECT,
        sessionBelongsToOpenProject: true,
      }),
    ).toEqual({ url: PROJECT, status: 'ended' })
  })

  it('a transcript with a finished build is the bottom of the status precedence, and contributes no URL', () => {
    // A reloaded tab: no live anything, but a build once ran here. The terminal placeholder and
    // its Relaunch, rather than the idle empty state. The outcome's own URL is never framed — it
    // names a container that is long gone.
    expect(
      resolve({ transcriptHasBuildOutcome: true }),
    ).toEqual({ url: null, status: 'ended' })
  })

  it('a transcript outcome does not overrule a live session that is still building', () => {
    expect(
      resolve({
        sessionUrl: SESSION,
        sessionStatus: 'building',
        sessionId: 'sess-1',
        sessionBelongsToOpenProject: true,
        transcriptHasBuildOutcome: true,
      }),
    ).toEqual({ url: SESSION, status: 'building' })
  })

  it('an out-of-scope session contributes neither its URL nor its status', () => {
    // Both halves, together: the status is gated by the same `sessionId` + project pair the URL is.
    // Leaking the status alone would have another project's ended build render this project's
    // terminal placeholder.
    expect(
      resolve({
        sessionUrl: SESSION,
        sessionStatus: 'ended',
        sessionId: 'sess-1',
        sessionBelongsToOpenProject: false,
        transcriptHasBuildOutcome: false,
      }),
    ).toEqual({ url: null, status: null })
  })
})
