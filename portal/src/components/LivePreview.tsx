import { useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { Loader2, Moon, PowerOff, RotateCcw, WifiOff } from 'lucide-react'
import type { BuildSessionStatus } from '../utils/buildSessionTypes'
import type { PreviewLifeState } from '../utils/buildSessionApi'
import type { CompileState } from '../utils/compileState'

// Device-card widths drive the preview's REAL rendered pixel width (an inline style on
// the wrapper, not a Tailwind max-width class) so the framed cross-origin doc's own media
// queries evaluate against the TRUE viewport width — the actual fix for "doesn't look like
// a real phone" (#42 device switcher). The iframe itself stays `w-full` (100% of the
// wrapper) rather than repeating the pixel value: `width` is excluded from the wrapper's
// own transition (see the device-card className below), so it snaps to its target in one
// paint — `w-full` just means the iframe always matches the card's width exactly, with no
// separate inline value of its own to fall out of sync. `width: null` = full width
// (Desktop, unchanged). Height is deliberately NOT constrained per mode — it stays bounded
// to the pane (`h-full`, as today) with the iframe's own native scrollbar handling taller
// content, matching the Lovable/v0 reference: a bounded-height card that scrolls
// internally, never a fixed-aspect-ratio clip.
// THE TABLE ITSELF MOVED UP WITH ITS CONTROL (plan 002, U2) — it is `WorkspaceToolbar`'s now,
// because the switcher that picks a width lives in the shell's toolbar row. This component still
// reads the widths, so it imports the one table rather than keeping a second copy that could
// disagree about what "Tablet" means.
import { DEVICES, type DeviceName } from './workspace/devices'

// U5 — bound the wait for the framed document's own `load`. The reveal itself is gated on that
// event and nothing else (see `loadedUrl` below); this cap exists only so a frame that NEVER
// loads still resolves to a labelled state instead of an eternal spinner. Same posture as
// RECONNECT_CAP_MS — bound it, then degrade to something that says what happened — with its own
// knob, because it is timing a different thing: a first Turbopack route compile measured at 5-7s
// on a cold sandbox, plus whatever a server-rendered root route spends on the per-project
// database. 20s sits well past both, so reaching this cap means something is genuinely wrong.
const FRAME_LOAD_CAP_MS = 20000
// F8/U5 — bound the reconnecting state AFTER a completed build (its build loop is gone, so nothing
// will re-frame a dev server that never recovers): cap it, then collapse to "preview unavailable"
// + Relaunch instead of spinning forever. TUNE against real dev-server restart times.
const RECONNECT_CAP_MS = 20000

// The scheme://host[:port] of an absolute preview URL, or null if unset/malformed. Used to
// VALIDATE inbound postMessage origins (C8 §3). A malformed value fails closed (null → no
// frame trusted, every inbound message rejected).
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
    // Folded into the same fails-closed return as a malformed URL — PR #93 review,
    // security finding 4.
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
// …and then a THIRD wait nobody used to narrate: the URL has arrived and the frame is mounted,
// but the sandbox is still compiling its first route, so nothing has painted. "Building your app"
// is stale by then (the build is done) and silence is the blank white card this unit exists to
// kill — so the wait gets its own honest line.
const FRAMING_TEXT = 'Starting your app…'
// The frame-load wait's escalated sentence, said in both places it belongs (the card and the live
// region). It was shared with a second, relaunch-side wait that has since gone with its flag; the
// reason it is one string rather than two survives that — the citizen is in ONE situation ("my app
// has not opened yet"), and naming it twice depending on which internal timer happens to be running
// is the pane talking about itself instead of to them.
const SLOW_TEXT = 'Your app is taking longer than usual to open'

// R16/R18 — THE COVER. What the citizen sees instead of the framework's full-screen compile-error
// screen, which filled the preview for ~66 seconds in each of three builds during the 2026-08-18
// demo.
//
// WHY A COVER AND NOT A FIX INSIDE THE APP. The app is framed genuinely cross-origin, so this
// pane cannot reach into `contentDocument` — but it can absolutely put its own element on top.
// That is the whole reason this is the right mechanism: it needs no per-app change, no Caddy
// plugin and no edit to a file the build agent could overwrite; it survives a restore by
// construction; and it works identically on every Next version, which makes it the ONLY fix that
// reaches apps already built. A framework env var (a later plan) is defence in depth for new
// apps, not the fix.
//
// THERE IS NO LAST-GOOD-VIEW, and the reason is the same cross-origin wall: the parent cannot
// copy or screenshot the working render either. It would also be actively harmful in the case it
// sounds best in — a citizen whose workspace was wiped would keep watching their app apparently
// rendering, which is the 2026-08-18 failure inverted. The cover shows the holding state, full
// stop.
const HOLDING_TEXT = 'Putting the latest change together…'
// …and the ONE escalation, because a wait that never changes its wording stops being read as a
// wait. Said once and never again — a card that keeps re-narrating itself reads as broken.
const HOLDING_SLOW_TEXT =
  'This change is taking longer than usual — it will appear here as soon as it\u2019s ready.'
// Pinned as a named constant BESIDE `FRAME_LOAD_CAP_MS` so it is testable and changeable in one
// place — not because 20s is measured. The holding-state duration counter is what settles it.
const HOLDING_ESCALATE_MS = 20000

// R13 — WHAT THE COVER SAYS WHEN NO TURN IS RUNNING. "Putting the latest change together…" is
// true for exactly as long as one is; left up afterwards it becomes a progress state that never
// resolves, which is the failure U7 exists to end, reproduced on the other pane.
//
// TWO SENTENCES, BECAUSE THE COVER HAS TWO IDLE CAUSES AND THEY ARE OPPOSITES. `failed` means the
// app is not serving anything usable. `building` means the app is compiling a route right now —
// which the supervisor publishes for any on-demand compile inside a perfectly healthy app, so a
// single "your app stopped running" would be told over a working, completed build.
//
// NEITHER CLEARS THE COVER. Behind it is the framework's error screen today and a blank page once
// that is suppressed, so clearing would trade a lie about progress for a lie about the app.
//
// Deliberately NOT the retraction sentence below. That one means the workspace was lost and a
// restore is coming; promising a restore for a compile error would be a third lie.
const IDLE_BUSY_TEXT = 'Getting your app ready…'
const IDLE_BROKEN_TEXT =
  'Your app isn\u2019t running right now. Send a message describing what you\u2019d like and we\u2019ll get it working.'

// U4/R7 — THE RETRACTION, and it goes on the PANE rather than in the chat because no turn is
// running. Nobody is looking at the transcript; they are looking at what they believe is their
// app, above a message that says it is finished.
//
// IT PROMISES A RESTORE, and unlike every other sentence in this file it is entitled to: the next
// turn's integrity gate finds the same reversion, puts the app back from the last durable copy,
// and says so. That is why the promise is here and not on `IDLE_BROKEN_TEXT`, which describes a
// compile failure that no restore would fix.
//
// It OUTRANKS both idle sentences. "Getting your app ready" over a workspace that has been wiped
// is the exact false-progress claim this whole plan exists to remove.
const STOPPED_RUNNING_TEXT =
  'Your app stopped running and needs to be brought back. Send a message and we\u2019ll restore it.'

/** The headline for a pane whose container is not serving this project — C3 §8.3.
 *
 *  NONE OF THESE IS AN ERROR, and the copy is the whole deliverable of R16/R17. A reclaimed
 *  workspace used to read "Preview unavailable", which describes a platform fault; it is a
 *  sleeping workspace whose work is on durable storage, and the next prompt brings it back. */
/** The three states that mean "no container is serving this project" — `alive` and `unknown` are
 *  pointedly excluded, and so is U13's `starting`: a start already under way is the opposite of
 *  gone, and the server's own action mapping (C3 §10.3) groups it with `alive` as "nothing to
 *  offer, just a wait" — this pane must not invite a "send a prompt" remedy over a container that
 *  is already on its way up. Named once so the copy tables and the render sites all narrow to the
 *  same union instead of each asserting it with a cast (`.claude/rules/fail-first-typescript.md`). */
type GoneState = Exclude<PreviewLifeState, 'alive' | 'unknown' | 'starting'>

const GONE_TITLE: Record<GoneState, string> = {
  asleep: 'Your workspace is asleep',
  slot_taken: 'Another project has your workspace',
  never_built: 'Nothing has been built here yet',
}

/** What the citizen should do about it, and what it costs them (nothing). */
/**
 * Copy for the three not-alive states.
 *
 * `restorable` is honoured, not decorative. This used to promise "nothing is lost" / "your work
 * is saved" UNCONDITIONALLY — but `restorable === false` is a reachable backend state (the server
 * holds neither a recovery slot nor a saved bundle), and telling a builder their work is safe
 * when the server has just said it is not is the one thing this unit exists to stop. The
 * tri-state is deliberate: `null` means the object store was unreachable, so we claim NOTHING
 * rather than guessing in either direction — the same discipline the relaunch affordance already
 * follows ("the claim is made ONLY when the server confirmed a restorable snapshot").
 */
function goneBody(
  state: GoneState,
  occupier: string | null,
  restorable: boolean | null,
): string {
  if (state === 'never_built') return 'Send a prompt and your app will be built and appear here.'

  const reassurance =
    restorable === true
      ? ' Nothing is lost.'
      : restorable === false
        ? ' This project has no saved build yet, so it will start fresh.'
        : ''

  if (state === 'slot_taken') {
    const who = occupier ? `${occupier} is` : 'Another project is'
    return `${who} using your build workspace right now. Send a prompt here and this project comes back.${reassurance}`
  }
  return `It went to sleep while you were away. Send a prompt and it comes back where you left it.${reassurance}`
}

/**
 * `RelaunchAffordance` IS GONE, and its four render sites with it (Plan F, U4).
 *
 * R3 says exactly ONE control starts the app, pressed deliberately, and that control is
 * `components/workspace/StartAppControl.tsx` — rendered by `AppPane` from the one computed
 * workspace state, whose action union contains no destructive verb at all. Four more start
 * buttons scattered through this file's placeholder arms, each with its own copy and its own
 * `hasSavedBuild === true` gate, is the same requirement satisfied five times over — and they
 * spoke a different vocabulary ("Relaunch preview", "Bring it back") from the one the client
 * settled on ("Launch Application"), because "preview" is the developer's word for the thing and
 * the person's word is their app.
 *
 * The COPY around them stayed where it was: these placeholders still explain what happened. What
 * left is the button — and, with this sweep, the whole prop chain that fed it. `onRelaunch`,
 * `relaunching`, `relaunchError` and `lastBuildFailed` are gone from this component, from
 * `PaneView`, and from the session hook that produced them: the first was accepted and never read,
 * so nothing above it could ever fire. The placeholders keep their sentences; what they no longer
 * carry is a not-found arm that only a dead `relaunchError` could select.
 */

/**
 * The calm wait: an opaque full-bleed card with the pane's bouncing dots and one sentence.
 *
 * ONE component for both waits on this pane — the frame-load wait and the compile cover — because
 * their visual identity is the point, not a coincidence: the citizen is in one situation ("my app
 * has not opened yet") and two subtly different cards would be the pane talking about itself. It
 * was two verbatim copies of this markup, kept in step by a comment; this keeps them in step by
 * construction. Deliberately NOT shared with the frame-stall card below, which uses the
 * spinner-plus-warning tint reserved for a dev server that is genuinely down — that one means
 * something different and must stay able to diverge.
 *
 * The two call sites keep their own guards. Only the chrome is shared, never the condition.
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
 * this pane frames that app's genuinely CROSS-ORIGIN `previewUrl` (C8) once the dev server is
 * up. All single-file machinery — the `jsx:preview` fence, `previewCode` threading, the
 * outbound `postMessage` of `{previewCode, config, accessToken, user}`, the `generationStage`
 * progress theater, and the "View Code" source panel — is GONE (U9 already retired the
 * same-origin Babel `/preview`; the app now gets its data credentials server-side at provision,
 * C9 — the portal feeds the app nothing).
 *
 * Driven entirely by the C3 build session:
 *   - `previewUrl` — the sandbox `next dev` root (C3 status / C7 `preview_ready`). Framed once set.
 *   - `status`     — the C3 lifecycle; drives loading / framed / terminal visuals.
 *   - `iterating`  — true while the loop keeps emitting step/log envelopes AFTER the preview
 *                    went live (a refine turn holding at `ready`); shows a subtle overlay.
 *   - `onFrameMessage` — the client-error receiver seam, LIVE as of U13. The inbound `message`
 *                    listener validates BOTH `e.origin` against the preview origin AND `e.source`
 *                    against this pane's own iframe window (the C8 §3 security assertion), and
 *                    forwards only messages that pass both; the conversation surface relays
 *                    them to the harness, where a reported browser crash makes the health verdict not-green.
 *                    The source half is what survives every app sharing one hostname — origin
 *                    alone no longer tells this pane's app from any other app in the document.
 *                    Together they prove PROVENANCE, not content — the shape check lives on the
 *                    receiving side. Note `scripts/skeleton/frame-proof` is a standalone Chromium
 *                    rig with its OWN inline origin guard: it never renders this component, so it
 *                    neither exercises nor regression-catches the gate written here.
 *
 *   - `restoredFromFailedBuild` — a restore of the last SAVED version, because the newest build
 *                    FAILED (server-confirmed); a small
 *                    overlay says so, so older code is never presented as the latest build.
 *   - `completedLive` — the session ended as a SUCCESS and the server pardoned its container
 *                    (#13/R2: it stays up under an idle lease), so `ended` + `previewUrl` means
 *                    "done, preview live" and the pane keeps framing the app instead of collapsing
 *                    to the placeholder. Only stop / force-end / failure / reclaim collapse.
 *   - `reconnecting` — the dev-server PROCESS crashed after the preview was framed (F8/U5, a
 *                    backend `preview_reconnecting` signal — the frontend can't poll /dev/status).
 *                    DISTINCT from the "Building…" loading bounce and from `feedDisconnected` (the
 *                    SSE feed dropping): the pane shows a "Reconnecting…" state over the dead frame
 *                    until a fresh `preview_ready` re-frames. After a COMPLETED build (no loop left
 *                    to recover it) it is BOUNDED — a cap collapses it to "preview unavailable" +
 *                    Relaunch, never an unbounded spinner.
 *   - `hasSavedBuild` — does the PROJECT have a snapshot a Relaunch could actually restore, so
 *                    even a conversation with no build history of its own offers Relaunch from
 *                    the EMPTY state (finding #1: relaunch derives from project-level snapshot
 *                    state, not this transcript). THREE-STATE, and each state means something
 *                    different (N7): `true` = there is one, `false` = confirmed there is not,
 *                    `null` = the server could not reach the object store, so it declines to
 *                    claim anything and this pane says nothing either. Only `true` makes a claim.
 *
 *                    It replaces `projectHasApp`, which keyed on the mere EXISTENCE of an app
 *                    registry row — and that row is minted by PROVISION, before anything is
 *                    built, so every project whose first build failed advertised a saved build
 *                    and then 404'd on the click.
 *
 */
export interface LivePreviewProps {
  previewUrl?: string | null
  status?: BuildSessionStatus | null
  iterating?: boolean
  onFrameMessage?: (data: unknown) => void
  restoredFromFailedBuild?: boolean
  completedLive?: boolean
  hasSavedBuild?: boolean | null
  reconnecting?: boolean
  // #83, reshaped by C3 §8.3 — the server's verdict on THIS project's container, in five
  // values rather than the one boolean (`previewReclaimed`) it replaces. That boolean could
  // only ever say "not serving", so a Redis blip, a sleeping workspace, a slot taken by a
  // sibling project and a project nobody ever built all arrived here identically and got the
  // same "Preview unavailable" — a sentence that describes a fault for three situations that
  // are not one.
  //
  // `unknown` is why this is not a boolean with extra steps: it means the server could not
  // ask, so the pane changes NOTHING and keeps framing what it has. `null` = we have not
  // polled yet, and claims nothing either.
  //
  // Still DISTINCT from `reconnecting`, which promises a recovery already on its way: routing
  // a reclaimed container through it would spin the 20s RECONNECT_CAP_MS countdown lying
  // about a reconnect that will never happen.
  previewState?: PreviewLifeState | null
  // R17/R18 — what the app's dev server is compiling RIGHT NOW, streamed from the container.
  // FOUR values, and the fourth is why this is not a boolean: `unknown` means the platform
  // could not tell (no signal yet, socket down, or a container image predating the signal —
  // which is every app built before this shipped), and it HOLDS the cover rather than clearing
  // it. `null` = nothing has been reported on this turn at all, which is the pre-signal state
  // and behaves exactly as this pane did before the cover existed.
  compileState?: CompileState | null
  // Is a turn running on this project RIGHT NOW? It decides which of the cover's two sentences is
  // true — a wait that describes work nobody is doing is the progress-state-that-never-ends this
  // plan exists to remove — and nothing else. It deliberately does NOT decide whether to cover:
  // an app that is broken is just as broken between turns, and the error screen behind the cover
  // does not become safe to show because the build stopped.
  turnRunning?: boolean
  // U4/R7 — the idle probe found this app's workspace reverted. It outranks every other cover
  // sentence because it is the only one that is a fact about the WORKSPACE rather than about a
  // compile: the others all describe an app that is still there.
  workspaceLost?: boolean
  // `slot_taken` only — the sibling project standing in the way, so the copy can name it.
  occupyingProjectName?: string | null
  /**
   * THE WIDTH THIS PANE FRAMES AT, AND ITS RELOAD SIGNAL — both owned by the shell (plan 002, U2).
   *
   * Both used to be this component's private `useState`, which was right while their controls
   * lived in this component's own toolbar row. That row is gone: the boards draw ONE toolbar for
   * the whole workspace, above the two columns, and the device switcher and the Reload control are
   * in it. State follows its control, so these arrive as props. `reloadNonce` is combined with —
   * never replaces — this component's own automatic remount signal below.
   */
  device?: DeviceName
  reloadNonce?: number
  // Fired the moment this pane is actually SHOWING the app — the frame loaded and no cover is
  // over it — which is the honest stop-clock for "how long until the citizen saw their app".
  // Optional and fire-and-forget: this pane owes the caller nothing if the caller does not care,
  // and a throwing callback is swallowed rather than allowed to take the pane down. Fires at most
  // once per FRAME KEY, the same discipline the load and stall verdicts follow.
  //
  // WHAT IT DOES NOT PROMISE, so a counter built on it is read correctly: that the app WORKS. A
  // cross-origin `load` fires for a 500 exactly as for a 200, and this pane deliberately reveals
  // an un-verdicted frame rather than leave every pre-compile-endpoint container permanently
  // blank (see `covered` above). So this fires when the citizen is looking at their app, not when
  // the app is known good — the wait is what it measures, and a broken app ends a wait too.
  onRevealed?: () => void
}

export default function LivePreview({
  previewUrl = null,
  status = null,
  iterating = false,
  onFrameMessage,
  restoredFromFailedBuild = false,
  completedLive = false,
  // Absent means UNKNOWN, never "confirmed there is not" — a default of false would let a
  // caller that forgot the prop render the definite "this project has no saved build" claim.
  hasSavedBuild = null,
  reconnecting = false,
  // Absent means NOT YET ASKED, never "confirmed gone" — the same defaulting discipline
  // `hasSavedBuild` follows, and for the same reason: a caller that forgot the prop must not
  // be able to render a verdict nobody reached.
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
  occupyingProjectName = null,
  device = 'Desktop',
  reloadNonce: externalReloadNonce = 0,
  onRevealed,
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

  // C8 §3: the one cross-origin trust seam, and it now takes TWO facts, not one.
  //
  // WHY THE ORIGIN CHECK STOPPED BEING ENOUGH. It only ever discriminated because each app had a
  // hostname of its own. BIAL refused a wildcard certificate, so every generated app is now served
  // from ONE name — one certificate, one label, one browser origin for all of them — and an origin
  // comparison that used to mean "this is the app I am framing" degrades to "this is an app". The
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
  // The forwarded payload feeds the browser client-error arm of self-heal (U13): passing this gate
  // proves only WHERE the bytes came from, so the receiver narrows their shape downstream.
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (!previewOriginRef.current || e.origin !== previewOriginRef.current) return
      const frameWindow = frameRef.current?.contentWindow
      if (!frameWindow || e.source !== frameWindow) return
      onFrameMessageRef.current?.(e.data)
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [])

  // F8/U5 — the reconnect cap. After a COMPLETED build, a dev-process crash that never recovers
  // has no build loop left to re-frame it; cap the reconnecting state and collapse to a terminal
  // "preview unavailable" line. While a build is still running, its loop owns recovery, so we wait
  // it out (no cap) — the running build itself is bounded by its own wall-clock deadline.
  const [reconnectExpired, setReconnectExpired] = useState(false)
  useEffect(() => {
    if (!(reconnecting && completedLive)) {
      setReconnectExpired(false)
      return
    }
    const t = setTimeout(() => setReconnectExpired(true), RECONNECT_CAP_MS)
    return () => clearTimeout(t)
  }, [reconnecting, completedLive])

  const isTerminal = status === 'ended' || status === 'failed'
  // #13/R2 — a completed build's container is PARDONED server-side (alive under an idle
  // lease), so its `ended` is "done, preview live", not "gone": keep framing the URL. Only
  // with a URL, though — a completed build whose preview never came up still gets the
  // placeholder rather than a blank pane.
  const keepFramed = completedLive && !!previewUrl
  // Precedence: a terminal session collapses to a defined placeholder even if a `previewUrl` is
  // still around (post-ready teardown must NOT keep displaying a now-dead URL) — UNLESS the pardon
  // says the URL is genuinely live. Otherwise a live `previewUrl` frames the app; else we are still
  // provisioning/building (loading) or idle (empty).
  //
  // A "Restoring…" state used to sit above all of this, keyed off `relaunching`. It is gone with
  // the flag: nothing could set it, so it could only ever have unmounted the frame for a restore
  // that never started. The restore a citizen can actually run is `StartAppControl`'s, and the
  // frame's own load-gated wait is what labels it.
  const showTerminal = isTerminal && !keepFramed
  // The three states that mean "no container is serving this project". `unknown` is POINTEDLY
  // not one of them: the server could not ask, so the pane changes nothing — which is the
  // entire behavioural difference between this and the boolean it replaced.
  // Narrowed ONCE, here, so every render site below reads the union off this value instead of
  // asserting it with a cast. `notServing` keeps its exact previous meaning — none of the three
  // state strings is falsy, so `goneState !== null` is the same boolean it always was.
  const goneState: GoneState | null =
    previewState === 'asleep' || previewState === 'slot_taken' || previewState === 'never_built'
      ? previewState
      : null
  const notServing = goneState !== null
  // The pane WOULD frame the app here (live preview or pardoned completed build). A dev-process
  // crash (`reconnecting`) pre-empts the live frame with the reconnecting/unavailable states.
  const frameContext = !!previewUrl && (!isTerminal || keepFramed)
  const showReconnecting = frameContext && reconnecting && !notServing && !reconnectExpired
  const showUnavailable = frameContext && (notServing || (reconnecting && reconnectExpired))
  const showFrame = frameContext && !reconnecting && !notServing

  // U5 — the reveal is gated on the framed document's own `load`, never on a timer. A timer can
  // only prove that time passed; `load` is the only signal the browser gives us that something
  // actually arrived in the frame.
  //
  // Both verdicts are recorded PER SRC rather than as bare booleans, because a stale verdict is a
  // lie about the frame the citizen is currently looking at: a fresh `preview_ready` re-gates the
  // reveal by construction, and a relaunch after the cap returns to the honest wait instead of
  // opening into a 20-second-old complaint about a frame that no longer exists. (That second one
  // was caught in a browser, not in a test — jsdom will happily agree with whatever the state
  // machine says.)
  //
  // Note what a reveal does and does NOT claim: `load` fires for a 500 exactly as it does for a
  // 200 and this pane cannot read a cross-origin status code, so revealing means "a document
  // arrived", never "the app is healthy". Whatever ends up rendering over a framed-but-broken app
  // hangs off a health signal from the server, not off this flag.
  // …but `previewUrl` alone is NOT a sufficient identity for the frame, and that gap is review
  // finding #5. U1's attach arm made "same container, same FQDN, same URL" the common case, so a
  // repair turn ends with `previewUrl` byte-identical to what it was before. React sees the same
  // key, keeps the same DOM node, the browser never re-requests — and the citizen keeps staring
  // at the broken render of an app the server has already fixed. SL-16 caught it exactly: the
  // server served 341 chars of the repaired app while the frame reported `loads 1 -> 1`.
  //
  // HMR usually rescues this, which is why it is intermittent rather than constant. It does not
  // rescue it when self-heal restarts `next dev` mid-turn, because that kills the framed
  // document's HMR socket without anything on this side noticing.
  //
  // So the frame gets an identity that can change when the URL cannot. `iterating` falling is the
  // honest moment: it means a turn that was running OVER a live preview just ended, which is the
  // repair case and nothing else. A timer would reload an idle pane; a status tick would reload
  // on every poll and leak the HMR socket the original key comment rightly protects.
  const [autoReloadNonce, setAutoReloadNonce] = useState(0)
  const wasIterating = useRef(false)
  useEffect(() => {
    if (wasIterating.current && !iterating && previewUrl) setAutoReloadNonce((n) => n + 1)
    wasIterating.current = iterating
  }, [iterating, previewUrl])
  // TWO INDEPENDENT REASONS TO RE-REQUEST THE DOCUMENT, COMBINED RATHER THAN COLLAPSED. The one
  // above is the platform's — a turn ended over a live preview, so the served bundle may be stale.
  // The other is the citizen's, from the toolbar row's Reload control, for the staleness the
  // platform cannot detect (a dev server restarted, an HMR socket that died quietly). Either one
  // moving changes the key; neither can reset the other, which a single shared counter would.
  const frameKey = previewUrl ? `${previewUrl}#${autoReloadNonce}.${externalReloadNonce}` : null

  const [covered, setCovered] = useState(false)
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
  useEffect(() => {
    const sameApp = coveredUrlRef.current === previewUrl
    coveredUrlRef.current = previewUrl
    if (compileState === 'building' || compileState === 'failed') setCovered(true)
    else if (compileState === 'clean') setCovered(false)
    else if (!sameApp) setCovered(false)
  }, [previewUrl, compileState])

  // The cover only exists over a frame. Everything above it in the precedence chain
  // (terminal / reconnecting / unavailable) already replaces the frame entirely, and `showFrame`
  // is false in every one of those states — so this single conjunction expresses the whole of
  // `showTerminal > showReconnecting > showUnavailable > cover`.
  //
  // U4 — AND A CONFIRMED REVERSION COVERS UNCONDITIONALLY. Every other reason to cover is a fact
  // about a COMPILE, so it is right that they defer to a compile signal that says clean. This one
  // is a fact about the workspace: the app behind the frame is not the citizen's app, and a clean
  // compile of the starter template is exactly the state that would otherwise leave it revealed.
  const showCover = showFrame && (covered || workspaceLost)

  // …and the escalation, exactly once.
  //
  // Armed off `covered` — the VERDICT — rather than off `showCover`, which folds in the frame and
  // reconnect context. The citizen is being told how long their CHANGE has been coming together,
  // and that clock does not restart because a dev-server blip briefly put the reconnecting card
  // in front of the cover. Off `showCover` it did: a flicker reset the countdown mid-wait, so a
  // genuinely slow build could keep resetting to the shorter wording forever.
  const [holdingSlow, setHoldingSlow] = useState(false)
  useEffect(() => {
    // SCOPED TO THE TURN AS WELL AS TO THE COVER, and the turn half is what U7 made necessary.
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

  // WHICH SENTENCE THE COVER IS TELLING THE TRUTH WITH (U7/R13).
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
  // these that is a fact about the WORKSPACE rather than about a compile; the others all describe
  // an app that is still there.
  const coverText = workspaceLost
    ? STOPPED_RUNNING_TEXT
    : turnRunning
      ? holdingSlow
        ? HOLDING_SLOW_TEXT
        : HOLDING_TEXT
      : compileState === 'failed'
        ? IDLE_BROKEN_TEXT
        : IDLE_BUSY_TEXT

  const [loadedUrl, setLoadedUrl] = useState<string | null>(null)
  const [stalledUrl, setStalledUrl] = useState<string | null>(null)
  // Both verdicts track the FRAME KEY, not the URL. Keyed on the URL they survived a remount, so
  // a reload that hung would have kept the stale document revealed and unlabelled forever — the
  // reveal must be re-earned by whichever document is actually in the frame now.
  const frameLoaded = showFrame && loadedUrl === frameKey
  const frameStalled = showFrame && !frameLoaded && stalledUrl === frameKey
  useEffect(() => {
    if (!showFrame) {
      // The frame is coming down (a crash, a teardown). Forget both verdicts: when it comes back
      // it is a brand-new element that has to earn its reveal again.
      setLoadedUrl(null)
      setStalledUrl(null)
      return
    }
    if (frameLoaded) return
    // Do not count the wait while the cover is up. The cap exists to label a frame that never
    // loaded for no visible reason; under the cover we know exactly why it has not loaded, and
    // the card this timer arms tells the citizen to relaunch — advice that is wrong mid-build.
    // Worse, a timer left running lands the instant the cover clears, so the pane would answer a
    // successful recovery with "your app is taking longer than usual". Any verdict earned while
    // covered is dropped, and the wait restarts from the uncover.
    if (showCover) {
      setStalledUrl(null)
      return
    }
    const t = setTimeout(() => setStalledUrl(frameKey), FRAME_LOAD_CAP_MS)
    return () => clearTimeout(t)
  }, [showFrame, frameKey, frameLoaded, showCover])

  // U5 — ONE honest wait, running from "no URL yet" all the way to the framed document's own
  // `load`. It used to be destroyed the instant `previewUrl` arrived, which is precisely when the
  // 5-7s first-route compile begins: the spinner vanished and left an unlabelled blank white card
  // at the exact moment the citizen had been told their app was ready.
  // (A second copy of this wait once ran for a RELAUNCH, off `relaunching`. SL-20's finding —
  // that the one wait which can legitimately run for minutes must label itself — is not lost: the
  // restore a citizen can run today comes back as a `previewUrl` and is waited on by the frame's
  // own cap above, which is where SL-20's shared vocabulary was the point.)

  // R16/R18 — THE COVER'S STATE, and the whole safety property is in which signals move it.
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

  // R11 — THE REVEAL, AND WHAT IT IS NOT ALLOWED TO REST ON. `load` is not evidence that the app
  // works: it fires for a 500 exactly as it does for a 200, this pane cannot read a cross-origin
  // status, and the in-container proxy emits its handle-block headers even on the 502 it returns
  // when the dev server is down — so `load` fires on that too. Pairing it with the compile verdict
  // is what makes the reveal mean something, and the verdict is evaluated continuously through the
  // build rather than claimed once per turn.
  //
  // TWO PROPERTIES FALL OUT OF `!covered` THAT ARE WORTH NAMING. A verdict that flips to failed
  // after a reveal RETRACTS it — the app goes back to hidden and the cover explains — and an
  // UNKNOWN verdict does not, because `covered` holds on unknown rather than moving. Those are
  // exactly R4's retraction and AE8's don't-retract, and neither needs a rule of its own here.
  //
  // This unit controls opacity and nothing else: it renders no overlay, and every visible surface
  // above the frame belongs to the cover.
  //
  // WHAT THIS DOES NOT CLOSE, stated rather than left to be discovered. `covered` moves on
  // `building`/`failed`/`clean` and HOLDS on `unknown` and on `null` — so where no compile
  // verdict has ever been reported, the load still reveals on its own, exactly as it did before
  // this unit. That is every container running an image older than the compile endpoint, and the
  // opening moments of every turn. It is a deliberate compatibility concession and not an
  // oversight: gating on a POSITIVE verdict would leave the whole existing fleet's preview
  // permanently blank, which is a worse failure than the one being fixed. The signal reaches an
  // app on its next provision or restore, and the reveal gets teeth at the same moment the cover
  // does — the same trade the cover already documents.
  const revealed = frameLoaded && !covered
  // Announce that reveal ONCE per document. Keyed on the frame key rather than on `revealed`
  // alone, because a verdict that flips to failed RETRACTS the reveal and a later re-reveal of
  // the same document is not a second first-view. A reload (a new nonce, so a new key) does
  // announce again; the caller's own mark is idempotent, so the two guards agree rather than
  // either one having to be perfect.
  //
  // AND IT CHECKS `workspaceLost` SEPARATELY, because `revealed` is NOT "the cover is down".
  // `showCover` is `covered || workspaceLost` while `revealed` reads only `covered`, so a
  // confirmed reversion leaves the frame at full opacity UNDER a cover that says the app stopped
  // running — visually correct (the cover is on top) and, without this term, a reported first
  // view of an app the citizen cannot see. `revealed` already implies `!covered`, so this is the
  // only case the two expressions disagree on.
  const announcedRevealOf = useRef<string | null>(null)
  useEffect(() => {
    if (!revealed || workspaceLost || !frameKey) return
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
  }, [revealed, workspaceLost, frameKey, onRevealed])
  const framePending = showFrame && !frameLoaded && !frameStalled
  const showLoading =
    framePending || (!isTerminal && !previewUrl && (status === 'provisioning' || status === 'building'))
  // `showEmpty` is GONE with the empty state it gated — see the note at its old render site.

  // ONE announcement for the whole pane, read out of a region that is ALWAYS mounted (below).
  // The pane now has four preview states on top of its four waits, and before this the only
  // thing carrying `aria-live` was the reconnecting box; the loading overlay carried
  // `aria-busy`, which announces exactly nothing. Mounting a live region
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
            ? framePending
              ? FRAMING_TEXT
              : ((status && LOADING_TEXT[status]) ?? 'Building your app…')
            : showUnavailable
              ? goneState
                ? GONE_TITLE[goneState]
                : 'Preview unavailable'
              : showTerminal
                ? 'The preview is no longer running'
                : previewState === 'starting'
                  ? // U13: a start is CONFIRMED in flight (a build, a relaunch, or another tab's
                    // turn), unlike `unknown` below which confirms nothing. Reuses the same
                    // sentence the frame's own cold-start wait uses — one truthful "starting" word
                    // for the citizen, not a second name for the same fact. Announcement-only, like
                    // `unknown`: the fuller pane treatment for this state is Plan F's (R-4).
                    FRAMING_TEXT
                  : previewState === 'unknown'
                    ? // The honest sentence for a check that did not happen. It deliberately does
                      // NOT disturb the frame — nothing was learned, so nothing changes on screen.
                      'We could not check on your preview just now — it may still be running'
                    : revealed
                      ? 'Your app preview is live'
                      : ''

  return (
    <div className="flex flex-col h-full">
      {/* THE TOOLBAR ROW THIS COMPONENT USED TO DRAW IS GONE (plan 002, U2), and it is a removal
          rather than a relocation of markup. The boards draw ONE row for the whole workspace,
          under the navbar and above both columns, and this one sat INSIDE the pane — so it only
          existed once something was framed, which is why a project with nothing built had no
          device switcher, no Save and no way to open the app in a tab.

          Where its four occupants went: the device group and the Reload control to the shell's
          row (their state came with them and arrives here as props); Save to the same row, reading
          the channel's save cell; and the publish chip to the row's left cluster beside the title,
          where U4 rebuilds it. Nothing was dropped. */}

      {/* Main area */}
      <div className="flex-1 flex overflow-hidden relative">
        <div className="flex-1 bg-[#e8edf2] flex p-4 overflow-auto">
          {/* THE EMPTY STATE IS GONE (Plan F, U4) — structurally unreachable, not merely unused.
              `AppPane` is what mounts the host now, and it mounts it only when the address
              resolver returned a URL; `showEmpty` required `!previewUrl`. The sentence a citizen
              reads when there is nothing to frame is `AppPane`'s, drawn from the one computed
              workspace state, so a pane sentence has exactly one author. */}

          {/* F8/U5 — the dev-server PROCESS crashed after framing. A DISTINCT visual from the
              "Building…" blue bouncing dots (a spinning glyph + warning tint) so a dead frame never
              reads as "still building". Self-heals when the server restarts (a fresh preview_ready). */}
          {/* No `aria-live` of its own any more: the persistent status region at the bottom of
              this pane speaks for every state, and two live regions describing the same
              situation announce it twice. `aria-busy` stays — it is a property, not a speech. */}
          {showReconnecting && (
            <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center" aria-busy="true">
              <RotateCcw size={26} className="text-warning animate-spin" style={{ animationDuration: '1.4s' }} />
              <p className="text-sm font-semibold text-neutral">Reconnecting to your preview…</p>
              <p className="text-xs text-neutral/60 max-w-xs leading-relaxed">
                The preview server restarted. This usually reconnects on its own in a moment.
              </p>
            </div>
          )}

          {/* Two situations share this card and they are NOT the same news.
              · `notServing` — the server's verdict that no container is on this project. An
                ordinary workspace state (asleep / taken by a sibling project / never built),
                so the copy says what happened and how to undo it, and the icon is a moon
                rather than a severed connection. Nothing here is styled as danger and nothing
                carries `role="alert"`: a reclaimed container is not a failure, and telling a
                citizen it is teaches them to distrust a platform that behaved correctly.
              · the reconnect cap expiring — a dev server that genuinely died and did not come
                back. That one keeps its original "Preview unavailable" wording, because that
                is what it is. */}
          {showUnavailable && (
            <div className="flex-1 flex items-center justify-center">
              {/* F3: same compact-card treatment as showTerminal, applied here too (#42). */}
              <div
                data-testid="preview-unavailable-card"
                data-preview-state={notServing ? previewState : 'disconnected'}
                className="w-full max-w-xs bg-white rounded-xl border border-bial-border shadow-sm px-5 py-5 flex flex-col items-center text-center"
              >
                <div className="w-10 h-10 rounded-xl bg-gray-100 flex items-center justify-center mb-3">
                  {notServing ? (
                    <Moon size={18} className="text-gray-300" />
                  ) : (
                    <WifiOff size={18} className="text-gray-300" />
                  )}
                </div>
                <p className="text-sm font-semibold text-neutral mb-1">
                  {goneState ? GONE_TITLE[goneState] : 'Preview unavailable'}
                </p>
                {/* R5: the saved-app promise is made ONLY when the server confirmed a saved build
                    (strict === true — null is "store unreachable", which claims nothing). */}
                <p className="text-xs text-neutral/60 leading-relaxed mb-3">
                  {goneState
                    ? goneBody(goneState, occupyingProjectName, hasSavedBuild)
                    : hasSavedBuild === false
                      ? 'There’s nothing to relaunch yet — this project has no saved build. Build the app first.'
                      : hasSavedBuild === true
                        ? 'The preview server stopped and didn’t come back. Your saved app is still there.'
                        : 'The preview server stopped and didn’t come back. Start a new build to bring the live preview back.'}
                </p>
                {/* The button is a SHORTCUT, never the only way back: a prompt restores the
                    workspace too, behind the labelled wait. Offered on the same confirmed-true
                    gate as everywhere else on this pane. */}
              </div>
            </div>
          )}

          {showTerminal && (
            <div className="flex-1 flex items-center justify-center">
              {/* F3: a small bounded card, not a full-pane dead state (#42). Inner copy/logic is
                  the current (post-#82) showTerminal content, unchanged; only the outer shrank. */}
              <div
                data-testid="preview-ended-card"
                className="w-full max-w-xs bg-white rounded-xl border border-bial-border shadow-sm px-5 py-5 flex flex-col items-center text-center"
              >
                <div className="w-10 h-10 rounded-xl bg-gray-100 flex items-center justify-center mb-3">
                  <PowerOff size={18} className="text-gray-300" />
                </div>
                <p className="text-sm font-semibold text-neutral mb-1">The preview is no longer running</p>
                {/* R5, same discipline as the empty branch: the saved-app claim needs the server's
                    confirmed === true (null claims nothing in either direction). */}
                <p className="text-xs text-neutral/60 leading-relaxed mb-3">
                  {hasSavedBuild === false
                    ? 'There’s nothing to relaunch yet — this project has no saved build. Build the app first.'
                    : hasSavedBuild === true
                      ? 'This build session has ended. Your saved app is still there.'
                      : 'This build session has ended. Start a new build to bring the live preview back.'}
                </p>
              </div>
            </div>
          )}

          {showFrame && (
            // No padding/border here: the iframe's `w-full` below depends on this box's
            // content width being exactly the device pixel width, with nothing to subtract.
            <div
              data-testid="device-card"
              style={{ width: DEVICES[device].width ? `${DEVICES[device].width}px` : '100%' }}
              // `width` is deliberately EXCLUDED from the transition (no `transition-all`, no
              // `transition-[width]`): animating layout width genuinely resizes the cross-origin
              // iframe on every intermediate frame of the sweep, firing a burst of real `resize`
              // events / ResizeObserver callbacks inside the framed app — components that latch a
              // dimension on their first callback can settle on a transient mid-sweep value
              // instead of the real device width. Scoping the transition to paint-only properties
              // makes the box snap to its target width in one paint; still visually smooth.
              // `opacity` IS in the transition (it is paint-only, so it costs the framed document
              // nothing) — that is the U5 fade, and until it runs the card is opacity-0 with the
              // labelled wait sitting over it. Hidden, not unmounted: an iframe that never mounts
              // never loads, and `load` is the only thing that reveals it.
              className={`shrink-0 mx-auto h-full transition-[box-shadow,border-radius,opacity] duration-300 rounded-xl overflow-hidden shadow-lg bg-white relative ${revealed ? 'opacity-100' : 'opacity-0'}`}
            >
              {/* A subtle "still working" overlay while the loop keeps refining a LIVE preview
                  (status holds at `ready` and new step/log envelopes keep arriving). Non-blocking
                  (pointer-events-none) so the operator can still interact with the framed app. */}
              {iterating && (
                <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 pointer-events-none">
                  <div className="flex items-center gap-2 bg-white/90 backdrop-blur border border-bial-border rounded-full px-3 py-1 shadow-sm">
                    <span className="flex gap-1">
                      {[0, 1, 2].map((i) => (
                        <span key={i} className="w-1.5 h-1.5 bg-primary rounded-full animate-bounce" style={{ animationDelay: `${i * 0.15}s` }} />
                      ))}
                    </span>
                    <span className="text-[11px] font-semibold text-neutral">Still working…</span>
                  </div>
                </div>
              )}
              {keepFramed && !showCover && (
                // #13/R2 honesty chip: the build is DONE and this is the live result — without
                // it, an ended status with a working frame reads as "is it still building?".
                //
                // U18 — AND IT SITS BENEATH THE OVERLAY CHAIN, which is not a z-index remark: the
                // cover is `z-20` over this `z-10`, so the chip was already invisible under it
                // and still in the DOM, which is where a screen reader lives. A pane covered by
                // the retraction announced "your app stopped running" and "Build complete — your
                // app is live below" in the same breath, and the reader who most needs the first
                // sentence is the one who heard both. Every other overlay in this file already
                // loses to `showCover` (the two waits, the stalled card); the completion claim is
                // the one that must lose hardest, because it is the claim being retracted.
                <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 pointer-events-none">
                  <div className="bg-white/90 backdrop-blur border border-bial-border rounded-full px-3 py-1 shadow-sm">
                    <span className="text-[11px] font-semibold text-neutral">
                      Build complete — your app is live below
                    </span>
                  </div>
                </div>
              )}
              {restoredFromFailedBuild && (
                // U6 honesty overlay: this frame restored the LAST SAVED version because the
                // newest build failed — never present older code as the latest build's result.
                <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 pointer-events-none">
                  <div className="bg-white/90 backdrop-blur border border-warning/40 rounded-full px-3 py-1 shadow-sm">
                    <span className="text-[11px] font-semibold text-neutral">
                      Showing your last saved version — the most recent build failed
                    </span>
                  </div>
                </div>
              )}
              <iframe
                /* Key on `frameKey` = url + reload nonce. A NEW url still remounts exactly as it
                   always did; the nonce adds the case the url alone cannot express — same
                   container, repaired app (#5). A plain re-render still keeps the same DOM node,
                   so the framed app's HMR websocket is not leaked on every status tick. */
                key={frameKey}
                /* The identity the inbound message gate compares `e.source` against (C8 §3).
                   React attaches and detaches this alongside `key`, so a remount or an unmounted
                   pane nulls it on its own — which is the fail-closed state, not a gap. */
                ref={frameRef}
                src={previewUrl}
                /* U5 — the ONLY thing that reveals this frame. Recording the KEY rather than a
                   bare `true` is what makes the next load re-gate itself. */
                onLoad={() => setLoadedUrl(frameKey)}
                className="w-full h-full border-0"
                title="App Preview"
                /* C8 §4 (FROZEN): the preview is a genuinely CROSS-ORIGIN sandbox frame (the sandbox's
                   own FQDN, served by its Caddy with `frame-ancestors <portal-origin>`), so the token
                   list ADDS `allow-same-origin` — the real `next dev` app must run as its own
                   sandbox-FQDN origin (storage, the HMR websocket, RSC fetches). Safe BECAUSE the
                   frame is genuinely cross-origin: SOP still walls the app off from the portal, and the
                   cross-origin barrier stops the framed script stripping its own sandbox.
                   `allow-top-navigation*` / `allow-popups` stay WITHHELD — the framed app is unreviewed,
                   agent-generated, self-heal-loop code (no top-nav hijack of, nor popup-phishing of, the
                   portal tab). Do not widen without revising C8. */
                sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"
              />
            </div>
          )}
        </div>

        {/* U5 — the wait, rendered OVER the pane rather than beside it. It has to co-exist with a
            mounted-but-unrevealed frame (the frame must be loading for `load` to ever fire), and
            the device card owns the pane's whole content width, so a sibling would be squeezed to
            nothing. Same anchor for both waits, so they can never be on screen at once. */}
        {/* R16/R18 — THE COVER, over the frame and under nothing. Same anchor and same calm
            bouncing-dots treatment as the wait below it, DELIBERATELY: the citizen is in one
            situation ("my app has not opened yet") and the spinner-plus-warning tint two blocks
            down means something else — a dev server that is genuinely down. Do not borrow it.

            Its precedence is expressed by `showFrame` (everything above it in the chain
            unmounts the frame) plus the `!showCover` guards on the two waits below. A later
            unit adds the retraction card into this same cover, ABOVE this content and never
            alongside it — the cover shows exactly one thing. */}
        {showCover && (
          <BouncingWait className="text-center px-6 max-w-sm">
            {coverText}
          </BouncingWait>
        )}

        {showLoading && !showCover && (
          <BouncingWait>
            {framePending ? FRAMING_TEXT : ((status && LOADING_TEXT[status]) ?? 'Building your app…')}
          </BouncingWait>
        )}

        {/* U5 — the bounded degradation: the frame never loaded, so say so and offer a way out,
            while leaving it MOUNTED underneath. Unmounting it would make the timeout permanent by
            construction (the `load` it is waiting for could never arrive), so this says "slow",
            not "dead" — a load that lands after FRAME_LOAD_CAP_MS still wins and reveals. */}
        {/* …and this degraded twin loses to the cover for the same reason the wait above does:
            when the cover is up we KNOW why the frame has not loaded (the app is compiling, or
            it failed to), and this card's "relaunch it" advice would be wrong. Two waits never
            share the screen — the rule this file already kept, extended to the third. */}
        {frameStalled && !showCover && (
          <div className="absolute inset-0 z-20 bg-[#e8edf2] flex flex-col items-center justify-center text-center px-6">
            <div className="w-16 h-16 rounded-2xl bg-gray-100 flex items-center justify-center mb-4">
              <Loader2 size={26} className="text-warning animate-spin" style={{ animationDuration: '1.8s' }} />
            </div>
            <p className="text-sm font-semibold text-neutral mb-1">{SLOW_TEXT}</p>
            {/* THE COPY NO LONGER NAMES A CONTROL THIS CARD DOES NOT HAVE (Plan F, U4). These three
                placeholders used to end with "relaunch it" / "relaunch the preview to start it
                fresh" beside a button that sat right below them; the button moved to `AppPane`,
                where R3's one start control lives, and an instruction pointing at nothing is worse
                than no instruction. Each sentence keeps its FACT and drops the direction. */}
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
