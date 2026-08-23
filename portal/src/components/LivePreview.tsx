import { useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { Monitor, Tablet, Smartphone, LayoutTemplate, PowerOff, RotateCcw, WifiOff, Moon, Save, Loader2 } from 'lucide-react'
import { relaunchRetryable } from '../utils/buildSessionTypes'
import type { RelaunchError, BuildSessionStatus } from '../utils/buildSessionTypes'
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
const DEVICES = {
  Desktop: { icon: Monitor, width: null as number | null },
  Tablet: { icon: Tablet, width: 834 }, // iPad Pro 11" portrait width — Chrome DevTools preset
  Mobile: { icon: Smartphone, width: 390 }, // iPhone 12/13/14-class width
}
type DeviceName = keyof typeof DEVICES

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
// The one sentence both slow waits use. Deliberately shared: the citizen is in ONE situation
// ("my app has not opened yet") and naming it twice depending on which internal timer happens
// to be running is the pane talking about itself instead of to them.
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

/** The headline for a pane whose container is not serving this project — C3 §8.3.
 *
 *  NONE OF THESE IS AN ERROR, and the copy is the whole deliverable of R16/R17. A reclaimed
 *  workspace used to read "Preview unavailable", which describes a platform fault; it is a
 *  sleeping workspace whose work is on durable storage, and the next prompt brings it back. */
/** The three states that mean "no container is serving this project" — `alive` and `unknown` are
 *  pointedly excluded. Named once so the copy tables and the render sites all narrow to the same
 *  union instead of each asserting it with a cast (`.claude/rules/fail-first-typescript.md`). */
type GoneState = Exclude<PreviewLifeState, 'alive' | 'unknown'>

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
 * The relaunch error + button pair, shared by the terminal placeholder and the
 * project-has-app empty state so the U6 response matrix behaves identically in both:
 * retryable errors keep the button, `not_found` (handled by the CALLER's copy) hides it.
 */
interface RelaunchAffordanceProps {
  onRelaunch?: () => void
  relaunchError: RelaunchError | null
  label: string
}

function RelaunchAffordance({ onRelaunch, relaunchError, label }: RelaunchAffordanceProps) {
  return (
    <>
      {relaunchError && relaunchError.kind !== 'not_found' && (
        // 503 (transient) / 5xx: the failure's own copy, with the button restored below
        // so the retry sits right where the user is looking.
        <p role="alert" className="text-xs text-danger max-w-xs leading-relaxed mb-3">
          {relaunchError.message}
        </p>
      )}
      {onRelaunch && (!relaunchError || relaunchRetryable(relaunchError.kind)) && (
        <button
          type="button"
          onClick={onRelaunch}
          className="inline-flex items-center gap-1.5 text-xs font-worksans font-semibold text-white bg-primary hover:bg-primary/90 rounded-lg px-3.5 py-2 transition"
        >
          <RotateCcw size={13} />
          {label}
        </button>
      )}
    </>
  )
}

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
 *   - `onFrameMessage` — the FUTURE Wave-1 client-error receiver seam. The inbound `message`
 *                    listener still validates `e.origin` against the sandbox origin (the C8 §3
 *                    security assertion, pinned by the skeleton) and forwards ONLY origin-valid
 *                    messages here. No relay is wired yet (C7 §7 is deferred); the gate stands.
 *
 *   - `onRelaunch` — when set, the terminal (ended/failed) placeholder offers a "Relaunch preview"
 *                    button (#43): restore the torn-down app from its snapshot into a fresh sandbox.
 *   - `relaunching` — true while that restore is in flight; the pane shows a "Restoring…" affordance
 *                    (and hides the button) so a second click can't fire a self-conflicting request.
 *   - `relaunchError` — the discriminated relaunch failure (U6 response matrix): `not_found` hides
 *                    the affordance ("nothing to relaunch"); `unavailable`/`failed` show their copy
 *                    with the button restored for a retry. 409 renders via the block banner, not here.
 *   - `lastBuildFailed` — the newest recorded build outcome FAILED, so a relaunch restores the last
 *                    SAVED version — the button says so instead of promising that build's result.
 *   - `restoredFromFailedBuild` — the framed preview IS such a restore (server-confirmed); a small
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
  onRelaunch?: () => void
  relaunching?: boolean
  relaunchError?: RelaunchError | null
  lastBuildFailed?: boolean
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
  // `slot_taken` only — the sibling project standing in the way, so the copy can name it.
  occupyingProjectName?: string | null
  saveDirty?: boolean | null
  onSave?: () => void
  saving?: boolean
  saveError?: string | null
  toolbarLeading?: ReactNode
  toolbarTrailing?: ReactNode
}

export default function LivePreview({
  previewUrl = null,
  status = null,
  iterating = false,
  onFrameMessage,
  onRelaunch,
  relaunching = false,
  relaunchError = null,
  lastBuildFailed = false,
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
  occupyingProjectName = null,
  // The save model (KTD-5e). `saveDirty` is TRI-STATE: true = unsaved work, false = saved,
  // null = UNKNOWN (no live workspace, or the server could not compare). Unknown must not
  // render as saved — that tells the user their work is safe when nothing checked.
  saveDirty = null,
  onSave,
  saving = false,
  saveError = null,
  // Rendered as the FIRST child of the toolbar's left group, ahead of the device-width
  // buttons — the caller's own toggle/controls that need to live in-flow next to this
  // toolbar rather than float over it (#87 — an absolutely-positioned caller button here
  // used to overlap the device-width group in the same corner).
  toolbarLeading = null,
  // Rendered as the LAST child of the toolbar's right group, after Save. Publish lives here
  // (via the caller, so this component stays unaware of deployment) because the moment a
  // build finishes is the moment someone wants to put it out — making them navigate to the
  // project page to find the button is asking them to leave the room to use the light switch.
  toolbarTrailing = null,
}: LivePreviewProps) {
  const [viewport, setViewport] = useState<DeviceName>('Desktop')

  // The sandbox preview origin, held in a ref so the mount-once message listener always reads
  // the CURRENT origin without re-subscribing on every prop change.
  const previewOrigin = originOf(previewUrl)
  const previewOriginRef = useRef(previewOrigin)
  previewOriginRef.current = previewOrigin
  const onFrameMessageRef = useRef(onFrameMessage)
  onFrameMessageRef.current = onFrameMessage

  // C8 §3: the one cross-origin trust seam. Reject any message whose origin is not the sandbox
  // preview origin — the only trusted sender — BEFORE trusting the payload. A null previewOrigin
  // (preview dark) rejects everything. This is a security assertion, not a nicety; the walking
  // skeleton (scripts/skeleton) pins the reject path for real. The forwarded payload feeds the
  // future Wave-1 browser client-error self-heal arm — the seam exists now so nothing is
  // retrofitted later, but nothing consumes it yet.
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (!previewOriginRef.current || e.origin !== previewOriginRef.current) return
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
  // Precedence: a relaunch in flight shows "Restoring…" over everything; then a terminal session
  // collapses to a defined placeholder even if a `previewUrl` is still around (post-ready teardown
  // must NOT keep displaying a now-dead URL) — UNLESS the pardon says the URL is genuinely live.
  // Otherwise a live `previewUrl` frames the app; else we are still provisioning/building
  // (loading) or idle (empty).
  const showRestoring = relaunching
  const showTerminal = isTerminal && !relaunching && !keepFramed
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
  const frameContext = !relaunching && !!previewUrl && (!isTerminal || keepFramed)
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
  const [reloadNonce, setReloadNonce] = useState(0)
  const wasIterating = useRef(false)
  useEffect(() => {
    if (wasIterating.current && !iterating && previewUrl) setReloadNonce((n) => n + 1)
    wasIterating.current = iterating
  }, [iterating, previewUrl])
  const frameKey = previewUrl ? `${previewUrl}#${reloadNonce}` : null

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
    const t = setTimeout(() => setStalledUrl(frameKey), FRAME_LOAD_CAP_MS)
    return () => clearTimeout(t)
  }, [showFrame, frameKey, frameLoaded])

  // U5 — ONE honest wait, running from "no URL yet" all the way to the framed document's own
  // `load`. It used to be destroyed the instant `previewUrl` arrived, which is precisely when the
  // 5-7s first-route compile begins: the spinner vanished and left an unlabelled blank white card
  // at the exact moment the citizen had been told their app was ready.
  // …and the same honest wait for a RELAUNCH, which had none. The cap above is armed off
  // `showFrame`, but `frameContext` excludes `relaunching`, so the frame is unmounted for the whole
  // restore — meaning the ONE wait that can legitimately run for minutes was the one wait that
  // could never label itself. SL-20 watched a citizen sit on a bare "Restoring your app…" for two
  // solid minutes and then get told the sandbox was unavailable. Same cap as the frame's, so both
  // waits start speaking at the same moment rather than inventing a second timing vocabulary.
  const [relaunchSlow, setRelaunchSlow] = useState(false)
  useEffect(() => {
    if (!relaunching) {
      setRelaunchSlow(false)
      return
    }
    const t = setTimeout(() => setRelaunchSlow(true), FRAME_LOAD_CAP_MS)
    return () => clearTimeout(t)
  }, [relaunching])

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
  const [covered, setCovered] = useState(false)
  // …with ONE exception to the hold, and it is not a hole in it: a DIFFERENT running app. Holding
  // is fail-closed because an absent signal says nothing about the app we are covering; a new
  // `previewUrl` means we are no longer covering that app at all, and keeping the cover up would
  // be a claim about code this container has never seen. Nothing is uncovered in the gap either —
  // a new url remounts the frame, so the frame-load wait owns the screen until the first report
  // lands a poll later. Declared BEFORE the signal effect so a simultaneous change applies the
  // reset first and the new verdict second.
  useEffect(() => {
    setCovered(false)
  }, [previewUrl])
  useEffect(() => {
    if (compileState === 'building' || compileState === 'failed') setCovered(true)
    else if (compileState === 'clean') setCovered(false)
    // 'unknown' and null: hold. See above — this is the fail-closed arm, not an oversight.
  }, [compileState])

  // The cover only exists over a frame. Everything above it in the precedence chain
  // (restoring / terminal / reconnecting / unavailable) already replaces the frame entirely, and
  // `showFrame` is false in every one of those states — so this single conjunction expresses the
  // whole of `showRestoring > showTerminal > showReconnecting > showUnavailable > cover`.
  const showCover = showFrame && covered

  // …and the escalation, exactly once. The timer is armed off `showCover` alone, so it restarts
  // when a NEW cover goes up and never re-fires while one stays up.
  const [holdingSlow, setHoldingSlow] = useState(false)
  useEffect(() => {
    if (!showCover) {
      setHoldingSlow(false)
      return
    }
    const t = setTimeout(() => setHoldingSlow(true), HOLDING_ESCALATE_MS)
    return () => clearTimeout(t)
  }, [showCover])

  const framePending = showFrame && !frameLoaded && !frameStalled
  const showLoading =
    framePending || (!isTerminal && !relaunching && !previewUrl && (status === 'provisioning' || status === 'building'))
  const showEmpty = !isTerminal && !relaunching && !previewUrl && !showLoading

  // ONE announcement for the whole pane, read out of a region that is ALWAYS mounted (below).
  // The pane now has four preview states on top of its four waits, and before this the only
  // thing carrying `aria-live` was the reconnecting box; `showRestoring` and the loading
  // overlay carried `aria-busy`, which announces exactly nothing. Mounting a live region
  // together with its text announces inconsistently across screen readers, so the region is
  // permanent and only this string changes.
  //
  // Ordered by what is actually on screen, most specific first. `polite` throughout — none of
  // these is an error, and `assertive` is reserved for the ones that are (the relaunch failure
  // and the save failure keep their own `role="alert"`).
  const announcement = showRestoring
    ? relaunchSlow
      ? SLOW_TEXT
      : 'Restoring your app…'
    : showReconnecting
      ? 'Reconnecting to your preview…'
      : showCover
        ? holdingSlow
          ? HOLDING_SLOW_TEXT
          : HOLDING_TEXT
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
                : previewState === 'unknown'
                  ? // The honest sentence for a check that did not happen. It deliberately does
                    // NOT disturb the frame — nothing was learned, so nothing changes on screen.
                    'We could not check on your preview just now — it may still be running'
                  : frameLoaded
                    ? 'Your app preview is live'
                    : ''

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-bial-border bg-white flex-shrink-0">
        <div className="flex items-center gap-2">
          {toolbarLeading}
          <div role="group" aria-label="Preview device width" className="flex items-center gap-1 bg-bial-bg rounded-lg p-1">
            {(Object.entries(DEVICES) as [DeviceName, (typeof DEVICES)[DeviceName]][]).map(([label, { icon: Icon }]) => (
              <button
                key={label}
                type="button"
                aria-pressed={viewport === label}
                onClick={() => setViewport(label)}
                className={`flex items-center gap-1.5 text-xs font-worksans font-medium px-3 py-1.5 rounded-md transition ${
                  viewport === label
                    ? 'bg-white text-primary shadow-sm border border-bial-border'
                    : 'text-neutral hover:text-primary'
                }`}
              >
                <Icon size={12} />{label}
              </button>
            ))}
          </div>
        </div>

        {/* RELOAD. The automatic remount above covers the case we can detect (a turn ending over
            a live preview), but "what I see is out of date" is a judgement only the person
            looking at it can make — a dev-server restart, an HMR socket that died quietly, a
            route the agent touched without ending a turn. Without this the citizen's only
            recourse was reloading the whole portal. Rendered only when something is framed. */}
        {showFrame && (
          <button
            type="button"
            onClick={() => setReloadNonce((n) => n + 1)}
            title="Reload the preview"
            className="flex items-center gap-1.5 text-xs font-worksans font-medium px-3 py-1.5 rounded-md text-neutral hover:text-primary transition"
          >
            <RotateCcw size={12} />
            Reload
          </button>
        )}

        {/* SAVE. The agent commits inside the container as it works; this is the only thing
            that pushes the result to durable storage, and it happens because the user asked.
            Rendered only when there is a workspace to save FROM (`saveDirty !== null`) — a
            button that cannot do anything is worse than no button. */}
        {onSave && saveDirty !== null && (
          <div className="flex items-center gap-2">
            {saveError && (
              <span role="alert" className="text-[11px] text-danger max-w-[220px] text-right">
                {saveError}
              </span>
            )}
            {saveDirty === false && !saving && (
              <span className="text-[11px] text-neutral/70">All changes saved</span>
            )}
            <button
              type="button"
              onClick={onSave}
              disabled={saving || saveDirty === false}
              data-testid="save-project"
              // Highlighted ONLY when there is something to save. A permanently-primary Save
              // button trains the user to ignore it, which is the state the dirty check exists
              // to escape.
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-worksans font-semibold transition disabled:opacity-50 ${
                saveDirty
                  ? 'bg-primary text-white hover:bg-primary-600'
                  : 'border border-bial-border bg-white text-neutral'
              }`}
            >
              {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
              {saving ? 'Saving…' : saveDirty ? 'Save' : 'Saved'}
            </button>
          </div>
        )}

        {toolbarTrailing}
      </div>

      {/* Main area */}
      <div className="flex-1 flex overflow-hidden relative">
        <div className="flex-1 bg-[#e8edf2] flex p-4 overflow-auto">
          {showEmpty && (
            <div className="flex-1 flex flex-col items-center justify-center text-center">
              <div className="w-16 h-16 rounded-2xl bg-gray-100 flex items-center justify-center mb-4">
                <LayoutTemplate size={28} className="text-gray-300" />
              </div>
              <p className="text-sm font-semibold text-neutral mb-1">Your app preview will appear here</p>
              {/* Finding #1: relaunch derives from PROJECT state, not this conversation's build
                  history — a fresh chat in a project with a saved build can bring it back here.
                  N7: the claim is made ONLY when the server confirmed a restorable snapshot. */}
              {hasSavedBuild === true && onRelaunch && relaunchError?.kind !== 'not_found' ? (
                <>
                  <p className="text-xs text-neutral/60 max-w-xs leading-relaxed mb-4">
                    This project already has a saved build. Relaunch it to preview the latest
                    version, or send a prompt to keep building.
                  </p>
                  <RelaunchAffordance
                    onRelaunch={onRelaunch}
                    relaunchError={relaunchError}
                    label="Relaunch preview"
                  />
                </>
              ) : (
                <>
                  {/* A 404 on the click used to hide the button with NO message: the user
                      pressed Relaunch and the affordance simply vanished. That silence was
                      defensible while the claim itself was untrustworthy — it hid our own
                      false promise. With a truthful predicate a 404 is genuinely exceptional
                      (the bundle was deleted between the read and the click), so say so. */}
                  {relaunchError?.kind === 'not_found' && (
                    <p role="alert" className="text-xs text-danger max-w-xs leading-relaxed mb-3">
                      That saved build is no longer available. Send a prompt to build the app again.
                    </p>
                  )}
                  <p className="text-xs text-neutral/60 max-w-xs leading-relaxed">
                    Submit a prompt to start a build — the live app appears here once its dev server is up.
                  </p>
                </>
              )}
            </div>
          )}

          {showRestoring && (
            <div className="flex-1 flex flex-col items-center justify-center gap-4" aria-busy="true">
              <div className="flex gap-2">
                {[0, 1, 2].map((i) => (
                  <div
                    key={i}
                    className="w-3 h-3 bg-primary rounded-full animate-bounce"
                    style={{ animationDelay: `${i * 0.2}s` }}
                  />
                ))}
              </div>
              <p className="text-sm text-neutral font-medium">Restoring your app…</p>
              {/* Deliberately the SAME sentence the frame-load stall uses. The citizen is in one
                  situation — "my app has not opened yet" — and giving that situation two different
                  names depending on which internal wait happens to be running is the pane talking
                  about itself instead of to them. The second line says the part that is specific to
                  a relaunch: nothing is lost while this runs. */}
              {relaunchSlow && (
                <div className="flex flex-col items-center gap-1 text-center max-w-xs">
                  <p className="text-sm font-semibold text-neutral">{SLOW_TEXT}</p>
                  <p className="text-xs text-neutral leading-relaxed">
                    This usually means the app&rsquo;s first page is slow to load. Your work is safe
                    — nothing is discarded while this runs.
                  </p>
                </div>
              )}
            </div>
          )}

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
                    (strict === true — null is "store unreachable", which claims nothing). A 404
                    after the click is said out loud, like the empty branch's, never a silently
                    vanished button. */}
                {relaunchError?.kind === 'not_found' ? (
                  <p role="alert" className="text-xs text-danger leading-relaxed mb-3">
                    There&rsquo;s nothing to relaunch yet — this project has no saved build. Build
                    the app first.
                  </p>
                ) : (
                  <p className="text-xs text-neutral/60 leading-relaxed mb-3">
                    {goneState
                      ? goneBody(goneState, occupyingProjectName, hasSavedBuild)
                      : hasSavedBuild === false
                        ? 'There’s nothing to relaunch yet — this project has no saved build. Build the app first.'
                        : hasSavedBuild === true && onRelaunch
                          ? 'The preview server stopped and didn’t come back. Relaunch it to restore your saved app.'
                          : 'The preview server stopped and didn’t come back. Start a new build to bring the live preview back.'}
                  </p>
                )}
                {/* The button is a SHORTCUT, never the only way back: a prompt restores the
                    workspace too, behind the labelled wait. Offered on the same confirmed-true
                    gate as everywhere else on this pane. */}
                {hasSavedBuild === true && (
                  <RelaunchAffordance
                    onRelaunch={onRelaunch}
                    relaunchError={relaunchError}
                    label={notServing ? 'Bring it back' : 'Relaunch preview'}
                  />
                )}
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
                    confirmed === true (null claims nothing in either direction), and the 404
                    not-found is an announced role="alert", never an unexplained missing button. */}
                {relaunchError?.kind === 'not_found' ? (
                  <p role="alert" className="text-xs text-danger leading-relaxed mb-3">
                    There&rsquo;s nothing to relaunch yet — this project has no saved build. Build
                    the app first.
                  </p>
                ) : (
                  <p className="text-xs text-neutral/60 leading-relaxed mb-3">
                    {hasSavedBuild === false
                      ? 'There’s nothing to relaunch yet — this project has no saved build. Build the app first.'
                      : hasSavedBuild === true && onRelaunch
                        ? 'This build session has ended. Relaunch it to restore your saved app into a fresh preview.'
                        : 'This build session has ended. Start a new build to bring the live preview back.'}
                  </p>
                )}
                {hasSavedBuild === true && (
                  <RelaunchAffordance
                    onRelaunch={onRelaunch}
                    relaunchError={relaunchError}
                    label={lastBuildFailed ? 'Relaunch last saved version' : 'Relaunch preview'}
                  />
                )}
              </div>
            </div>
          )}

          {showFrame && (
            // No padding/border here: the iframe's `w-full` below depends on this box's
            // content width being exactly the device pixel width, with nothing to subtract.
            <div
              data-testid="device-card"
              style={{ width: DEVICES[viewport].width ? `${DEVICES[viewport].width}px` : '100%' }}
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
              className={`shrink-0 mx-auto h-full transition-[box-shadow,border-radius,opacity] duration-300 rounded-xl overflow-hidden shadow-lg bg-white relative ${frameLoaded ? 'opacity-100' : 'opacity-0'}`}
            >
              {/* A subtle "still iterating" overlay while the loop keeps refining a LIVE preview
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
                    <span className="text-[11px] font-semibold text-neutral">Still iterating…</span>
                  </div>
                </div>
              )}
              {keepFramed && (
                // #13/R2 honesty chip: the build is DONE and this is the live result — without
                // it, an ended status with a working frame reads as "is it still building?".
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
            {holdingSlow ? HOLDING_SLOW_TEXT : HOLDING_TEXT}
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
            {/* R5/N7, as everywhere else on this pane: the relaunch is offered — and PROMISED in
                the copy — only when the server confirmed a saved build (null is "store
                unreachable", which claims nothing), and a 404 after the click is said out loud
                rather than vanishing the button in silence. */}
            {relaunchError?.kind === 'not_found' ? (
              <p role="alert" className="text-xs text-danger max-w-xs leading-relaxed mb-4">
                That saved build is no longer available, so there is nothing to relaunch. The
                preview will still appear here if it finishes loading.
              </p>
            ) : (
              <p className="text-xs text-neutral/60 max-w-xs leading-relaxed mb-4">
                {hasSavedBuild === true && onRelaunch
                  ? 'It will appear here the moment it loads. If you would rather not wait, relaunch the preview to start it fresh.'
                  : 'It will appear here the moment it loads.'}
              </p>
            )}
            {hasSavedBuild === true && (
              <RelaunchAffordance
                onRelaunch={onRelaunch}
                relaunchError={relaunchError}
                label="Relaunch preview"
              />
            )}
          </div>
        )}

        {/* THE pane's live region — mounted always, empty when there is nothing to say.
            Permanent on purpose: inserting a live region together with its text announces
            inconsistently (some readers miss it entirely), so the element outlives every state
            and only its text changes. Visually hidden because every state above already SAYS
            what it is on screen; this exists for the reader that cannot see the moon icon go
            up. `polite`, never `assertive` — none of these states is an error, and the two
            that genuinely are (a failed relaunch, a failed save) keep their own `role="alert"`
            so they still cut in. */}
        <p role="status" aria-live="polite" className="sr-only">
          {announcement}
        </p>
      </div>
    </div>
  )
}
