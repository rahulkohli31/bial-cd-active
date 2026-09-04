/**
 * Open or save an attachment the server already serves, from the object URL
 * `attachmentApi` cached for it. Two helpers, one technique.
 *
 * A NEW TAB RATHER THAN A FRAME. The strict main-app CSP blocks embedding a
 * blob:/data: document in an iframe — the same gotcha the builder preview hit —
 * so a PDF, or any other non-image type, cannot be shown in place. A top-level
 * navigation to the object URL is allowed, and is what these do.
 *
 * AN ANCHOR CLICK RATHER THAN window.open(). window.open with `noopener`
 * returns null even on success, so its return value can't tell "popup blocked"
 * from "opened fine". An anchor click from the originating user gesture isn't
 * popup-blocked and needs no such check. What separates viewing from saving is
 * the `download` attribute alone: absent below, the browser renders the PDF
 * inline; present, it writes the file out.
 *
 * NEITHER REVOKES THE URL. Both take one the caller already holds, and the
 * caller's cache owns its lifetime.
 */

/**
 * Open an EXISTING object URL (e.g. one served by attachmentApi and cached) in a
 * new tab via a user-gesture anchor click. Returns false if there's no URL.
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
 * extension (Decision 9).
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
