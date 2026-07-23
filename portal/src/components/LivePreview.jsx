import { useState, useEffect, useRef } from 'react'
import { Monitor, Smartphone, LayoutTemplate, PowerOff, RotateCcw } from 'lucide-react'
import { relaunchRetryable } from '../utils/buildSessionTypes'

const VIEWPORTS = { Desktop: 'w-full', Mobile: 'max-w-[390px]' }
const VP_ICONS = { Desktop: Monitor, Mobile: Smartphone }

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
  const showFrame = !relaunching && !!previewUrl && (!isTerminal || keepFramed)
  const showLoading = !isTerminal && !relaunching && !previewUrl && (status === 'provisioning' || status === 'building')
  const showEmpty = !isTerminal && !relaunching && !previewUrl && !showLoading

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
              <p className="text-xs text-neutral/60 max-w-xs leading-relaxed">
                Submit a prompt to start a build — the live app appears here once its dev server is up.
              </p>
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
                  {lastBuildFailed ? 'Relaunch last saved version' : 'Relaunch preview'}
                </button>
              )}
            </div>
          )}

          {showFrame && (
            <div className={`${VIEWPORTS[viewport]} h-full transition-all duration-300 rounded-xl overflow-hidden shadow-lg bg-white relative`}>
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
