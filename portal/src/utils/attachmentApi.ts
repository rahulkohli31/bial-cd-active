/**
 * Attachment byte access over the server object-store routes (replaces the
 * retired IndexedDB engine). Image/PDF bytes are uploaded once on send and
 * fetched lazily for display; text attachments are NEVER uploaded — their
 * content travels inline as a text part (see attachmentStore.js).
 *
 * Thin wrappers over /api/attachments via authFetch (Bearer + one 401-refresh
 * retry). Deps are injectable so the module is testable without a real network
 * or token, mirroring utils/admin.js.
 */
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'

/** The one server code that means "this conversation cannot hold another file". */
export const CONVERSATION_FULL_CODE = 'CONVERSATION_ATTACHMENTS_FULL'

/** Thrown when an upload is rejected for the per-conversation cap; the UI catches it.
 *
 * IT USED TO ADVERTISE A CODE THE SERVER NO LONGER SENDS. The per-conversation BYTE budget was
 * deleted in favour of a flat file count, taking `ATTACHMENT_STORE_FULL` with it — but this class
 * hardcoded that string on every instance it threw, so a caller branching on `.code` was reading
 * a value nothing could produce. Both the throw site's test and this one now name one constant. */
export class AttachmentCapError extends Error {
  code: string
  constructor(message: string) {
    super(message)
    this.name = 'AttachmentCapError'
    this.code = CONVERSATION_FULL_CODE
  }
}

interface UploadAttachmentArgs {
  attachmentId: string
  name: string
  mediaType: string
  size: number
  base64: string
  /** The thread this file belongs to. Sent so the row carries its conversation link, which the
   *  server's per-conversation limits count over. The server has always accepted
   *  and owner-validated this field; no client ever sent it, so every stored row was NULL and a
   *  conversation-scoped count would have counted nothing. */
  conversationId?: string
}

/**
 * The uploaded ref, traced from its one real consumer (`attachmentStore.ts`'s
 * `buildUserParts`, which spreads exactly these fields into a `file` message
 * part per `messageTypes.ts`'s `FilePart` union) — `attachmentId`/`key`/`kind`/
 * `name`/`mediaType`/`size` always; `format`/`text`/`truncated`/`truncationNote`
 * for the office hybrid; `pdfFileId`/`pageCount` for the deck hybrid.
 *
 * `truncationNote` is now also on `messageTypes.ts`'s `FilePartOffice` —
 * added converting `attachmentStore.ts`, closing the gap flagged here.
 *
 * `pageCount` WENT WITH THE PAGE CAP, on both sides of the wire. Its siblings survive because
 * they still render historic messages; a page count was only ever produced by the admission check
 * that is gone, so declaring it here would promise a field the route can never send.
 */
export interface AttachmentRef {
  attachmentId: string
  key: string
  kind: string
  name: string
  mediaType: string
  size: number
  format?: string
  text?: string
  truncated?: boolean
  truncationNote?: string
  pdfFileId?: string
}

/**
 * Upload one image/PDF and return its file-part ref
 * `{ attachmentId, key, kind, name, mediaType, size }`. Text attachments must
 * NOT be passed here (they're inline). Throws AttachmentCapError when the
 * per-user cap is hit, else a generic Error with the server message.
 */
export async function uploadAttachment(
  { attachmentId, name, mediaType, size, base64, conversationId }: UploadAttachmentArgs,
  deps: AuthFetchDeps = {},
): Promise<AttachmentRef> {
  const res = await authFetch(
    '/api/attachments',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ attachmentId, name, mediaType, size, base64, conversationId }),
    },
    deps,
  )
  if (!res.ok) {
    // UNCHECKED, and reads only `.error.message`/`.error.code` (not `readApiError`'s
    // three-envelope read) — matches pre-migration behavior exactly. Flagged in the
    // migration follow-up list: a 403/500/422 shows the generic fallback instead of
    // the real server message.
    const errBody: unknown = await res.json().catch(() => ({}))
    const err = errBody as { error?: { message?: string; code?: string } }
    const message = err.error?.message || `Attachment upload failed (${res.status}).`
    if (err.error?.code === CONVERSATION_FULL_CODE) throw new AttachmentCapError(message)
    throw new Error(message)
  }
  // UNCHECKED (matches pre-migration behavior): the shape is asserted, not validated.
  const body: unknown = await res.json()
  const data = body as { attachment: AttachmentRef }
  return data.attachment
}

// Module-level object-URL cache keyed by attachmentId: a second display of the
// same image reuses the URL instead of refetching the bytes. URLs are released
// at the session boundary (revokeAllAttachmentUrls on logout), not per chip
// unmount — a shared URL must outlive any single component that shows it.
const urlCache = new Map<string, string>() // attachmentId -> resolved object URL
// In-flight fetches keyed by attachmentId so concurrent callers (StrictMode
// double-mount, or the same image shown in two chips) coalesce onto ONE request
// — without this, both pass the cache miss, both createObjectURL, and the first
// URL is orphaned (leaked, never revoked) while a redundant GET is issued.
const pendingFetches = new Map<string, Promise<string | null>>()

/**
 * Fetch an attachment's bytes and return a cached object URL (or null if the
 * object is gone / forbidden). The second call for the same id returns the
 * cached URL without a network round-trip; concurrent calls share one fetch.
 */
export async function fetchAttachmentObjectUrl(attachmentId: string, deps: AuthFetchDeps = {}): Promise<string | null> {
  if (urlCache.has(attachmentId)) return urlCache.get(attachmentId) ?? null
  if (pendingFetches.has(attachmentId)) return pendingFetches.get(attachmentId) ?? null
  const p: Promise<string | null> = (async () => {
    const res = await authFetch(`/api/attachments/${encodeURIComponent(attachmentId)}`, {}, deps)
    if (!res.ok) return null
    const url = URL.createObjectURL(await res.blob())
    urlCache.set(attachmentId, url)
    return url
  })().finally(() => pendingFetches.delete(attachmentId))
  pendingFetches.set(attachmentId, p)
  return p
}

/** Release one cached object URL (revokes + drops it so the next fetch refetches). */
export function revokeAttachmentObjectUrl(attachmentId: string): void {
  const url = urlCache.get(attachmentId)
  if (url) {
    URL.revokeObjectURL(url)
    urlCache.delete(attachmentId)
  }
}

/** Release every cached object URL (session teardown / logout). */
export function revokeAllAttachmentUrls(): void {
  for (const url of urlCache.values()) URL.revokeObjectURL(url)
  urlCache.clear()
}

/**
 * Delete one attachment object (best-effort) and drop its cached URL. For a deck,
 * pass its `pdfFileId` so the route also releases the internal Files-API PDF (the
 * bare route can't otherwise know it); the conversation-delete sweep is the
 * authoritative cleanup.
 */
export async function deleteAttachment(
  attachmentId: string,
  { pdfFileId }: { pdfFileId?: string } = {},
  deps: AuthFetchDeps = {},
): Promise<void> {
  try {
    const q = typeof pdfFileId === 'string' && pdfFileId ? `?pdfFileId=${encodeURIComponent(pdfFileId)}` : ''
    await authFetch(`/api/attachments/${encodeURIComponent(attachmentId)}${q}`, { method: 'DELETE' }, deps)
  } catch {
    // best-effort; the conversation-delete sweep is the authoritative cleanup.
  }
  revokeAttachmentObjectUrl(attachmentId)
}
