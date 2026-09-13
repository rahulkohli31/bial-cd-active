import { useState, useEffect, useRef, useCallback } from 'react'
import type { ReactNode } from 'react'
import { RotateCcw } from 'lucide-react'
import { BusyGlyph } from './ui/Waiting'
import type { BuildSessionStatus } from '../utils/buildSessionTypes'
import type { PreviewLifeState } from '../utils/buildSessionApi'
import type { CompileState } from '../utils/compileState'
import { isRecord } from '../utils/apiError'

// Device-card widths drive the preview's REAL rendered pixel width (an inline style on
// the wrapper, not a Tailwind max-width class) so the framed cross-origin doc's own media
// queries evaluate against the TRUE viewport width — the actual fix for "doesn't look like
// a real phone". The iframe itself stays `w-full` (100% of the wrapper) rather than repeating
// the pixel value: `width` is excluded from the wrapper's own transition (see the device-card
// className below), so it snaps to its target in one paint — `w-full` just means the iframe
// always matches the card's width exactly, with no separate inline value of its own to fall
// out of sync. `width: null` = full width (Desktop, unchanged). Height is deliberately NOT
// constrained per mode — it stays bounded to the pane (`h-full`, as today) with the iframe's
// own native scrollbar handling taller content, matching the Lovable/v0 reference: a
// bounded-height card that scrolls internally, never a fixed-aspect-ratio clip.
// THE WIDTH TABLE IS `WorkspaceToolbar`'S — the switcher that picks a width lives in the shell's
// toolbar row. This component still reads the widths, so it imports the one table rather than
// keeping a second copy that could disagree about what "Tablet" means.
import { DEVICES, type DeviceName } from './workspace/devices'

// THE REVEAL RESTS ON THE FRAMED DOCUMENT VOUCHING FOR ITSELF, AND ON NOTHING ELSE.
//
// Every signal the platform can measure is taken on the wrong side of the network: the serving
// proof the ALIVE reading rests on is a loopback GET to 127.0.0.1:3000 INSIDE the container,
// while the browser reaches the same app through the portal edge → the ACA ingress, where a 502
// with an empty body loads in the frame exactly as a page does. Measured on 2026-09-10: the
// public address answered `502` with zero bytes while the control plane had already stamped the
// app as served — and `load` fired, because `load` fires for a 502 too. So `load` is not
// evidence, and neither is any status the pane is handed. The only thing that can say "a
// document of this app is showing something in the citizen's browser" is that document, and it
// does: the template's platform-owned `instrumentation-client.ts` posts `bial:app-mounted` to
// this window once its body holds visible content, and answers a `bial:ping` with the same
// message — or with `bial:app-painting` when it is alive and has nothing to show yet. The pane
// reveals on `bial:app-mounted` and never on a timer, a status, or a `load`.
//
// WHAT THE BEACON CLAIMS, AND WHAT IT DOES NOT. That this app's script ran in the frame (a 502
// ships none) and reported the page not blank. Not that the app works — a page whose code fails
// after it painted has still been seen — and not which route painted: Next's own 404 inside the
// root layout is a page with words on it, and it vouches. The compile verdict and the client-error
// relay carry the health question; this signal carries only "there is something to look at". And
// it is a CLAIM, made by a script in the framed document: the platform-owned file is the one
// meant to make it, and the model is told never to post it itself — approach (A) makes the app
// the witness, and that is the accepted trade.
//
// THE WAIT SERVES THAT ONE RULE, and nothing in it reveals anything:
//   FRAME_LOAD_CAP_MS     how long a frame may go without even a `load` before it is asked.
//   VOUCH_AFTER_LOAD_MS   how long a frame that loaded may stay silent before it is asked.
//   PINGS_BEFORE_RELOAD   ASKING COMES BEFORE RE-REQUESTING. A silent document is pinged first,
//                         because a re-request tears down the very hydration that produces the
//                         beacon; only a document that ignores its pings is fetched again — which
//                         is the bodyless 502 (nothing in it can answer), and that one closes on
//                         its own once the ingress catches up.
//   ALIVE_PINGS_BEFORE_STALL
//                         A document that answers an ask with "painting" is ALIVE for that ask
//                         and is asked again instead of fetched again — up to this many times in
//                         one uncovered stretch, then labelled; its own beacon still reveals it
//                         whenever it finally shows something. The mark is spent by the expiry
//                         that reads it: a document must answer EVERY ask to stay alive, so one
//                         reply from a page that then died buys it nothing past the next ask.
//   HEARTBEAT_MS          A revealed frame is still asked, slowly. A page can go blank after it
//                         painted (a client-side route to nothing) with no `load` for the pane
//                         to see; its "painting" answer to a heartbeat takes the reveal back.
//   VOUCH_RETRY_LIMIT     re-requests per ADDRESS — never per turn. A container whose image
//                         predates the beacon never answers, and this is what it costs: three
//                         fetches of its app, then the labelled stall card; a turn edge afterwards
//                         costs one fetch and two pings, never a fresh budget.
const FRAME_LOAD_CAP_MS = 20000
const VOUCH_AFTER_LOAD_MS = 5000
const PINGS_BEFORE_RELOAD = 2
const ALIVE_PINGS_BEFORE_STALL = 12
const VOUCH_RETRY_LIMIT = 3
const HEARTBEAT_MS = 15000

// THE WIRE, matched by value on both sides (`sandbox/template/instrumentation-client.ts`).
const MOUNTED_TYPE = 'bial:app-mounted'
const PAINTING_TYPE = 'bial:app-painting'
const PING_TYPE = 'bial:ping'

// The path an address frames, without its trailing slash, for the identity half of the beacon
// check below. Malformed fails closed to null, exactly as `originOf` does.
function framedPathOf(url: string | null): string | null {
  try {
    if (!url) return null
    return new URL(url).pathname.replace(/\/+$/, '')
  } catch {
    return null
  }
}

/** Is this inbound frame message the document at `framedPath` speaking for itself, with this
 *  `type`? Shape and identity — provenance (origin and window) is the listener's job and is
 *  checked first. Every generated app shares one origin, so the window check alone says "an app";
 *  the path the message reports is what says "the app at this address", and a frame that
 *  navigated itself to another app reports that app's path and is not revealed as this one. An
 *  address framed at the origin's root (framed path `''`) accepts any path — there is nothing to
 *  discriminate under it, and that is the shape the origin check alone already covers. */
function isFrameReportFor(data: unknown, type: string, framedPath: string | null): boolean {
  if (!isRecord(data) || data.type !== type) return false
  if (framedPath === null || typeof data.path !== 'string') return false
  const reported = data.path.replace(/\/+$/, '')
  return reported === framedPath || reported.startsWith(`${framedPath}/`)
}

// The scheme://host[:port] of an absolute preview URL, or null if unset/malformed. Used to
// VALIDATE inbound postMessage origins. A malformed value fails closed (null → no frame
// trusted, every inbound message rejected).
function originOf(url: string | null): string | null {
  try {
    if (!url) return null
    const origin = new URL(url).origin
    // An opaque origin (a data: URL, about:blank, or a sandboxed iframe without
    // allow-same-origin) doesn't throw and doesn't return null — new URL(...).origin
    // is the STRING "null" for it (confirmed: new URL('data:text/html,x').origin ===
    // "null"). That string is truthy, so without this check it would pass the
    // `!previewOriginRef.current` guard below and every opaque-origin document's
    // postMessage (whose real e.origin is also the string "null") would be trusted.
    // Folded into the same fails-closed return as a malformed URL.
    return origin === 'null' ? null : origin
  } catch {
    return null
  }
}

// While the sandbox provisions and the agent builds, there is no live app to frame yet.
const LOADING_TEXT: Partial<Record<BuildSessionStatus, string>> = {
  provisioning: 'Setting up your sandbox…',
  building: 'Building your app…',
}
// …and then a THIRD wait, which needs a line of its own: the URL has arrived and the frame is
// mounted, but nothing has painted yet. "Building your app" is stale by then (the build is done)
// and silence is a blank white card.
//
// IT SAYS "OPENING", NOT "STARTING", AND THE WORD IS THE WHOLE POINT. This pane is mounted only
// once the platform has watched the app ANSWER a request, so by the time this sentence is on
// screen the starting is over and the only thing still pending is this frame's own document —
// which is the one fact this component is entitled to describe. "Starting your app…" also belongs
// to somebody else now: it is `StartAppControl`'s pending label and `ReclaimWorkspaceDialog`'s
// step, both of them about a press, and one sentence with two authors is what this change exists
// to stop.
const FRAMING_TEXT = 'Opening your app…'
// The frame-load wait's escalated sentence, said in both places it belongs (the card and the live
// region). ONE string rather than two — the citizen is in ONE situation ("my app has not opened
// yet"), and naming it twice depending on which internal timer happens to be running is the pane
// talking about itself instead of to them.
const SLOW_TEXT = 'Your app is taking longer than usual to open'

// THE COVER. What the citizen sees instead of the framework's full-screen compile-error screen,
// which has been measured filling the preview for ~66 seconds in each of three consecutive
// builds.
//
// WHY A COVER AND NOT A FIX INSIDE THE APP. The app is framed genuinely cross-origin, so this
// pane cannot reach into `contentDocument` — but it can absolutely put its own element on top.
// That is the whole reason this is the right mechanism: it needs no per-app change and no edit
// to a file the build agent could overwrite; it survives a restore by construction; and it works
// identically on every Next version, which makes it the ONLY fix that reaches apps already
// built. A framework env var is defence in depth for new apps, not the fix.
//
// THERE IS NO LAST-GOOD-VIEW, and the reason is the same cross-origin wall: the parent cannot
// copy or screenshot the working render either. It would also be actively harmful in the case it
// sounds best in — a citizen whose workspace was wiped would keep watching their app apparently
// rendering. The cover shows the holding state, full stop.
const HOLDING_TEXT = 'Putting the latest change together…'
// …and the ONE escalation, because a wait that never changes its wording stops being read as a
// wait. Said once and never again — a card that keeps re-narrating itself reads as broken.
const HOLDING_SLOW_TEXT =
  'This change is taking longer than usual — it will appear here as soon as it\u2019s ready.'
// Pinned as a named constant BESIDE `FRAME_LOAD_CAP_MS` so it is testable and changeable in one
// place — not because 20s is measured. The holding-state duration counter is what settles it.
const HOLDING_ESCALATE_MS = 20000
// How long a `building` verdict may hold the cover ONCE THE TURN IS OVER before the framed
// document's own word outranks it. A compile takes seconds (a cold first route was measured at
// 5–7 s); a `building` still standing half a minute after the build finished is a verdict nothing
// will ever update. Mid-turn the running turn covers on its own account, so this clock starts only
// when it ends.
const BUILDING_COVER_MAX_MS = 30000

// WHAT THE COVER SAYS WHEN NO TURN IS RUNNING. "Putting the latest change together…" is
// true for exactly as long as one is; left up afterwards it becomes a progress state that never
// resolves.
//
// TWO SENTENCES, BECAUSE THE COVER HAS TWO IDLE CAUSES AND THEY ARE OPPOSITES. `failed` means the
// newest change did not come together, so the document behind this cover is the framework's error
// screen. `building` means the app is compiling a route right now — which the supervisor publishes
// for any on-demand compile inside a perfectly healthy app, so one sentence for both would tell a
// citizen with a working app that something is wrong with it.
//
// BOTH DESCRIBE THE PAGE, NOT THE WORKSPACE, AND THAT IS A RULE NOW RATHER THAN A PREFERENCE. This
// pane is mounted only where the platform has watched the app ANSWER a request, so it is never
// this component's place to say whether an app is running — and it no longer has the vocabulary
// to. `IDLE_BUSY_TEXT` used to read "Getting your app ready…", which is word for word the sentence
// the workspace map says while nothing is serving (`workspace/workspaceState.ts`), and
// `IDLE_BROKEN_TEXT` used to open "Your app isn't running right now" — the same claim the apps
// router's own error page makes, told from inside a pane that exists only because the app is up.
// Two authors, one sentence; on 2026-09-10 the two of them contradicted each other on screen.
//
// NEITHER CLEARS THE COVER. Behind it is the framework's error screen today and a blank page once
// that is suppressed, so clearing would trade a lie about progress for a lie about the app.
//
// Deliberately NOT the reversion sentence below. That one means the workspace was lost and a
// restore is coming; promising a restore for a compile error would be a third lie.
const IDLE_BUSY_TEXT = 'Putting this page together…'
const IDLE_BROKEN_TEXT =
  'The last change didn\u2019t come together, so this page can\u2019t open. Send a message describing what you\u2019d like and we\u2019ll get it working.'

// THE RETRACTION, and it goes on the PANE rather than in the chat because no turn is
// running. Nobody is looking at the transcript; they are looking at what they believe is their
// app, above a message that says it is finished.
//
// IT NAMES WHAT IS IN THE FRAME, which is both the only claim this pane is entitled to make and,
// here, the whole of the news: the idle probe found the workspace reverted, so the document behind
// this cover is a stranger's — a starter template rendering perfectly, which is exactly why the
// compile signal would never raise a cover over it. This sentence used to open "Your app stopped
// running", a verdict on the WORKSPACE that this component cannot reach and that contradicts its
// own mounting condition.
//
// IT PROMISES A RESTORE, and unlike every other sentence in this file it is entitled to: the next
// turn's integrity gate finds the same reversion, puts the app back from the last durable copy,
// and says so. That is why the promise is here and not on `IDLE_BROKEN_TEXT`, which describes a
// compile failure that no restore would fix.
//
// It OUTRANKS both idle sentences. A progress line over a workspace that has been wiped is exactly
// the false-progress claim this pane must never make.
const WORKSPACE_REVERTED_TEXT =
  'What\u2019s in this frame isn\u2019t your app any more — the workspace was reverted. Send a message and we\u2019ll restore it.'

/**
 * THIS FILE NO LONGER AUTHORS A SINGLE WORKSPACE SENTENCE, AND THAT IS THE CHANGE.
 *
 * WHAT WENT: `GoneState`, `GONE_TITLE` and `goneBody` — four headlines and six bodies for
 * asleep / slot_taken / never_built / disconnected — together with the `showUnavailable` card that
 * drew them and the `showTerminal` card beside it. Every one of those sentences already had an
 * author: `workspace/workspaceState.ts` computes ONE state for the whole workspace and `AppPane`
 * draws it. This component was the second author, and two authors of one sentence is not a
 * duplication, it is a CONTRADICTION waiting for the composition nobody tested — which is exactly
 * what the state map found: a reading of `asleep` reached this pane as a start outcome the veto let
 * through, so "Your workspace is asleep" was drawn over an app the map was at that moment calling
 * up, for as long as a 45-second poll cycle.
 *
 * The `slot_taken` copy was already unreachable through the composed product (the map sends every
 * slot-taken reading to a held state, which `AppPane` never frames), and once BUILDING absorbs the
 * container-up-but-not-serving reading, nothing escapes that veto with a gone `previewState` at
 * all. So the rest went with it.
 *
 * WHAT STAYS is only what has no other author and is a COVER OVER A FRAME rather than a state: the
 * reconnecting card, the compile cover, the loading bounce and the frame-stall card. Their rule is
 * that they may describe the document in front of them and nothing else. If you find yourself
 * adding a sentence here about whether an app is running, whose workspace it is, or what a citizen
 * should press — it belongs in the map, not in the pane.
 */

/**
 * `RelaunchAffordance` IS GONE, and its four render sites with it. Exactly ONE control
 * starts the app — `components/workspace/StartAppControl.tsx`, rendered by `AppPane` from
 * the one computed workspace state, whose action union has no destructive verb. The four
 * placeholder-arm buttons this replaced said the same thing five times over, each in a
 * different vocabulary ("Relaunch preview") than the one the client settled on ("Launch
 * Application"). The placeholders keep their sentences: they name no control they lack.
 */

/**
 * The calm wait: an opaque full-bleed card with the pane's bouncing dots and one sentence.
 *
 * Shared by the frame-load wait and the compile cover so they can't visually drift — it was
 * two copies of the same markup kept in step by a comment; now kept in step by construction.
 * Deliberately NOT shared with the frame-stall card below (a dev server genuinely down is a
 * different fact). Callers keep their own guards; only the chrome is shared.
 */
function BouncingWait({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div
      className="absolute inset-0 z-20 bg-[#e8edf2] flex flex-col items-center justify-center gap-4"
      aria-busy="true"
    >
      <div className="flex gap-2">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="w-3 h-3 bg-primary rounded-full animate-bounce"
            style={{ animationDelay: `${i * 0.2}s` }}
          />
        ))}
      </div>
      <p className={`text-sm text-neutral font-medium ${className}`}>{children}</p>
    </div>
  )
}

/**
 * The live-preview pane.
 *
 * Phase-2 model: the agent builds a REAL running Next.js app inside a per-user sandbox, and
 * this pane frames that app's genuinely CROSS-ORIGIN `previewUrl` once the dev server is up.
 * All single-file machinery — the `jsx:preview` fence, `previewCode` threading, the outbound
 * `postMessage` of `{previewCode, config, accessToken, user}`, the `generationStage` progress
 * theater, and the "View Code" source panel — is GONE (the same-origin Babel `/preview` was
 * already retired; the app now gets its data credentials server-side at provision — the portal
 * feeds the app nothing).
 *
 * Driven entirely by the build session:
 *   - `previewUrl` — the sandbox `next dev` root, from the status read / `preview_ready`.
 *                    Framed once set.
 *   - `status`     — the session lifecycle; drives loading / framed / terminal visuals.
 *   - `iterating`  — true while the loop keeps emitting step/log envelopes AFTER the preview
 *                    went live (a refine turn holding at `ready`); shows a subtle overlay.
 *   - `onFrameMessage` — the client-error receiver seam. The inbound `message` listener
 *                    validates BOTH `e.origin` against the preview origin AND `e.source` against
 *                    this pane's own iframe window, and forwards only messages that pass both;
 *                    the conversation surface relays them to the harness, where a reported
 *                    browser crash makes the health verdict not-green. The source half is what
 *                    survives every app sharing one hostname — origin alone no longer tells this
 *                    pane's app from any other app in the document. Together they prove
 *                    PROVENANCE, not content — the shape check lives on the receiving side. Note
 *                    `scripts/skeleton/frame-proof` is a standalone Chromium rig with its OWN
 *                    inline origin guard: it never renders this component, so it neither
 *                    exercises nor regression-catches the gate written here.
 *
 *   - `serving`   — IS A CONTAINER STILL ANSWERING AT `previewUrl`? It arrives on the ADDRESS
 *                    (`utils/previewAddress.ts`), not from the conversation, and it exists to
 *                    outrank a terminal `status`: the backend pardons a turn's container
 *                    unconditionally (it stays up under an idle lease), so `ended` plus a serving
 *                    container means "the build is over and your app is still there" and the pane
 *                    keeps framing it. A container that is genuinely gone collapses the frame to
 *                    an EMPTY pane, which is not an oversight: with no frame there is nothing for
 *                    this component to describe, and `AppPane` is what draws a sentence when the
 *                    workspace has one to say.
 *
 *                    IT WAS `completedLive`, AND THE RENAME IS THE FIX. That name answered two
 *                    questions with one boolean — "the container is up" and "a build finished
 *                    successfully" — and the second one is gone with the completion chip. What is
 *                    left is liveness, and liveness alone: this prop is not evidence that anything
 *                    compiled, and it must never be read as such. What the pane may SAY about the
 *                    newest build comes from `compileState`, whose `unknown` asserts nothing.
 *   - `reconnecting` — the dev-server PROCESS crashed after the preview was framed (a backend
 *                    `preview_reconnecting` signal — the frontend can't poll /dev/status).
 *                    DISTINCT from the "Building…" loading bounce and from `feedDisconnected` (the
 *                    SSE feed dropping): the pane shows a "Reconnecting…" cover over the dead frame
 *                    until a fresh `preview_ready` re-frames.
 *
 *                    IT IS NO LONGER BOUNDED HERE, AND THE BOUND MOVED TO THE SERVER RATHER THAN
 *                    BEING DROPPED. A 20-second cap used to collapse this cover into a "preview
 *                    unavailable" card — one of the four workspace verdicts this file has stopped
 *                    authoring — so the cap had nowhere left to land: expiring it would have left
 *                    an empty rectangle, and re-mounting the frame instead would have framed the
 *                    apps router's own "This app isn't running right now" page, the exact defect
 *                    this change exists to end. A dev server that dies and never comes back is now
 *                    the SERVING STAMP's business: the watcher clears it on the crash edge and the
 *                    reconciler clears it out of turn, the reading stops being `running`, and the
 *                    one workspace author draws the one card. What this pane owes that citizen is
 *                    not a verdict — it is to keep saying, honestly, that it is still waiting.
 *
 * `hasSavedBuild` AND `occupyingProjectName` ARE GONE, with the two cards that read them. Both
 * existed to fill in a sentence about the WORKSPACE ("your saved app is still there", "Baggage
 * Reconciliation is using your build workspace"), and the workspace map owns every one of those
 * now. `workspace/workspaceChannel.ts` must drop the matching `PaneView` fields — its
 * `UnacceptedPaneProps` assertion is what makes that a compile error rather than a field quietly
 * going nowhere — and its publishers stop computing them.
 */
export interface LivePreviewProps {
  previewUrl?: string | null
  status?: BuildSessionStatus | null
  iterating?: boolean
  onFrameMessage?: (data: unknown) => void
  serving?: boolean
  reconnecting?: boolean
  // The server's verdict on THIS project's container. ONE value of it is read here now, and it is
  // read for one purpose: `starting` withholds the frame.
  //
  // EVERY OTHER VALUE IS SOMEBODY ELSE'S TO SPEAK FOR. `asleep`, `slot_taken`, `never_built` and
  // `unknown` used to each pick a headline out of this file's own copy table; they are the map's
  // now, and `AppPane` will not mount this component while the workspace reading is any of them.
  // What is left is a refusal, not a sentence — see `starting` below for why the refusal stays
  // even though the veto above it makes it unreachable.
  //
  // `null` = we have not polled yet, and claims nothing.
  previewState?: PreviewLifeState | null
  // What the app's dev server is compiling RIGHT NOW, streamed from the container.
  // FOUR values, and the fourth is why this is not a boolean: `unknown` means the platform
  // could not tell (no signal yet, socket down, or a container image predating the signal —
  // which is every app built before this shipped), and it HOLDS the cover rather than clearing
  // it. `null` = nothing has been reported on this turn at all, which is the pre-signal state
  // and behaves exactly as this pane did before the cover existed.
  //
  // NEITHER OF THOSE TWO IS A LICENCE TO REVEAL ANY MORE, and that is the 2026-09-10 change. While
  // a turn is running they DENY the reveal outright rather than merely holding whatever is already
  // up — holding is only fail-closed when something is showing to hold, and on a first build
  // nothing is. `unknown` was this signal's answer for the whole of the run in which a citizen
  // watched a blank white rectangle, so "the signal said nothing" can no longer be the entire
  // basis on which this pane shows a frame.
  compileState?: CompileState | null
  // Is a turn running on this project RIGHT NOW? Two jobs, and the second one is new.
  //
  // IT PICKS WHICH OF THE COVER'S SENTENCES IS TRUE — a wait that describes work nobody is doing
  // is a progress state that never ends.
  //
  // AND IT NOW RAISES THE COVER, IN ONE DIRECTION ONLY. A running turn with no `clean` verdict is
  // covered, because nothing vouches for a document served out of an app that is being rewritten
  // as the citizen watches (see `flyingBlind`). What this prop still cannot do is LOWER a cover:
  // an app that is broken is just as broken between turns, and the error screen behind the cover
  // does not become safe to show because the build stopped. The note that used to sit here said
  // this prop "deliberately does NOT decide whether to cover" — written while the compile signal
  // was assumed to work. On 2026-09-10 it did not work, and the entire cover went down with it.
  turnRunning?: boolean
  // The idle probe found this app's workspace reverted. It outranks every other cover sentence
  // because it is the only one that is a fact about what is IN THE FRAME rather than about a
  // compile: the others all describe the citizen's own app, and this one says the document behind
  // the cover is not it.
  workspaceLost?: boolean
  /**
   * THE WIDTH THIS PANE FRAMES AT, AND ITS RELOAD SIGNAL — both owned by the shell.
   *
   * The boards draw ONE toolbar for the whole workspace, above the two columns, and the device
   * switcher and the Reload control are in it. State follows its control, so these arrive as
   * props. `reloadNonce` is combined with — never replaces — this component's own automatic
   * remount signal below.
   */
  device?: DeviceName
  reloadNonce?: number
  // Fired the moment this pane is actually SHOWING the app — the frame loaded and no cover is
  // over it — which is the honest stop-clock for "how long until the citizen saw their app".
  // Optional and fire-and-forget: this pane owes the caller nothing if the caller does not care,
  // and a throwing callback is swallowed rather than allowed to take the pane down. Fires at most
  // once per FRAME KEY, the same discipline the load and stall verdicts follow.
  //
  // WHAT IT DOES NOT PROMISE, so a counter built on it is read correctly: that the app WORKS. The
  // reveal means the framed document itself reported that it is showing something; a page whose
  // own code then crashes, or one that renders the wrong thing, has still been SEEN. So this fires
  // when the citizen is looking at their app, not when the app is known good — the wait is what
  // it measures, and a broken app ends a wait too.
  //
  // WHAT IT NEVER FIRES FOR is a document nothing vouched for — a `load` with no beacon behind it.
  // That stop-clock used to be stopped by a blank white rectangle, so the one number meant to say
  // "this is how long until the citizen saw their app" was reporting the fastest views of the day
  // for exactly the runs in which they saw nothing at all.
  onRevealed?: () => void
  // Told when the wait on the framed document is LABELLED SLOW — `true` the moment the re-requests
  // run out with no vouch — and told again with `false` when that label comes down: a late beacon
  // that wins, or a new document with a wait of its own. Same fire-and-forget rules as `onRevealed`.
  //
  // A STALLED FRAME IS THE ONLY SIGN THIS PANE GETS OF A STOPPED APP. `preview-state` answers from
  // the registry and goes on saying `alive` over a dev server that has died, so the caller asks the
  // server to look at the process instead — and a stopped app with its work saved is put away and
  // offered back with its start control, rather than this card waiting for a load that cannot come.
  onStallChange?: (stalled: boolean) => void
}

export default function LivePreview({
  previewUrl = null,
  status = null,
  iterating = false,
  onFrameMessage,
  // Absent means NOTHING IS KNOWN TO BE SERVING, which is restrictive on purpose: it is the only
  // default under which a caller that forgot the prop cannot keep framing a URL nobody is
  // answering. The cost of the restrictive default is a frame that collapses on a terminal status,
  // which is what this pane did for its whole life before the flag existed.
  serving = false,
  reconnecting = false,
  // Absent means NOT YET ASKED, never "confirmed gone": a caller that forgot the prop must not be
  // able to make this pane act on a verdict nobody reached.
  previewState = null,
  // Absent means NOTHING WAS REPORTED, never "clean". A default of `'clean'` would let a caller
  // that forgot the prop uncover the frame over an error screen nobody looked at — which is the
  // exact failure this whole mechanism exists to stop.
  compileState = null,
  // Absent means NO TURN IS RUNNING, which is the safe default here: it selects the sentence that
  // asks the citizen to send a message, and telling someone their app needs a nudge when a build
  // is quietly in flight costs them one message. The reverse — claiming work is in progress when
  // none is — is the failure this prop exists to prevent.
  turnRunning = false,
  workspaceLost = false,
  device = 'Desktop',
  reloadNonce: externalReloadNonce = 0,
  onRevealed,
  onStallChange,
}: LivePreviewProps) {

  // The sandbox preview origin, held in a ref so the mount-once message listener always reads
  // the CURRENT origin without re-subscribing on every prop change.
  const previewOrigin = originOf(previewUrl)
  const previewOriginRef = useRef(previewOrigin)
  previewOriginRef.current = previewOrigin
  const onFrameMessageRef = useRef(onFrameMessage)
  onFrameMessageRef.current = onFrameMessage
  // This pane's own frame, for the sender-identity half of the trust gate below. A ref for the
  // same reason the two above are: the listener mounts once and must read the CURRENT frame.
  const frameRef = useRef<HTMLIFrameElement | null>(null)

  // The one cross-origin trust seam, and it now takes TWO facts, not one.
  //
  // WHY THE ORIGIN CHECK STOPPED BEING ENOUGH. It only ever discriminated because each app had a
  // hostname of its own. BIAL refused a wildcard certificate, so every generated app is now served
  // from ONE name — one certificate, one label, one browser origin for all of them — so an origin
  // comparison alone now only proves "this is an app", not "this is the app I am framing". The
  // reachable sender is not an unrelated tab (every place the portal opens an app in a new tab
  // severs window.opener with rel="noopener"): it is a nested or sibling frame inside THIS portal
  // document, whose e.origin is now identical to the pane's own preview origin.
  //
  // So the sender's identity is checked as well: `e.source` must be the window of the iframe this
  // pane rendered. Both halves stay — origin proves the bytes came from the apps host, source
  // proves they came from THIS pane's app rather than any other one on it. Dropping either is a
  // regression, not a simplification.
  //
  // FAILS CLOSED, DELIBERATELY STRUCTURALLY. The frame is conditionally mounted and keyed, so the
  // ref is legitimately null while the pane is reconnecting or terminal, and across every
  // reload-nonce remount; messages arriving then are dropped, where origin alone used to forward
  // them. That is accepted knowingly — in those states the app document is gone, so nothing can be
  // posting. `frameWindow` is bound and null-guarded rather than compared inline because
  // `e.source !== ref.current?.contentWindow` reads correct while being one character (`!==` ->
  // `!=`) away from accepting every source-less message: null == undefined.
  //
  // The forwarded payload feeds the browser client-error arm of self-heal: passing this gate
  // proves only WHERE the bytes came from, so the receiver narrows their shape downstream.
  // THE FRAMED DOCUMENT'S OWN VERDICTS — declared here, above the listener that writes one of
  // them; derived into booleans further down, where the frame key they are compared against
  // exists. See the block above `frameLoaded` for what each one means. `loadedKey` and
  // `vouchedKey` are mirrored in refs for the two places that must read them at EVENT time — a
  // timer deciding whether to re-request, and a `load` deciding whether it is a reload — where a
  // value captured at render can be a paint behind the truth.
  const [loadedKey, setLoadedKey] = useState<string | null>(null)
  const [vouchedKey, setVouchedKey] = useState<string | null>(null)
  const [stalledKey, setStalledKey] = useState<string | null>(null)
  const loadedKeyRef = useRef<string | null>(null)
  const vouchedKeyRef = useRef<string | null>(null)
  // The key of a document that answered a ping with "painting": alive, not yet showing anything.
  // Read only at event time, so a ref.
  const aliveKeyRef = useRef<string | null>(null)
  // The path this pane frames, for the identity half of the beacon check — a ref for the same
  // reason `previewOriginRef` is one.
  const framedPathRef = useRef(framedPathOf(previewUrl))
  framedPathRef.current = framedPathOf(previewUrl)
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (!previewOriginRef.current || e.origin !== previewOriginRef.current) return
      const frame = frameRef.current
      const frameWindow = frame?.contentWindow
      if (!frame || !frameWindow || e.source !== frameWindow) return
      // THE BEACON, consumed here and forwarded nowhere: it is this pane's evidence, not a report.
      // It is recorded against the key of the element that SENT it — read off the committed DOM,
      // never off a value computed in render, which can run a key ahead of the iframe that is
      // actually on screen — so a late beacon from a document on its way out vouches for that
      // document and never for its successor.
      if (isFrameReportFor(e.data, MOUNTED_TYPE, framedPathRef.current)) {
        const sentBy = frame.getAttribute('data-frame-key')
        vouchedKeyRef.current = sentBy
        // A vouch settles every question the wait was asking, and refunds the asking budgets: a
        // page that blanks and repaints more than once must be asked afresh each time, not
        // stall-carded on the second blank because the first one spent its asks.
        aliveKeyRef.current = null
        pingsRef.current = 0
        alivePingsRef.current = 0
        setVouchedKey(sentBy)
        return
      }
      // "ALIVE, NOTHING TO SHOW YET" — the answer to a ping from a document that is still painting.
      // Not a reveal; it only tells the wait below that this document must be asked again rather
      // than fetched again, which is the difference between a slow page being waited for and a
      // slow page being torn down three times and labelled.
      if (isFrameReportFor(e.data, PAINTING_TYPE, framedPathRef.current)) {
        const sentBy = frame.getAttribute('data-frame-key')
        aliveKeyRef.current = sentBy
        // …AND A REVEALED FRAME THAT SAYS SO HAS GONE BLANK: the heartbeat asked, and the answer
        // is that there is nothing to see any more. The reveal is taken back and the ordinary
        // wait — asking, then labelling — takes over from here.
        if (sentBy !== null && vouchedKeyRef.current === sentBy) {
          vouchedKeyRef.current = null
          setVouchedKey((current) => (current === sentBy ? null : current))
        }
        return
      }
      onFrameMessageRef.current?.(e.data)
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [])

  const isTerminal = status === 'ended' || status === 'failed'
  // A finished turn's container is PARDONED server-side (alive under an idle lease), so
  // its `ended` is "done, your app is still there", not "gone": keep framing the URL. Only with a
  // URL, though — liveness with no address to frame is not a frame, and this pane draws nothing it
  // cannot point at.
  const keepFramed = serving && !!previewUrl
  // The pane WOULD frame the app here (a live preview, or a pardoned container after the turn
  // ended). A terminal session collapses it even with a `previewUrl` still around — post-ready
  // teardown must NOT keep displaying a now-dead URL — UNLESS the pardon says the URL is genuinely
  // live. A dev-process crash (`reconnecting`) pre-empts the live frame with the cover below.
  //
  // WHAT A COLLAPSE LEAVES BEHIND IS AN EMPTY PANE, AND THAT IS THE DELIBERATE ANSWER RATHER THAN
  // A HOLE. There used to be a card here — "The preview is no longer running", with a saved-build
  // line under it — and it was this file's fourth workspace verdict: a sentence about the citizen's
  // app, told by the one component that can only see a frame. The composed product does not reach
  // it (a reading that is not `running` never mounts this pane at all), and where it did, the map
  // was already saying something better on the same screen. The pane frames what is there; when
  // nothing is there, `AppPane` is what speaks.
  const frameContext = !!previewUrl && (!isTerminal || keepFramed)

  // STARTING IS A WAIT, NOT A FAULT, and it must not be framed either.
  //
  // A container the platform is still bringing up answers 502 at its own edge, and the apps
  // router turns a 502 into the "This app isn't running right now" page. Framed, that page is
  // shown to a citizen whose app is being started — the opposite of the truth, told at the one
  // moment they are watching. It is a backstop for a document the portal did not expect, never a
  // state a person should reach.
  //
  // IT SURVIVES THE VETO THAT MAKES IT UNREACHABLE, ON PURPOSE. `AppPane` now mounts this pane if
  // and only if the workspace reading is `running`, which is gated on the platform having watched
  // the app ANSWER a request — so a `starting` reading should never get this far. "Should never"
  // is exactly the claim that was true of the eight seconds a citizen spent reading that error
  // page on 2026-09-10, and this refusal costs one boolean. It is the pane's own last word on
  // never framing a container that is provably not answering yet.
  //
  // TAKING THE FRAME AWAY IS ONLY HALF A STATE, and the first version of this shipped only that
  // half: with the frame withheld and nothing put in its place, a start with a `previewUrl` in
  // hand drew an EMPTY RECTANGLE — no iframe, no wait, the sentence reaching the live region and
  // nobody else. So `starting` is carried into `showLoading` below, and those two uses are the
  // whole of it: not framed, visibly waiting.
  const starting = previewState === 'starting'
  // THE RECONNECTING COVER IS NO LONGER CAPPED, and the bound moved rather than vanished — see the
  // `reconnecting` prop's docblock. Its landing state was the deleted "preview unavailable" card,
  // so an expiry now has nowhere honest to go: an empty rectangle says nothing, and re-mounting the
  // frame over a dev server that is genuinely down frames the apps router's error page — the defect
  // this whole change exists to end. A crash that never recovers clears the serving stamp on the
  // server; the reading stops being `running`; `AppPane` unmounts this pane and draws the one card.
  const showReconnecting = frameContext && reconnecting && !starting
  const showFrame = frameContext && !reconnecting && !starting

  // The reveal is gated on the framed document vouching for itself, never on a timer and never on
  // `load` — see the top of this file. A timer can only prove that time passed; `load` proves a
  // response arrived, and a bodyless 502 is a response.
  //
  // Every verdict is recorded PER FRAME KEY rather than as a bare boolean, because a stale verdict
  // is a lie about the frame the citizen is currently looking at: a fresh `preview_ready` re-gates
  // the reveal by construction, and a relaunch after the stall returns to the honest wait instead
  // of opening into a stale complaint about a frame that no longer exists. (That second one was
  // caught in a browser, not in a test — jsdom will happily agree with whatever the state machine
  // says.)
  //
  // Note what a reveal does and does NOT claim: the beacon says the document showed something in
  // this browser, never that the app is healthy. Whatever ends up rendering over a framed-but-broken app hangs off a
  // health signal from the server, not off this flag.
  // …but `previewUrl` alone is NOT a sufficient identity for the frame. Attaching to a container
  // that is already up makes "same container, same FQDN, same URL" the common case, so a repair
  // turn ends with `previewUrl` byte-identical to what it was before: React sees the same key,
  // keeps the same DOM node, the browser never re-requests — and the citizen keeps staring at the
  // broken render of an app the server has already fixed. A browser run caught it exactly: the
  // server served 341 chars of the repaired app while the frame reported `loads 1 -> 1`. HMR
  // usually rescues it, which is why it is intermittent rather than constant — but not when
  // self-heal restarts `next dev` mid-turn, which kills the framed document's HMR socket without
  // anything on this side noticing.
  //
  // So the frame gets an identity that can change when the URL cannot. `iterating` falling is the
  // honest moment: it means a turn that was running OVER a live preview just ended, which is the
  // repair case and nothing else. A timer would reload an idle pane; a status tick would reload
  // on every poll and leak the HMR socket the frame's `key` comment rightly protects.
  const [autoReloadNonce, setAutoReloadNonce] = useState(0)
  // Re-requests spent on a document that ignored its pings (the vouch wait below), counted per
  // ADDRESS and reset by exactly two things: a new URL and the citizen's own Reload. NOT by the
  // platform's turn edges below — `iterating` falls after any four-second gap in the stream, so
  // a reset there would hand a document that never answers a fresh budget several times per turn,
  // and the bound on this counter would bound nothing. Pings are counted per KEY beside it and
  // reset with the verdicts. Refs, because a count must not render.
  const vouchRetriesRef = useRef(0)
  const pingsRef = useRef(0)
  const alivePingsRef = useRef(0)
  useEffect(() => {
    vouchRetriesRef.current = 0
  }, [previewUrl, externalReloadNonce])
  const wasIterating = useRef(false)
  useEffect(() => {
    if (wasIterating.current && !iterating && previewUrl) setAutoReloadNonce((n) => n + 1)
    wasIterating.current = iterating
  }, [iterating, previewUrl])
  // AND THE FIRST TIME THE APP ACTUALLY STARTS ANSWERING, which is the one this pane was missing
  // and the reason a citizen had to reload four times to see their first build.
  //
  // The URL the poll publishes while the container is merely CREATED and the URL published once
  // the app is serving are the same string — both are `settings.app_url(app_name)`. So `frameKey`
  // is byte-identical across the moment the app comes up: React keeps the DOM node and the browser
  // never re-requests. Whatever loaded first is what the citizen keeps looking at, and during a
  // first build what loaded first is the router's 502 page, because the app was not listening yet.
  // Nothing on this side notices, because nothing about the address changed.
  //
  // `iterating` above cannot cover it: it fires when a turn ends OVER a live preview, and a first
  // build has no live preview to have been iterating over. This is the other edge — not-serving to
  // serving — and it is the only moment at which the document that failed becomes worth asking for
  // again. Guarded on a previous status existing so a pane that mounts straight into `ready` does
  // not reload a document it just asked for.
  //
  // THE EDGE IS "A BUILD FINISHED", NOT "THE STATUS BECAME READY", and the difference is a
  // regression this nearly shipped. Written as `!== 'ready'` it also fired on `ended`→`ready`,
  // which is the transition EVERY follow-up message produces on a warm container: the turn's own
  // status takes over from the transcript's `ended` the moment a send starts. The citizen would
  // have had their running app torn down and re-fetched — scroll position, form state and the HMR
  // socket with it — on every message they sent, to fix a document that was never stale. Only the
  // provisioning/building→ready edge means "the app was not answering and now is", which is the
  // one case the stale 502 document survives.
  // AND THE EDGE HAS TO BELONG TO THE SAME APP, which is why the ref carries the address and not
  // just the status. This pane deliberately has no `key` (see `AppPaneHost`) so it survives a
  // navigation — meaning it can go from one app mid-build straight to a DIFFERENT app already
  // serving, in a single update. Watching the status alone, that reads as "the build finished"
  // and bumps the nonce, so the new app's document is fetched once for the address change and
  // again for a build that was never its own: two round trips and a visible flash for one switch.
  // The cover below already guards its own state this way (`sameApp`); this is the same hazard.
  const wasStatus = useRef<{ status: BuildSessionStatus | null; url: string | null }>({
    status: null,
    url: null,
  })
  useEffect(() => {
    const prev = wasStatus.current
    const wasMidBuild = prev.status === 'provisioning' || prev.status === 'building'
    if (wasMidBuild && prev.url === previewUrl && status === 'ready' && previewUrl) {
      setAutoReloadNonce((n) => n + 1)
    }
    wasStatus.current = { status, url: previewUrl }
  }, [status, previewUrl])
  // TWO INDEPENDENT REASONS TO RE-REQUEST THE DOCUMENT, COMBINED RATHER THAN COLLAPSED. The one
  // above is the platform's — a turn ended over a live preview, so the served bundle may be stale.
  // The other is the citizen's, from the toolbar row's Reload control, for the staleness the
  // platform cannot detect (a dev server restarted, an HMR socket that died quietly). Either one
  // moving changes the key; neither can reset the other, which a single shared counter would.
  const frameKey = previewUrl ? `${previewUrl}#${autoReloadNonce}.${externalReloadNonce}` : null
  // EVERY VERDICT IS FORGOTTEN THE MOMENT THE KEY CHANGES, in the same render and before anything
  // paints. The key is a function of the address and two counters, so it can RECUR: this pane
  // survives a navigation from app A to app B and back, and A's key comes back byte-identical
  // while A's iframe is a brand-new element that has fetched nothing. A vouch remembered from the
  // first visit would reveal that empty element on its first paint. Done as a render-time
  // derivation (React re-renders before committing) rather than as an effect, which would paint
  // the stale reveal for one frame first. The refs follow the state so a timer that fires in the
  // render-to-commit gap cannot mistake the old visit's vouch for this one's.
  const [keyOfTheseVerdicts, setKeyOfTheseVerdicts] = useState(frameKey)
  if (keyOfTheseVerdicts !== frameKey) {
    setKeyOfTheseVerdicts(frameKey)
    setLoadedKey(null)
    setVouchedKey(null)
    setStalledKey(null)
    loadedKeyRef.current = null
    vouchedKeyRef.current = null
    aliveKeyRef.current = null
    pingsRef.current = 0
    alivePingsRef.current = 0
  }

  // THE FRAMED DOCUMENT'S OWN VERDICTS, all three keyed on the FRAME KEY rather than the URL:
  // keyed on the URL they survived a remount, so a reload that hung would have kept the stale
  // document revealed and unlabelled forever. The reveal must be re-earned by whichever document
  // is in the frame now, and only that document can earn it.
  //
  //   loaded    the browser finished fetching SOMETHING at this key. Not evidence — a 502 fires
  //             it too — but it starts the shorter vouch wait and sends the ping.
  //   vouched   the document posted `bial:app-mounted`: it is showing something in this browser.
  //             THE ONLY REVEAL SIGNAL IN THIS FILE.
  //   stalled   the re-requests ran out with no vouch, so the wait is labelled as slow.
  const frameLoaded = showFrame && loadedKey === frameKey
  const frameVouched = showFrame && vouchedKey === frameKey
  const frameStalled = showFrame && !frameVouched && stalledKey === frameKey

  // THE COMPILE VERDICT'S HALF OF THE COVER, and only that half. `covered` below is what every
  // other line in this file reads; this state is split out of it so the fail-closed arm added
  // there can never be written back INTO the verdict and then lost the next time the container
  // reports. A signal that failed must not be able to erase the fact that it failed.
  // WHICH verdict raised it, not merely whether one did: `building` is transient by nature (a
  // compile takes seconds), `failed` is a state, and the two are treated differently below when
  // the signal that raised them goes dark.
  const [verdictCover, setVerdictCover] = useState<'building' | 'failed' | null>(null)
  const coveredByVerdict = verdictCover !== null
  // Which app the current verdict describes. A ref rather than state because it must not itself
  // cause a render — it exists only to tell "a new verdict about the same app" from "the same
  // verdict about a new app".
  const coveredUrlRef = useRef<string | null>(null)
  // ONE effect over BOTH inputs, and it must stay one.
  //
  // This was two effects — reset-on-url, then apply-on-verdict — and that shape had a hole the
  // signal's own vocabulary walks straight into. There are only four possible values, so "a new
  // app" and "the same verdict as the last app" routinely coincide: a relaunch onto a container
  // that is still failing carries `failed` -> `failed` across the url change with NO delta. React
  // skips an effect whose deps did not change, so the verdict effect never ran, the reset won
  // uncontested, and the pane uncovered itself over a broken app — the precise failure this
  // mechanism exists to prevent. Depending on the two effects' declaration order was the tell.
  //
  // Re-deriving from scratch on either input closes it, and the hold still holds: `unknown` and
  // `null` change nothing for an app we are already covering. They only uncover on a genuinely
  // NEW app, which we have learned nothing about yet — and nothing is exposed in that gap, since
  // a new url remounts the frame and the frame-load wait owns the screen until the first report.
  // …AND THE CITIZEN'S OWN RELOAD IS A GENUINE ESCAPE HATCH: it fetches a new document, and the
  // verdict about the old one does not get to pre-judge it. The next report re-raises the cover if
  // the compile is still broken.
  //
  // …AND THE CITIZEN'S OWN RELOAD IS FOLDED INTO THIS ONE DERIVATION, NOT WRITTEN FROM A SECOND
  // EFFECT. A Reload fetches a new document, so a verdict nobody can re-confirm does not get to
  // pre-judge it — but a verdict that IS standing (`building`, `failed`) is re-applied on the very
  // same render, because this effect re-derives from scratch. A second writer keyed on the nonce
  // alone would clear the cover and never see it re-raised: the standing value is Object.is-equal
  // to itself, so the verdict effect would not run again — the exact hole the two-effects note
  // above records.
  const lastReloadRef = useRef(externalReloadNonce)
  useEffect(() => {
    const sameApp = coveredUrlRef.current === previewUrl
    coveredUrlRef.current = previewUrl
    const reloaded = lastReloadRef.current !== externalReloadNonce
    lastReloadRef.current = externalReloadNonce
    if (compileState === 'building' || compileState === 'failed') setVerdictCover(compileState)
    else if (compileState === 'clean') setVerdictCover(null)
    else if (!sameApp || reloaded) setVerdictCover(null)
  }, [previewUrl, compileState, externalReloadNonce])
  // …AND A `building` COVER EXPIRES ONCE THE TURN IS OVER. The effect above latches it, and only
  // a `clean`, a new app or the citizen's Reload clears it. The engine settles the verdict with ONE
  // compile poll when a turn ends and, if that poll still reads `building`, leaves it standing
  // (`engine.py::_settle_compile_state`: "the next turn resolves it"). So a finished build whose
  // last read landed mid-compile kept "Putting this page together…" over a served app until the
  // citizen sent another message — photographed in production on 2026-09-10 over a green build, a
  // gateway with no 5xx and a container built from the beacon image. Only `building`: `failed` is a
  // state, not a moment, and never expires. Only when no turn is running: mid-turn `flyingBlind`
  // covers regardless. What follows is the document's own word — revealed if it vouched (it
  // usually already has, under the cover), asked like any silent document if it has not.
  // KEYED TO THE APP TOO: a new app whose first report is also `building` leaves `verdictCover`
  // Object.is-equal, so without `previewUrl` here it would inherit the outgoing app's clock.
  useEffect(() => {
    if (verdictCover !== 'building' || turnRunning) return
    const t = setTimeout(() => setVerdictCover(null), BUILDING_COVER_MAX_MS)
    return () => clearTimeout(t)
  }, [verdictCover, turnRunning, previewUrl])

  // WHY THE COVER NO LONGER TRUSTS A SINGLE SIGNAL.
  //
  // THE INCIDENT, NAMED SO NOBODY HAS TO GUESS WHAT THIS COSTS. On 2026-09-10 the owner watched
  // this pane twice, on two consecutive builds, show a COMPLETELY BLANK WHITE RECTANGLE — no app,
  // no loading state, no words — while the chat beside it said "Working on your app" and the
  // workspace called the preview live. The backend log has the whole chain: the container started
  // at 13:44:06 and the pane framed it 7,020ms later, which is inside a fresh Next.js app's very
  // first route compile, so the document that arrived was empty. Nothing covered it, because the
  // cover was derived from `compileState` ALONE and `compileState` was `unknown` for the entire
  // run — the control plane could not read that container's HMR socket at all
  // (`compile_signal_protocol_drift reason=no_recognised_frame`; the deployed supervisor image
  // predated the frame shape the reader expects). One signal was unavailable, and the pane
  // answered by confidently presenting a blank rectangle as the citizen's app.
  //
  // SO AMBIGUITY DENIES. Deriving `covered` from the verdict alone made "no verdict" mean "show
  // the bare frame, confidently" — the same fail-open mistake as `/dev/status.ready` counting a
  // 404 as an answer. A frame is not shown bare unless something POSITIVE says there is a document
  // worth showing, and there are exactly two things that can say it:
  //
  //   · `compileState === 'clean'` — the platform asked the container what it compiled and got an
  //     answer. Evidence outright, and the only kind of it that works mid-turn.
  //   · NO TURN RUNNING — whatever is in the frame is then the app as it stands, and the citizen
  //     is entitled to look at it. This arm is what stops the fix becoming a wait card that never
  //     resolves for the whole fleet whose image predates the compile signal, which would be a
  //     worse failure than the one being fixed: a blank pane at least ends when you reload.
  //
  // `frameLoaded` IS NOT EVIDENCE ON ITS OWN, and this file already records why: a blank 502 from
  // the in-container proxy fires `load` exactly as a 200 does. That is not a hypothetical here —
  // it is precisely what fired at 13:44:12.
  //
  // A RUNNING TURN WITH NO CLEAN VERDICT IS THEREFORE COVERED, which is the reported case and
  // nothing more exotic than it: the app is being written right now, so the document in the frame
  // is at best a snapshot of a half-written app and at worst — as it was that morning — nothing at
  // all. The price is paid by a citizen whose container is too old to report: their working app
  // sits behind a wait card for the length of a turn. That trade is deliberate. A wait card ends
  // when the turn does; a white rectangle ends when the person gives up on the product.
  //
  // AND IT COMES OFF ON ITS OWN, WITHOUT A RELOAD. `turnRunning` falling clears this arm in the
  // same render, and the auto-reload nonce above re-requests the document on exactly that edge (a
  // turn ending over a live preview, or provisioning/building → ready), so the first paint after a
  // build is a FRESH document rather than the stale one this cover was hiding.
  //
  // DO NOT "SIMPLIFY" THIS BACK TO READING `compileState` ALONE. That single-signal version is the
  // one that failed in front of the owner, twice, and it fails SILENTLY: a supervisor can always
  // be older than the control plane, a socket can always drop, a connect can always land on a
  // protocol it does not recognise — and every one of those reads as `unknown`. This predicate
  // has to keep standing when the compile signal is wrong, absent, or lying.
  // AND IT IS `showFrame`, NOT `frameLoaded`, THAT ARMS THIS — the correction that closed the
  // last hole. Gating on `frameLoaded` left the worst case uncovered: a frame that never finishes
  // loading is neither loaded nor covered, so the citizen watches the EMPTY IFRAME AREA, which is
  // the same white rectangle by another route. Measured on 2026-09-10 against a real build: the
  // app root answered `502` with a zero-byte body, `load` had not fired, and the pane showed bare
  // white with the build eight steps in. A document nothing vouches for should be hidden from the
  // moment we decide to frame it, not from the moment it happens to finish arriving.
  const documentIsVouchedFor = compileState === 'clean' || !turnRunning
  const flyingBlind = showFrame && !documentIsVouchedFor
  // AND A `building` COVER LOSES TO A VOUCHED DOCUMENT ONCE ITS READER HAS GONE DARK. The verdict
  // latches on `building`/`failed` and only an affirmative `clean` clears it — right while the
  // signal is alive, and a permanent lie once it is not: the compile reader drifting to `unknown`
  // after a `building` is exactly the fleet-wide failure the handoff records twice, and under it a
  // page the citizen's own browser has reported as showing sat behind "Putting this page
  // together…" with no stall card, no escalation and no way out. A compile takes seconds, so a
  // reader that reported one and then could not decide has almost certainly missed its end, and
  // the document's own word is the better evidence. NARROWLY, and each limit is deliberate:
  //   · only a `building` cover yields. `failed` is a state, not a moment, and a vouch earned
  //     before the broken change must never be what takes its cover down — only `clean`, a new
  //     app, or the citizen's Reload does that.
  //   · only an ANSWERED read that could not decide (`unknown`) counts as dark. `null` is this
  //     pane's own "nothing reported", which every other line here treats as no evidence at all.
  //   · mid-turn `flyingBlind` still covers regardless.
  const verdictHasGoneDark = compileState === 'unknown'
  const covered =
    (coveredByVerdict && !(verdictCover === 'building' && verdictHasGoneDark && frameVouched)) ||
    flyingBlind

  // The cover only exists over a frame. Everything above it in the precedence chain — which is now
  // just the reconnecting cover, the other three having been the workspace verdicts this file gave
  // up — already replaces the frame entirely, and `showFrame` is false in every one of those
  // states, so this single conjunction expresses the whole of `showReconnecting > cover`.
  //
  // AND A CONFIRMED REVERSION COVERS UNCONDITIONALLY. Every other reason to cover is a fact
  // about a COMPILE, so it is right that they defer to a compile signal that says clean. This one
  // is a fact about the workspace: the app behind the frame is not the citizen's app, and a clean
  // compile of the starter template is exactly the state that would otherwise leave it revealed.
  const showCover = showFrame && (covered || workspaceLost)

  // …and the escalation, exactly once.
  //
  // Armed off `covered` — THE REASON TO COVER — rather than off `showCover`, which folds in the
  // frame and reconnect context. The citizen is being told how long their CHANGE has been coming
  // together, and that clock does not restart because a dev-server blip briefly put the
  // reconnecting card in front of the cover. Off `showCover` it did: a flicker reset the countdown
  // mid-wait, so a genuinely slow build could keep resetting to the shorter wording forever.
  //
  // AND IT ARMS FOR THE FAIL-CLOSED ARM TOO, which is the change and is deliberate: a cover raised
  // because nothing vouches for the document (`flyingBlind`) is a wait exactly like the others,
  // and it is in fact the longest of them — a first build with no compile signal is the case that
  // most needs the escalated sentence rather than twenty seconds of the same one.
  const [holdingSlow, setHoldingSlow] = useState(false)
  useEffect(() => {
    // SCOPED TO THE TURN AS WELL AS TO THE COVER, and the turn half is what makes it honest.
    // The escalated wording is a claim about how long THIS change has been coming together, and
    // `covered` does not fall between turns — a failed turn leaves the compile state at `failed`,
    // so a cover raised twenty seconds into turn 1 was still armed when turn 2 began and the new
    // turn opened by telling the citizen it was already taking longer than usual.
    if (!covered || !turnRunning) {
      setHoldingSlow(false)
      return
    }
    const t = setTimeout(() => setHoldingSlow(true), HOLDING_ESCALATE_MS)
    return () => clearTimeout(t)
  }, [covered, turnRunning])

  // WHICH SENTENCE THE COVER IS TELLING THE TRUTH WITH.
  //
  // The holding wording is a claim about work in progress, so it holds only while a turn is
  // actually running. When one ends — with the change made or not — the claim expires with it,
  // and a cover still saying "putting the latest change together" IS the progress state that runs
  // forever: the citizen's only way to learn the build was over would be to wait long enough to
  // stop believing it.
  //
  // The escalation is inside the running arm rather than beside it, because "taking longer than
  // usual" is the same claim with more emphasis — if the first sentence has expired, so has this.
  //
  // AND A CONFIRMED REVERSION OUTRANKS EVERYTHING, running turn or not. It is the only one of
  // these that says the document in the frame is not the citizen's app at all; the others all
  // describe their own app, mid-change.
  const coverText = workspaceLost
    ? WORKSPACE_REVERTED_TEXT
    : turnRunning
      ? holdingSlow
        ? HOLDING_SLOW_TEXT
        : HOLDING_TEXT
      : compileState === 'failed'
        ? IDLE_BROKEN_TEXT
        : IDLE_BUSY_TEXT

  // THE PING: ask the document in the frame to vouch. Refs only, so the one function serves the
  // `load` handler, the wait's timer and the heartbeat without any of them capturing a stale frame.
  const pingFrame = useCallback((): void => {
    const target = frameRef.current?.contentWindow
    const origin = previewOriginRef.current
    if (!target || !origin) return
    try {
      target.postMessage({ type: PING_TYPE }, origin)
    } catch {
      // A detached or mid-navigation window. The spontaneous beacon still arrives on its own.
    }
  }, [])
  // Bumped by the wait each time it asks, so the wait re-arms for the answer.
  const [askedAgain, setAskedAgain] = useState(0)

  // THE VOUCH WAIT. A frame that has not vouched is ASKED first, re-requested only if it ignores
  // the asking, and labelled once the re-requests run out — see the timers at the top of this
  // file. Nothing in here reveals. The timer re-reads the vouch at fire time: a beacon that lands
  // a paint before the deadline must not have the document that sent it torn down underneath it.
  useEffect(() => {
    if (!showFrame) {
      // The frame is coming down (a crash, a teardown). Forget every verdict and the asking
      // budget: whatever comes back is a brand-new element that has to earn its reveal again. The
      // RE-REQUEST budget stays — it belongs to the address, and a dev server that flaps must not
      // buy a container that can never answer three more fetches per flap.
      setLoadedKey(null)
      setVouchedKey(null)
      setStalledKey(null)
      loadedKeyRef.current = null
      vouchedKeyRef.current = null
      aliveKeyRef.current = null
      pingsRef.current = 0
      alivePingsRef.current = 0
      return
    }
    // Do not count the wait while the cover is up. Under the cover we know exactly why the
    // document has not vouched (the app is compiling, or it failed to, or a turn is rewriting it),
    // and asking again would only get the same silence; worse, a timer left running lands the
    // instant the cover clears, so the pane would answer a recovery with "taking longer than
    // usual". Any progress made while covered is dropped — the stall verdict AND the asking
    // budget — so the wait restarts honestly from the uncover: asked again first, never fetched
    // again first.
    if (showCover) {
      setStalledKey(null)
      pingsRef.current = 0
      alivePingsRef.current = 0
      aliveKeyRef.current = null
      return
    }
    if (frameVouched) {
      // THE HEARTBEAT. A revealed page can go blank with no `load` for the pane to see; asked
      // slowly, it says "painting" and the listener above takes the reveal back. Nothing here
      // reveals, and a page that has nothing to add says nothing.
      const beat = setTimeout(() => {
        pingFrame()
        setAskedAgain((n) => n + 1)
      }, HEARTBEAT_MS)
      return () => clearTimeout(beat)
    }
    if (frameStalled) return
    const wait = frameLoaded ? VOUCH_AFTER_LOAD_MS : FRAME_LOAD_CAP_MS
    const t = setTimeout(() => {
      if (vouchedKeyRef.current === frameKey) return
      if (aliveKeyRef.current === frameKey) {
        // Alive at the last ask: ask again, never fetch again — and spend the mark, so the answer
        // to THIS ask is what keeps it alive. Bounded to the label; the document's own beacon
        // still reveals it after that.
        aliveKeyRef.current = null
        if (alivePingsRef.current < ALIVE_PINGS_BEFORE_STALL) {
          alivePingsRef.current += 1
          pingFrame()
          setAskedAgain((n) => n + 1)
          return
        }
        setStalledKey(frameKey)
        return
      }
      // CHARGED FOR THE ATTEMPT, not the delivery: a ping that could not leave (no window, no
      // origin, a throwing post) must still move this wait toward its label, or a frame nothing
      // can reach would be asked forever and never once called slow.
      if (pingsRef.current < PINGS_BEFORE_RELOAD) {
        pingsRef.current += 1
        pingFrame()
        setAskedAgain((n) => n + 1)
        return
      }
      if (vouchRetriesRef.current < VOUCH_RETRY_LIMIT) {
        vouchRetriesRef.current += 1
        setAutoReloadNonce((n) => n + 1)
        return
      }
      setStalledKey(frameKey)
    }, wait)
    return () => clearTimeout(t)
  }, [showFrame, frameKey, frameLoaded, frameVouched, frameStalled, showCover, askedAgain, pingFrame])

  // ONE honest wait, running from "no URL yet" all the way to the framed document vouching for
  // itself. It used to be destroyed the instant `previewUrl` arrived, which is precisely when the
  // 5-7s first-route compile begins: the spinner vanished and left an unlabelled blank white card
  // at the exact moment the citizen had been told their app was ready.

  // THE COVER'S STATE, and the whole safety property is in which signals move it.
  //
  // Only an AFFIRMATIVE `clean` takes the cover down. `building` and `failed` raise it;
  // `unknown` HOLDS whatever is currently showing, and `null` (nothing reported yet) leaves it
  // alone too. Absent-reads-as-clean is the one behaviour that must never exist here: today its
  // consequence is uncovering a red screen, and once the framework's own overlay is disabled
  // for new apps its consequence is uncovering a BLANK one. Holding is the fail-closed answer
  // in both directions — it never raises a cover over a healthy app either.
  //
  // Deliberately NOT tied to the frame's lifecycle. A remount does not reset this: the container
  // re-reports within a poll, and clearing on remount would mean a frame swap silently uncovers
  // a broken app for a second. The verdict is about the APP, not about this DOM node.
  //
  // ALL OF WHICH DESCRIBES `coveredByVerdict`, WHICH IS NOW HALF OF THE COVER. Every word above
  // stays true and none of it was enough: holding on `unknown` is fail-closed only while something
  // is showing to hold, and during a first build nothing is — so holding resolved to revealing.
  // The other half is `flyingBlind`; see the long block above `covered`, and the incident it names.

  // THE REVEAL, AND THE ONE THING IT RESTS ON. `frameVouched` is the framed document's own word
  // that it is showing something in this browser — the beacon described at the top of this file.
  // `load` is not in this expression and must never return to it: it fires for a 500 exactly as
  // for a 200, this pane cannot read a cross-origin status, and the in-container proxy emits its
  // handle-block headers even on the 502 it returns when the dev server is down. Three predicates
  // over the signals this pane is HANDED were tried on 2026-09-10 and each traded one fail-open
  // for another; the document is the only witness on the right side of the network.
  //
  // `!covered` is the other half, and two properties fall out of it worth naming: a verdict that
  // flips to failed after a reveal RETRACTS it — the app goes back to hidden and the cover
  // explains — and an UNKNOWN verdict does not, because `covered` holds on unknown rather than
  // moving.
  //
  // This unit controls opacity and nothing else: it renders no overlay, and every visible surface
  // above the frame belongs to the cover.
  //
  // WHAT THIS COSTS, stated rather than left to be discovered: a container running an image older
  // than the beacon never vouches. Its app is asked twice, fetched again up to three times
  // (VOUCH_RETRY_LIMIT, per address — a turn edge afterwards costs one fetch and two pings, never
  // a fresh budget), and then sits behind the labelled stall card until it is next launched — the
  // beacon rides the image and reaches every app on its next provision or restore. That is the
  // trade the owner chose over a fourth guess: a labelled wait that ends on the next launch,
  // never a white rectangle presented as the citizen's app.
  const revealed = frameVouched && !covered
  // …and whether the citizen can actually SEE it, which `revealed` alone is not: `showCover`
  // folds in the reversion cover, and a reverted workspace draws its card over a frame the compile
  // verdict has no quarrel with. ONE expression for the two consumers that must agree — the
  // attribute the harness reads and the stop-clock below — so they cannot drift apart.
  const seenByCitizen = revealed && !workspaceLost
  // Announce that reveal ONCE per document. Keyed on the frame key rather than on `revealed`
  // alone, because a verdict that flips to failed RETRACTS the reveal and a later re-reveal of
  // the same document is not a second first-view. A reload (a new nonce, so a new key) does
  // announce again; the caller's own mark is idempotent, so the two guards agree rather than
  // either one having to be perfect.
  //
  // AND IT READS `seenByCitizen`, NOT `revealed`, because `revealed` is NOT "the cover is down".
  // `showCover` is `covered || workspaceLost` while `revealed` reads only `covered`, so a
  // confirmed reversion leaves the frame at full opacity UNDER a cover that says the document in
  // it is not the citizen's app — visually correct (the cover is on top) and, read naively, a
  // reported first view of an app the citizen cannot see.
  const announcedRevealOf = useRef<string | null>(null)
  useEffect(() => {
    if (!seenByCitizen || !frameKey) return
    if (announcedRevealOf.current === frameKey) return
    announcedRevealOf.current = frameKey
    try {
      onRevealed?.()
    } catch {
      // The pane owes the caller nothing, and that has to include not dying for them. There is
      // no ErrorBoundary anywhere in this portal, so a throw from a caller's telemetry would
      // white-screen the builder — a measurement failing the thing it measures, which is the one
      // outcome this whole surface is built to avoid.
    }
  }, [seenByCitizen, frameKey, onRevealed])
  // …and the STALL, told as an edge in BOTH directions rather than once per key. The caller holds it
  // as "is the framed app stuck right now" and asks the server on its own schedule, so it has to hear
  // the label come down as well as go up. Starts at `false`, so a pane that never stalls never calls
  // — and nothing is sent on unmount: the caller clears its own copy on any reading that takes the
  // frame away.
  const reportedStall = useRef(false)
  useEffect(() => {
    if (reportedStall.current === frameStalled) return
    reportedStall.current = frameStalled
    try {
      onStallChange?.(frameStalled)
    } catch {
      // The pane owes the caller nothing — the rule the reveal above states in full.
    }
  }, [frameStalled, onStallChange])
  const framePending = showFrame && !frameVouched && !frameStalled
  // `starting` joins the wait WITHOUT a `status` term, deliberately. The other two arms both key
  // on the build lifecycle, and a relaunch has no build lifecycle at all — it carries the status
  // of the turn that ended, so any `provisioning`/`building` test would exclude the one case that
  // most needs the wait. The server said a start is in flight; that is the whole condition.
  const showLoading =
    framePending ||
    starting ||
    (!isTerminal && !previewUrl && (status === 'provisioning' || status === 'building'))
  // ONE sentence for the wait, chosen once and read by both the card and the live region, so the
  // two can never drift into naming the same situation differently. `starting` borrows the frame's
  // own cold-start line rather than earning a fourth: to the citizen it is the same fact.
  const loadingText =
    framePending || starting ? FRAMING_TEXT : ((status && LOADING_TEXT[status]) ?? 'Building your app…')

  // ONE announcement for the whole pane, read out of a region that is ALWAYS mounted (below).
  // `aria-busy` announces exactly nothing, so it is no substitute. Mounting a live region
  // together with its text announces inconsistently across screen readers, so the region is
  // permanent and only this string changes.
  //
  // Ordered by what is actually on screen, most specific first. `polite` throughout — none of
  // these is an error, and `assertive` is reserved for the ones that are (the save failure keeps
  // its own `role="alert"`).
  const announcement = showReconnecting
      ? 'Reconnecting to your preview…'
      : showCover
        ? coverText
        : frameStalled
          ? SLOW_TEXT
          : showLoading
            ? // Covers `starting` too, and is the reason no arm for it appears further down this
              // chain: it is drawn on screen now, so it is announced from the state that draws it.
              loadingText
            : // THE LIVE CLAIM IS EARNED NOW, NOT ASSUMED.
              //
              // This arm used to read `revealed ? 'Your app preview is live' : ''`, and that was
              // false. `revealed` was `frameLoaded && !covered` then, and `frameLoaded` is the
              // framed document's `load` event — which fires for a 500 exactly as it does for a
              // 200, on a cross-origin frame whose status code this pane cannot read, and which
              // the in-container proxy emits even on the 502 it returns when the dev server is
              // down. So the one sentence in this chain making a claim about the APP rested on the
              // one signal carrying no health term at all: a citizen using a screen reader was
              // told their preview was live over a framework error screen. `revealed` now reads
              // `frameVouched && !covered` — the framed document's own beacon — which is a reason
              // to keep this claim earned here, not a licence to go back to assuming it.
              //
              // It is not simply deleted, because deleting it leaves the success path SILENT while
              // the failure path speaks — a screen-reader user hears the wait end and then nothing,
              // and cannot tell "it worked" from "it stopped announcing". The failure verdict gets
              // a sentence; so should its opposite.
              //
              // So the claim is made only where there is evidence for it, from the two signals that
              // carry one: `serving` (a container is answering at this address) and a `clean`
              // compile verdict (the build the platform actually asked about). BOTH are required
              // and neither is `revealed`.
              //
              // `unknown` and `null` say NOTHING — that is the rule, and the reason this is a
              // `=== 'clean'` test rather than `!== 'failed'`. "Not failure" read as success is
              // exactly the collapse that republishes the false live claim on the reload where
              // nothing is serving.
              //
              // AND THE GATE IS LEFT EXACTLY AS IT IS, INCLUDING WHAT IT DOES NOT PROVE ON ITS OWN.
              // The serving stamp does not reach two of `serving`'s three arms: `fromProject`
              // consults the preview-state poll, but `fromTurn`/`fromSession`
              // (`utils/previewAddress.ts`) are a live turn's own word for it and never see the
              // stamp. So it is NOT true that this sentence became honest because its own inputs
              // carry proof. It is honest because `AppPane` will not mount this component at all
              // unless the workspace reading is `running` — the frame VETO is what holds it, one
              // level up, in a file this one cannot see. Written down because it is load-bearing
              // and invisible: weaken that veto and this claim goes back to being unearned on the
              // turn-sourced arms, with nothing here to catch it.
              revealed && serving && compileState === 'clean'
                ? 'Your app preview is live'
                : ''

  // What a `load` means here: something finished arriving at this key. The FIRST load of a key
  // records itself and asks — the ping — and takes nothing back, because a page's beacon
  // routinely beats its own `load` (hydration finishes while an image or a font is still
  // arriving) and a vouch already in hand is not in question. A SECOND load at the same key is a
  // different document: the page reloaded itself from the inside (a dev-server restart does this)
  // and the pane saw only the `load`, so the vouch is taken back on the spot and the new document
  // is asked to earn it — the moment a bodyless 502 lands in a frame that had vouched is covered,
  // not revealed. Read off the ref, not the render: two loads can land inside one paint.
  const onFrameLoad = () => {
    const reloaded = loadedKeyRef.current === frameKey
    loadedKeyRef.current = frameKey
    setLoadedKey(frameKey)
    if (reloaded) {
      // A NEW DOCUMENT, so a new wait: the asking budget and the clock belong to the document that
      // is being asked, and the one that just replaced itself has not been asked at all yet.
      vouchedKeyRef.current = null
      aliveKeyRef.current = null
      pingsRef.current = 0
      alivePingsRef.current = 0
      setVouchedKey((current) => (current === frameKey ? null : current))
      setAskedAgain((n) => n + 1)
    }
    pingFrame()
  }

  return (
    <div className="flex flex-col h-full">
      {/* NO TOOLBAR ROW HERE: the boards draw ONE row for the whole workspace, under the navbar
          and above both columns. A row inside the pane exists only once something is framed,
          which leaves a project with nothing built without a device switcher, without Save and
          without a way to open the app in a tab. */}

      {/* Main area */}
      <div className="flex-1 flex overflow-hidden relative">
        <div className="flex-1 bg-[#e8edf2] flex p-4 overflow-auto">
          {/* NO EMPTY STATE HERE, and it is structurally unreachable rather than merely unused:
              `AppPane` mounts the host only when the address resolver returned a URL. The
              sentence a citizen reads when there is nothing to frame is `AppPane`'s, drawn from
              the one computed workspace state, so a pane sentence has exactly one author. */}

          {/* The dev-server PROCESS crashed after framing. A DISTINCT visual from the
              "Building…" blue bouncing dots (a spinning glyph + warning tint) so a dead frame never
              reads as "still building". Self-heals when the server restarts (a fresh preview_ready). */}
          {/* No `aria-live` of its own: the persistent status region at the bottom of this pane
              speaks for every state, and two live regions describing the same situation announce
              it twice. `aria-busy` stays — it is a property, not a speech. */}
          {showReconnecting && (
            <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center" aria-busy="true">
              {/* Missed by the first sweep, which matched `<Loader2` and nothing else — so the
                  one wait a citizen sees when their connection drops was still freezing under
                  `prefers-reduced-motion`. The icon carries meaning here (reconnecting, not merely
                  waiting), so it travels as `icon`; the cadence travels with it. */}
              <BusyGlyph size={26} icon={RotateCcw} durationMs={1400} className="text-warning" />
              <p className="text-sm font-semibold text-neutral">Reconnecting to your preview…</p>
              <p className="text-xs text-neutral/60 max-w-xs leading-relaxed">
                The preview server restarted. This usually reconnects on its own in a moment.
              </p>
            </div>
          )}

          {/* NO "PREVIEW UNAVAILABLE" CARD AND NO "NO LONGER RUNNING" CARD, and their absence is
              the change rather than an omission. Between them they drew four headlines and six
              bodies — asleep, taken by a sibling project, never built, disconnected, ended — and
              every one of them was a verdict on the WORKSPACE, which `workspace/workspaceState.ts`
              computes once and `AppPane` draws once. Two authors for one sentence is how a pane
              ends up saying "Your workspace is asleep" over an app the map is at that moment
              calling up. What is left on this side of the wall is a frame, and covers over it.

              WHERE THEIR NEWS LIVES NOW, so nothing is merely dropped: a sleeping workspace is the
              map's "Your app is saved." with the press that brings it back; a sibling project
              holding the one workspace is the held card that names it; a project with nothing built
              is the composer's own invitation; and a dev server that died and did not come back
              clears the serving stamp on the server, so the reading stops being `running` and the
              map draws the wait. None of those readings mounts this component at all. */}

          {showFrame && (
            // No padding/border here: the iframe's `w-full` below depends on this box's
            // content width being exactly the device pixel width, with nothing to subtract.
            <div
              data-testid="device-card"
              /* THE REVEAL, WRITTEN WHERE IT CAN BE SEEN. Opacity is a class; a probe reading the
                 composed screen needs a fact, and "can the citizen see this frame" is the fact the
                 2026-09-10 harness could not ask — it read the framed document directly and counted
                 a blank one under the wait as a blank one on screen. `seenByCitizen`, not
                 `revealed`: the reversion cover hides a frame `revealed` would call visible. */
              data-revealed={seenByCitizen ? 'true' : 'false'}
              style={{ width: DEVICES[device].width ? `${DEVICES[device].width}px` : '100%' }}
              // `width` is deliberately EXCLUDED from the transition (no `transition-all`, no
              // `transition-[width]`): animating layout width genuinely resizes the cross-origin
              // iframe on every intermediate frame of the sweep, firing a burst of real `resize`
              // events / ResizeObserver callbacks inside the framed app — components that latch a
              // dimension on their first callback can settle on a transient mid-sweep value
              // instead of the real device width. Scoping the transition to paint-only properties
              // makes the box snap to its target width in one paint; still visually smooth.
              // `opacity` IS in the transition (it is paint-only, so it costs the framed document
              // nothing) — that is the fade, and until it runs the card is opacity-0 with the
              // labelled wait sitting over it. Hidden, not unmounted: the frame must be mounted for
              // its document to run at all, and only that document's beacon reveals it.
              className={`shrink-0 mx-auto h-full transition-[box-shadow,border-radius,opacity] duration-300 rounded-xl overflow-hidden shadow-lg bg-white relative ${revealed ? 'opacity-100' : 'opacity-0'}`}
            >
              {/* THE THREE CHIPS THAT USED TO SIT HERE ARE GONE.
                  "Still working…", "Build complete — your app is live below" and "Showing your
                  last saved version — the most recent build failed" were three sibling overlays
                  sharing ONE rectangle: `absolute top-3 left-1/2 -translate-x-1/2 z-10`. That
                  rectangle is where every app this platform builds puts its own navigation, so the
                  platform was drawing its chrome across the citizen's app — three nav items
                  unreadable, a focus ring behind our card, and a fill measuring 1.01:1 against the
                  app header. The justification is GEOMETRIC, so it applied identically to all
                  three; deleting only the middle one would have left the same collision in the
                  iterating and restored states.

                  WHERE EACH ONE'S NEWS LIVES NOW, so nothing is merely dropped:
                   · "Still working…"    — the transcript's own activity line, which narrates the
                                           running turn where the citizen is already reading it.
                   · "Build complete"    — the turn's closing message. The pane frames a live app;
                                           it no longer also asserts a build outcome.
                   · the restore notice  — HAS NO RENDERER, and that is stated rather than hidden.
                                           Nothing could produce it (both publishers hardcoded it
                                           `false`), so it still needs a new home — the toolbar row
                                           or a transcript line — not this rectangle.

                  The pane still speaks: everything it has to say is in the cover, the waits and the
                  permanent live region below, none of which are drawn over the app's own controls
                  while it is revealed. */}
              <iframe
                /* Key on `frameKey` = url + reload nonce. A NEW url still remounts exactly as it
                   always did; the nonce adds the case the url alone cannot express — same
                   container, repaired app. A plain re-render still keeps the same DOM node,
                   so the framed app's HMR websocket is not leaked on every status tick. */
                key={frameKey}
                /* THE SAME VALUE, WRITTEN WHERE IT CAN BE SEEN. A React `key` reaches no DOM
                   attribute, so every reason this frame remounts was invisible from outside —
                   including to a test. A guard that only suppresses a remount AT MOUNT is then
                   unprovable by construction: `render()` flushes the effect before it returns, so
                   a wrongful first bump is already folded into the node you get back, and the test
                   asserting it (a node-identity compare across a later re-render) passed happily
                   with its guard deleted. This attribute is the seam that makes the nonce a fact
                   about the document rather than a fact about React's internals — and it answers
                   "why did my preview just reload?" in devtools, which was previously unanswerable
                   without a debugger. */
                data-frame-key={frameKey}
                /* The identity the inbound message gate compares `e.source` against.
                   React attaches and detaches this alongside `key`, so a remount or an unmounted
                   pane nulls it on its own — which is the fail-closed state, not a gap. */
                ref={frameRef}
                src={previewUrl}
                /* NOT the reveal — see `onFrameLoad`: it records the load against the KEY, sends
                   the ping, and starts the clock on a document that had vouched before. */
                onLoad={onFrameLoad}
                className="w-full h-full border-0"
                title="App Preview"
                /* FROZEN: the preview is a genuinely CROSS-ORIGIN sandbox frame (the sandbox's
                   own FQDN, served by its Caddy with `frame-ancestors <portal-origin>`), so the token
                   list ADDS `allow-same-origin` — the real `next dev` app must run as its own
                   sandbox-FQDN origin (storage, the HMR websocket, RSC fetches). Safe BECAUSE the
                   frame is genuinely cross-origin: SOP still walls the app off from the portal, and the
                   cross-origin barrier stops the framed script stripping its own sandbox.
                   `allow-top-navigation*` / `allow-popups` stay WITHHELD — the framed app is unreviewed,
                   agent-generated, self-heal-loop code (no top-nav hijack of, nor popup-phishing of, the
                   portal tab). Do not widen this sandbox token list without a deliberate security review. */
                sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"
              />
            </div>
          )}
        </div>

        {/* The wait, rendered OVER the pane rather than beside it. It has to co-exist with a
            mounted-but-unrevealed frame (the frame must be loading for `load` to ever fire), and
            the device card owns the pane's whole content width, so a sibling would be squeezed to
            nothing. Same anchor for both waits, so they can never be on screen at once. */}
        {/* THE COVER, over the frame and under nothing. Same anchor and same calm
            bouncing-dots treatment as the wait below it, DELIBERATELY: the citizen is in one
            situation ("my app has not opened yet") and the spinner-plus-warning tint two blocks
            down means something else — a dev server that is genuinely down. Do not borrow it.

            Its precedence is expressed by `showFrame` (everything above it in the chain
            unmounts the frame) plus the `!showCover` guards on the two waits below. The cover
            shows exactly one thing. */}
        {showCover && (
          <BouncingWait className="text-center px-6 max-w-sm">
            {coverText}
          </BouncingWait>
        )}

        {showLoading && !showCover && <BouncingWait>{loadingText}</BouncingWait>}

        {/* The bounded degradation: the document never vouched, through every re-request, so say
            so — while leaving the frame MOUNTED underneath. Unmounting it would make the stall
            permanent by construction (the beacon it is waiting for could never arrive), so this
            says "slow", not "dead" — a beacon that lands after the re-requests ran out still wins
            and reveals. */}
        {/* …and this degraded twin loses to the cover for the same reason the wait above does:
            when the cover is up we KNOW why the frame has not loaded (the app is compiling, or
            it failed to), and this card's "relaunch it" advice would be wrong. Two waits never
            share the screen — the rule this file already kept, extended to the third. */}
        {frameStalled && !showCover && (
          <div className="absolute inset-0 z-20 bg-[#e8edf2] flex flex-col items-center justify-center text-center px-6">
            <div className="w-16 h-16 rounded-2xl bg-gray-100 flex items-center justify-center mb-4">
              {/* THE STALL CARD IS THE ONE PLACE A FROZEN SPINNER IS ACTIVELY MISLEADING: it is
                  already saying the load is slow, so a motionless glyph beside that sentence reads
                  as confirmation the thing died. Under `prefers-reduced-motion` this renders no
                  spinner at all — see `ui/Waiting.tsx`. */}
              <BusyGlyph size={26} durationMs={1800} className="text-warning" />
            </div>
            <p className="text-sm font-semibold text-neutral mb-1">{SLOW_TEXT}</p>
            {/* THE COPY NAMES NO CONTROL THIS CARD DOES NOT HAVE. The one start control lives in
                `AppPane`, and an instruction pointing at nothing is worse than no instruction:
                each sentence keeps its FACT and drops the direction. */}
            <p className="text-xs text-neutral/60 max-w-xs leading-relaxed mb-4">
              {'It will appear here the moment it loads.'}
            </p>
          </div>
        )}

        {/* THE pane's live region — mounted always, empty when there is nothing to say.
            Permanent on purpose: inserting a live region together with its text announces
            inconsistently (some readers miss it entirely), so the element outlives every state
            and only its text changes. Visually hidden because every state above already SAYS
            what it is on screen; this exists for the reader that cannot see the moon icon go
            up. `polite`, never `assertive` — none of these states is an error, and the one
            that genuinely is (a failed save) keeps its own `role="alert"` so it still cuts in. */}
        <p role="status" aria-live="polite" className="sr-only">
          {announcement}
        </p>
      </div>
    </div>
  )
}
