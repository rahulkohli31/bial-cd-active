/**
 * Open an attachment's bytes (held as base64) in a new browser tab via a
 * short-lived blob: URL. Used for PDFs (and any non-image type): the strict
 * main-app CSP blocks embedding data:/blob: documents in an iframe — the same
 * gotcha the builder preview hit — but a top-level navigation to a blob: URL is
 * allowed.
 *
 * We open via a user-gesture <a target="_blank"> click rather than
 * window.open(): window.open with `noopener` returns null even on success, so
 * its return value can't tell "popup blocked" from "opened fine". An anchor
 * click from the originating user gesture isn't popup-blocked and needs no such
 * check. No `download` attribute → the browser renders the PDF inline.
 */

/** Decode raw base64 (no data: prefix) into a typed Blob. */
export function base64ToBlob(base64: string, mediaType: string): Blob {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return new Blob([bytes], { type: mediaType })
}

/**
 * Open `base64` bytes (default application/pdf) in a new tab. Returns false if
 * there are no bytes or the blob can't be built, so callers can surface a
 * "no longer available" message. The blob URL is revoked after a delay so the
 * opened tab has time to read it.
 */
export function openAttachmentBytes(base64: string, name?: string, mediaType = 'application/pdf'): boolean {
  if (!base64) return false
  let url: string
  try {
    url = URL.createObjectURL(base64ToBlob(base64, mediaType))
  } catch {
    return false
  }
  const a = document.createElement('a')
  a.href = url
  a.target = '_blank'
  a.rel = 'noopener noreferrer'
  if (name) a.title = name
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
  return true
}

/** Convenience alias for the common PDF case. */
export function openPdf(base64: string, name?: string): boolean {
  return openAttachmentBytes(base64, name, 'application/pdf')
}

/**
 * Open an EXISTING object URL (e.g. one served by attachmentApi and cached) in a
 * new tab via the same user-gesture anchor click. Unlike openAttachmentBytes it
 * does NOT revoke the URL — the caller's cache owns its lifetime. Returns false
 * if there's no URL.
 */
export function openUrlInNewTab(url: string, name?: string): boolean {
  if (!url) return false
  const a = document.createElement('a')
  a.href = url
  a.target = '_blank'
  a.rel = 'noopener noreferrer'
  if (name) a.title = name
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  return true
}

/**
 * Trigger a DOWNLOAD of an existing (cached) object URL under `name`. Office
 * originals are served as octet-stream (the server can't tell `.docx` from
 * `.xlsx` by bytes), so the filename + extension come from the part's `name` via
 * the `download` attribute — that's what gives the saved file its correct
 * extension (Decision 9). Does NOT revoke the URL (the caller's cache owns it).
 */
export function downloadObjectUrl(url: string, name?: string): boolean {
  if (!url) return false
  const a = document.createElement('a')
  a.href = url
  a.download = name || 'download'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  return true
}
