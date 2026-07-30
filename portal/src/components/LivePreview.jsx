import { useState, useEffect, useRef } from 'react'
import { Monitor, Smartphone, LayoutTemplate, PowerOff, RotateCcw, WifiOff, Save, Loader2 } from 'lucide-react'
import { relaunchRetryable } from '../utils/buildSessionTypes'

const VIEWPORTS = { Desktop: 'w-full', Mobile: 'max-w-[390px]' }
const VP_ICONS = { Desktop: Monitor, Mobile: Smartphone }

// F8/U5 — a short grace before revealing the first iframe src: the dev server binds the port a
// beat before it serves HTML, so an instant reveal flickers a "connection refused" first frame.
// Mount the iframe immediately (so it starts loading) but fade it in after the grace.
const FRAME_GRACE_MS = 400
// F8/U5 — bound the reconnecting state AFTER a completed build (its build loop is gone, so nothing
// will re-frame a dev server that never recovers): cap it, then collapse to "preview unavailable"
// + Relaunch instead of spinning forever. TUNE against real dev-server restart times.
const RECONNECT_CAP_MS = 20000

// The scheme://host[:port] of an absolute preview URL, or null if unset/malformed. Used to
// VALIDATE inbound postMessage origins (C8 §3). A malformed value fails closed (null → no
// frame trusted, every inbound message rejected).
function originOf(url) {
  try {
    return url ? new URL(url).origin : null
  } catch {
    return null
  }
}

// While the sandbox provisions and the agent builds, there is no live app to frame yet.
const LOADING_TEXT = {
  provisioning: 'Setting up your sandbox…',
  building: 'Building your app…',
}

/**
 * The relaunch error + button pair, shared by the terminal placeholder and the
 * project-has-app empty state so the U6 response matrix behaves identically in both:
 * retryable errors keep the button, `not_found` (handled by the CALLER's copy) hides it.
 */
function RelaunchAffordance({ onRelaunch, relaunchError, label }) {
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
 * @param {{
 *   previewUrl?: string | null,
 *   status?: 'provisioning' | 'building' | 'ready' | 'ended' | 'failed' | null,
 *   iterating?: boolean,
 *   onFrameMessage?: (data: unknown) => void,
 *   onRelaunch?: () => void,
 *   relaunching?: boolean,
 *   relaunchError?: import('../utils/buildSessionTypes').RelaunchError | null,
 *   lastBuildFailed?: boolean,
 *   restoredFromFailedBuild?: boolean,
 *   completedLive?: boolean,
 *   hasSavedBuild?: boolean | null,
 *   reconnecting?: boolean,
 * }} props
 */
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
  hasSavedBuild = false,
  reconnecting = false,
  // The save model (KTD-5e). `saveDirty` is TRI-STATE: true = unsaved work, false = saved,
  // null = UNKNOWN (no live workspace, or the server could not compare). Unknown must not
  // render as saved — that tells the user their work is safe when nothing checked.
  saveDirty = null,
  onSave,
  saving = false,
  saveError = null,
}) {
  const [viewport, setViewport] = useState('Desktop')

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
    const onMsg = (e) => {
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
  // The pane WOULD frame the app here (live preview or pardoned completed build). A dev-process
  // crash (`reconnecting`) pre-empts the live frame with the reconnecting/unavailable states.
  const frameContext = !relaunching && !!previewUrl && (!isTerminal || keepFramed)
  const showReconnecting = frameContext && reconnecting && !reconnectExpired
  const showUnavailable = frameContext && reconnecting && reconnectExpired
  const showFrame = frameContext && !reconnecting
  const showLoading = !isTerminal && !relaunching && !previewUrl && (status === 'provisioning' || status === 'building')
  const showEmpty = !isTerminal && !relaunching && !previewUrl && !showLoading

  // F8/U5 — grace + fade-in on the first (and each fresh) framed src. Mount the iframe immediately
  // so it loads during the grace; reveal it once the grace elapses to hide the port-bind flicker.
  const [frameReady, setFrameReady] = useState(false)
  useEffect(() => {
    if (!showFrame) {
      setFrameReady(false)
      return
    }
    const t = setTimeout(() => setFrameReady(true), FRAME_GRACE_MS)
    return () => clearTimeout(t)
  }, [showFrame, previewUrl])

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-bial-border bg-white flex-shrink-0">
        <div className="flex items-center gap-1 bg-bial-bg rounded-lg p-1">
          {Object.entries(VP_ICONS).map(([label, Icon]) => (
            <button
              key={label}
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
      </div>

      {/* Main area */}
      <div className="flex-1 flex overflow-hidden relative">
        <div className="flex-1 bg-[#e8edf2] flex justify-center p-4 overflow-auto">
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

          {showLoading && (
            <div className="flex-1 flex flex-col items-center justify-center gap-4">
              <div className="flex gap-2">
                {[0, 1, 2].map((i) => (
                  <div
                    key={i}
                    className="w-3 h-3 bg-primary rounded-full animate-bounce"
                    style={{ animationDelay: `${i * 0.2}s` }}
                  />
                ))}
              </div>
              <p className="text-sm text-neutral font-medium">{LOADING_TEXT[status] ?? 'Building your app…'}</p>
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
            </div>
          )}

          {/* F8/U5 — the dev-server PROCESS crashed after framing. A DISTINCT visual from the
              "Building…" blue bouncing dots (a spinning glyph + warning tint) so a dead frame never
              reads as "still building". Self-heals when the server restarts (a fresh preview_ready). */}
          {showReconnecting && (
            <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center" aria-live="polite" aria-busy="true">
              <RotateCcw size={26} className="text-warning animate-spin" style={{ animationDuration: '1.4s' }} />
              <p className="text-sm font-semibold text-neutral">Reconnecting to your preview…</p>
              <p className="text-xs text-neutral/60 max-w-xs leading-relaxed">
                The preview server restarted. This usually reconnects on its own in a moment.
              </p>
            </div>
          )}

          {/* F8/U5 — the bounded terminal for a crash that never recovered after a completed build:
              a plain "preview unavailable" line + the explicit Relaunch affordance, never a forever
              spinner. */}
          {showUnavailable && (
            <div className="flex-1 flex flex-col items-center justify-center text-center">
              <div className="w-16 h-16 rounded-2xl bg-gray-100 flex items-center justify-center mb-4">
                <WifiOff size={26} className="text-gray-300" />
              </div>
              <p className="text-sm font-semibold text-neutral mb-1">Preview unavailable</p>
              <p className="text-xs text-neutral/60 max-w-xs leading-relaxed mb-4">
                The preview server stopped and didn&rsquo;t come back. Relaunch it to restore your saved app.
              </p>
              <RelaunchAffordance
                onRelaunch={onRelaunch}
                relaunchError={relaunchError}
                label="Relaunch preview"
              />
            </div>
          )}

          {showTerminal && (
            <div className="flex-1 flex flex-col items-center justify-center text-center">
              <div className="w-16 h-16 rounded-2xl bg-gray-100 flex items-center justify-center mb-4">
                <PowerOff size={26} className="text-gray-300" />
              </div>
              <p className="text-sm font-semibold text-neutral mb-1">The preview is no longer running</p>
              <p className="text-xs text-neutral/60 max-w-xs leading-relaxed mb-4">
                {relaunchError?.kind === 'not_found'
                  // A definite 404: nothing to relaunch — the affordance hides (U6 matrix).
                  ? "There's nothing to relaunch yet — this project has no saved build. Build the app first."
                  : onRelaunch
                    ? 'This build session has ended. Relaunch it to restore your saved app into a fresh preview.'
                    : 'This build session has ended. Start a new build to bring the live preview back.'}
              </p>
              <RelaunchAffordance
                onRelaunch={onRelaunch}
                relaunchError={relaunchError}
                label={lastBuildFailed ? 'Relaunch last saved version' : 'Relaunch preview'}
              />
            </div>
          )}

          {showFrame && (
            <div className={`${VIEWPORTS[viewport]} h-full transition-all duration-300 rounded-xl overflow-hidden shadow-lg bg-white relative ${frameReady ? 'opacity-100' : 'opacity-0'}`}>
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
                /* Key on `previewUrl` ONLY: a NEW url (a fresh `preview_ready`) remounts and reloads
                   the frame, while a re-render with the SAME url keeps the same DOM node — no reload,
                   so the framed app's HMR websocket is not leaked on every status tick. */
                key={previewUrl}
                src={previewUrl}
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
      </div>
    </div>
  )
}
