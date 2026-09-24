/**
 * The preview address resolver.
 *
 * The whole precedence, every arm violated independently, and above all the ASYMMETRIC cell: the
 * project predicate false while the turn arm still frames. A resolver that "tidied" the two
 * predicates into one is caught there and nowhere else — every other cell in the table passes
 * under the merged version.
 *
 * The page-level counterpart is `pages/__tests__/ConversationSurface-previewaddress.test.tsx`, which pins
 * the same asymmetry through the real iframe. This file is where the combinations live, because
 * driving sixteen of them through a page would be sixteen builds.
 */
import { describe, it, expect } from 'vitest'
import { resolvePreviewAddress, type PreviewAddressInputs } from '../previewAddress'

const TURN = 'https://turn.example.azurecontainerapps.io/'
const RELAUNCH = 'https://relaunch.example.azurecontainerapps.io/'
const PROJECT = 'https://project.example.azurecontainerapps.io/'

/** Nothing qualifies. Every scenario states only the inputs it is actually about. */
const nothing: PreviewAddressInputs = {
  turnPreviewUrl: null,
  turnStatus: null,
  narratingChatIsOpenChat: false,
  relaunchedUrl: null,
  projectPreviewUrl: null,
  belongsToOpenProject: false,
  transcriptHasBuildOutcome: false,
}

const resolve = (over: Partial<PreviewAddressInputs>) =>
  resolvePreviewAddress({ ...nothing, ...over })

/** All three sources populated at once, both predicates true — precedence tests narrow from here. */
const everything: Partial<PreviewAddressInputs> = {
  turnPreviewUrl: TURN,
  narratingChatIsOpenChat: true,
  relaunchedUrl: RELAUNCH,
  projectPreviewUrl: PROJECT,
  belongsToOpenProject: true,
}

describe('resolvePreviewAddress — the precedence', () => {
  it('a live turn preview outranks both arms below it', () => {
    expect(resolve(everything).url).toBe(TURN)
  })

  it('a relaunched URL outranks the project preview', () => {
    expect(resolve({ ...everything, turnPreviewUrl: null }).url).toBe(RELAUNCH)
  })

  it('the project preview resolves last, when nothing above it qualifies', () => {
    // The first screen: arrive at a project, see the app. There is no chat here at all, so
    // the two arms above are structurally unavailable — this arm is the only one that can
    // answer, and without it the project screen frames nothing.
    expect(
      resolve({
        projectPreviewUrl: PROJECT,
        belongsToOpenProject: true,
      }),
    ).toEqual({ url: PROJECT, status: 'ready', serving: true })
  })

  it('every source null resolves to nothing framed, and to a status that does not claim an ending', () => {
    expect(resolve({})).toEqual({ url: null, status: null, serving: false })
  })
})

describe('resolvePreviewAddress — the chat predicate gates the turn arm, and nothing else', () => {
  it('a false chat predicate drops the turn arm even though its URL is non-null', () => {
    expect(resolve({ ...everything, narratingChatIsOpenChat: false }).url).toBe(RELAUNCH)
  })

  it('a false chat predicate does NOT disturb the two project-scoped arms', () => {
    // The sibling-chat case: another conversation in this project is mid-build. Its preview is not
    // this chat's, but the project's own relaunched app is.
    const { url } = resolve({
      turnPreviewUrl: TURN,
      narratingChatIsOpenChat: false,
      relaunchedUrl: RELAUNCH,
      belongsToOpenProject: true,
    })
    expect(url).toBe(RELAUNCH)
  })
})

describe('resolvePreviewAddress — the project predicate gates the two lower arms, and nothing else', () => {
  it('a false project predicate drops the relaunched URL and the project preview', () => {
    expect(
      resolve({
        relaunchedUrl: RELAUNCH,
        projectPreviewUrl: PROJECT,
        belongsToOpenProject: false,
      }).url,
    ).toBeNull()
  })

  it('the same project preview URL for a DIFFERENT project does not resolve', () => {
    // The project arm is gated exactly as the relaunch arm above it is. Without this it would be
    // the one arm that could frame another project's app, and it is the arm with no chat behind it
    // to make the mistake visible.
    expect(resolve({ projectPreviewUrl: PROJECT, belongsToOpenProject: false }).url).toBeNull()
  })

  it('BOTH predicates false resolves to nothing at all', () => {
    expect(
      resolve({
        ...everything,
        narratingChatIsOpenChat: false,
        belongsToOpenProject: false,
      }),
    ).toEqual({ url: null, status: null, serving: false })
  })

  it('THE ASYMMETRY: the project predicate is false and the turn arm still wins', () => {
    // The one cell that fails under a resolver which merged the two predicates, and passes under
    // every other simplification. A citizen watching their build in a chat whose project this page
    // never stamped must still see their app.
    expect(
      resolve({
        ...everything,
        belongsToOpenProject: false,
      }),
    // …and it is SERVING, for the same reason it frames: the turn arm is chat-scoped, so a chat
    // whose project this page never stamped is still watching its own app run.
    ).toEqual({ url: TURN, status: null, serving: true })
  })
})

describe('resolvePreviewAddress — liveness, which is a THIRD question', () => {
  // WHAT THIS ANSWERS, AND WHAT IT MUST NOT. `serving` says a container is still answering at the
  // framed address, which is what lets a terminal status keep its frame. It says NOTHING about
  // whether the newest build compiled — that is the compile state's job, and conflating the two is
  // what put "Build complete" on a screen where no build ever runs.

  it('★ a turn that reset its narrative on a SEND stays serving', () => {
    // THE REMOUNT-ON-EVERY-MESSAGE DEFECT, at the resolver. A send clears the turn narrative, so
    // `turnStatus` drops to `null` while `turnPreviewUrl` keeps the URL the last `preview_ready`
    // named. Requiring a terminal phase here blinks liveness off for exactly that render — and the
    // status, falling through to the transcript's own `'ended'`, then collapses the frame and
    // remounts the iframe, throwing away everything the citizen had typed into their own app.
    //
    // Mutation check: narrow the turn arm to `turnStatus === 'ended'` and this is the only
    // scenario in this file that goes red.
    expect(
      resolve({
        turnPreviewUrl: TURN,
        turnStatus: null,
        narratingChatIsOpenChat: true,
        transcriptHasBuildOutcome: true,
      }),
    ).toEqual({ url: TURN, status: 'ended', serving: true })
  })

  it('★ a STOPPED turn is serving, exactly as a completed one is', () => {
    // THE STOP/COMPLETE EQUIVALENCE, at the resolver. A stop and a completion both leave the
    // phase at `ended` (`turnNarrative.turnPhase`) because the backend pardons the container
    // either way, so both resolve to a serving address and the pane keeps framing the app.
    expect(
      resolve({ turnPreviewUrl: TURN, turnStatus: 'ended', narratingChatIsOpenChat: true }),
    ).toEqual({ url: TURN, status: 'ended', serving: true })
  })

  it('★ a FAILED turn is not (its liveness is not widened as a side effect)', () => {
    // THE LINE THE WIDENING MUST NOT CROSS: "alive" and "worth framing" stay two questions. A
    // turn that genuinely failed — or that lost its workspace, which is the other way
    // `turnPhase` reaches `failed` — asserts nothing.
    //
    // Mutation check: relax the arm to `turnStatus !== null` and this goes red while every other
    // scenario in this block stays green.
    expect(
      resolve({ turnPreviewUrl: TURN, turnStatus: 'failed', narratingChatIsOpenChat: true }),
    ).toEqual({ url: TURN, status: 'failed', serving: false })
  })

  it('★ a live container makes a FAILED turn serving again — the read outranks the reason string', () => {
    // The pairing that keeps the previous scenario from being read as "failed means gone". The
    // turn's terminal reason is not evidence about the container; the preview-state read is. When
    // the read says a container is up, the frame stays — and what the pane may SAY about the failed
    // build is still the compile state's answer, not this one's.
    expect(
      resolve({
        turnPreviewUrl: TURN,
        turnStatus: 'failed',
        narratingChatIsOpenChat: true,
        projectPreviewUrl: PROJECT,
        belongsToOpenProject: true,
      }),
    ).toEqual({ url: TURN, status: 'failed', serving: true })
  })

  it('the project read answers liveness even when a higher arm won the URL — one app, one container', () => {
    expect(
      resolve({
        turnPreviewUrl: TURN,
        narratingChatIsOpenChat: true,
        projectPreviewUrl: PROJECT,
        belongsToOpenProject: true,
      }).serving,
    ).toBe(true)
  })

  it('and the project read carries the PROJECT PREDICATE — a sibling project\'s container claims nothing', () => {
    // The liveness half of the asymmetry the URL arms already pin, ISOLATED so only the project
    // read could answer: the turn arm is held at `failed`, which asserts nothing on its own. A
    // resolver that reached past `fromProject` to a raw "something is alive" flag would let one
    // project's running container hold another project's frame open.
    const failedTurnOverA = {
      turnPreviewUrl: TURN,
      turnStatus: 'failed' as const,
      narratingChatIsOpenChat: true,
      projectPreviewUrl: PROJECT,
    }
    expect(resolve({ ...failedTurnOverA, belongsToOpenProject: false }).serving).toBe(false)
    // …and the same inputs with the predicate TRUE do answer, so the assertion above is a gate
    // rather than an input nobody read.
    expect(resolve({ ...failedTurnOverA, belongsToOpenProject: true }).serving).toBe(true)
  })

  it('nothing framed is never "serving" — liveness describes an address, not a mood', () => {
    // With no URL the question is not "false because the app is down", it is not a question at all,
    // and `false` is the answer that leaves every reader behaving as it did before this field.
    expect(resolve({ turnStatus: 'ended', narratingChatIsOpenChat: true }).serving).toBe(false)
  })
})

describe('resolvePreviewAddress — the status is resolved independently of the URL', () => {
  it('a running turn has a status before it has a URL — the loading state', () => {
    // Tying the status to whichever arm won the URL would collapse this into an empty pane, and
    // the citizen would watch nothing happen for the length of a provision.
    expect(
      resolve({ turnStatus: 'provisioning', narratingChatIsOpenChat: true }),
    ).toEqual({ url: null, status: 'provisioning', serving: false })
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
        relaunchedUrl: RELAUNCH,
        belongsToOpenProject: true,
      }),
    ).toEqual({ url: RELAUNCH, status: 'ready', serving: false })
  })

  it('a relaunched URL resolves the status to ready — it is a restore, not a build', () => {
    // A relaunch has no lifecycle: no feed, no keep-alive, no lock. The transcript's own `ended`
    // sits below it, so a restored app is never painted "no longer running".
    expect(
      resolve({
        relaunchedUrl: RELAUNCH,
        belongsToOpenProject: true,
        transcriptHasBuildOutcome: true,
      }),
    ).toEqual({ url: RELAUNCH, status: 'ready', serving: false })
  })

  it('a container the project read finds up outranks the transcript\'s ended build', () => {
    // A chat whose last build ended, opened while the project's app is running: the read says the
    // app is there, and a transcript that ended must not declare the preview gone over it.
    expect(
      resolve({
        projectPreviewUrl: PROJECT,
        belongsToOpenProject: true,
        transcriptHasBuildOutcome: true,
      }),
    ).toEqual({ url: PROJECT, status: 'ready', serving: true })
  })

  it('a transcript with a finished build is the bottom of the status precedence, and contributes no URL', () => {
    // A reloaded tab: no live anything, but a build once ran here. The terminal placeholder and
    // its Relaunch, rather than the idle empty state. The outcome's own URL is never framed — it
    // names a container that is long gone.
    expect(
      resolve({ transcriptHasBuildOutcome: true }),
    ).toEqual({ url: null, status: 'ended', serving: false })
  })
})
