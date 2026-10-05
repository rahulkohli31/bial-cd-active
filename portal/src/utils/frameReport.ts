/**
 * What a framed generated app says about itself, and the gate a listener puts it through.
 *
 * Every generated app is served from one origin, so each half of the gate answers a different
 * question: the origin says "an app", the sender's window says "the app in this frame", and the
 * path the report carries says "the app at the address this frame was given". Dropping any of
 * the three is a regression, not a simplification.
 */
import { isRecord } from './apiError'

// THE WIRE, matched by value on both sides (`sandbox/template/instrumentation-client.ts`).
export const MOUNTED_TYPE = 'bial:app-mounted'
export const PAINTING_TYPE = 'bial:app-painting'
export const PING_TYPE = 'bial:ping'

// The path an address frames, without its trailing slash, for the identity half of the report
// check below. Malformed fails closed to null, exactly as `originOf` does.
export function framedPathOf(url: string | null): string | null {
  try {
    if (!url) return null
    return new URL(url).pathname.replace(/\/+$/, '')
  } catch {
    return null
  }
}

/** Is this inbound frame message the document at `framedPath` speaking for itself, with this
 *  `type`? Shape and identity — provenance (origin and window) is `isFromFrame`'s, checked first.
 *  Every generated app shares one origin, so the window check alone says "an app"; the path the
 *  message reports is what says "the app at this address", and a frame that navigated itself to
 *  another app reports that app's path and is not taken for this one. An address framed at the
 *  origin's root (framed path `''`) accepts any path — there is nothing to discriminate under it,
 *  and that is the shape the origin check alone already covers. */
export function isFrameReportFor(data: unknown, type: string, framedPath: string | null): boolean {
  if (!isRecord(data) || data.type !== type) return false
  if (framedPath === null || typeof data.path !== 'string') return false
  const reported = data.path.replace(/\/+$/, '')
  return reported === framedPath || reported.startsWith(`${framedPath}/`)
}

// The scheme://host[:port] of an absolute preview URL, or null if unset/malformed. Used to
// VALIDATE inbound postMessage origins. A malformed value fails closed (null → no frame
// trusted, every inbound message rejected).
export function originOf(url: string | null): string | null {
  try {
    if (!url) return null
    const origin = new URL(url).origin
    // An opaque origin (a data: URL, about:blank, or a sandboxed iframe without
    // allow-same-origin) doesn't throw and doesn't return null — new URL(...).origin
    // is the STRING "null" for it (confirmed: new URL('data:text/html,x').origin ===
    // "null"). That string is truthy, so without this check it would pass the `!origin`
    // guard in `isFromFrame` and every opaque-origin document's postMessage (whose real
    // e.origin is also the string "null") would be trusted. Folded into the same
    // fails-closed return as a malformed URL.
    return origin === 'null' ? null : origin
  } catch {
    return null
  }
}

/** Did `e` come from the document in `frameWindow`, at `origin`? Provenance only: what the
 *  message says is the caller's to narrow. A frame that is not mounted trusts nothing. */
export function isFromFrame(
  e: MessageEvent,
  origin: string | null,
  frameWindow: Window | null | undefined,
): frameWindow is Window {
  if (!origin || e.origin !== origin) return false
  // Null-guarded rather than compared bare: `e.source !== frameWindow` is one character (`!=`)
  // away from accepting every source-less message, because null == undefined.
  return !!frameWindow && e.source === frameWindow
}

/** The document framed at `framedUrl`, in `frameWindow`, reporting `type` about itself. */
export function isTrustedFrameReport(
  e: MessageEvent,
  framedUrl: string | null,
  frameWindow: Window | null | undefined,
  type: string,
): boolean {
  return isFromFrame(e, originOf(framedUrl), frameWindow) && isFrameReportFor(e.data, type, framedPathOf(framedUrl))
}
