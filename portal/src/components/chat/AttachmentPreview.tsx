/**
 * AN ATTACHMENT, OPENED OVER THE CONVERSATION (R47, R50, R52, R64).
 *
 * ══ AN IFRAME ON THE EXISTING ENDPOINT, NOT `react-pdf` ══
 *
 * THE CSP DECIDES IT. `portal/nginx.conf` sets `frame-src 'self' https://${APPS_HOSTNAME}` (and
 * re-declares it twice), so `blob:` and `data:` are NOT framable — which is exactly why today's
 * path builds a blob URL and opens a NEW BROWSER TAB instead, leaving the conversation behind.
 * A same-origin `/api/attachments/{id}` satisfies `frame-src 'self'` with zero CSP, backend or
 * bundle change, and gets the browser's native PDF viewer for free.
 *
 * `react-pdf` was rejected on cost: it pins `pdfjs-dist` at ~34 MB unpacked with worker wiring
 * that interacts with the existing `manualChunks`.
 *
 * THE ONE THING A PLAIN IFRAME DOES NOT GET is `authFetch`'s 401-refresh-and-retry. An expired
 * session shows the API's own error document INSIDE the frame rather than silently refreshing.
 * That is not a blank — the reader sees the server's response — and it is also not something this
 * component can improve on: an `<iframe>` does not fire `error` for a 401 or a 404, it renders
 * what came back. `onError` is therefore load-bearing for the IMAGE branch (an `<img>` does fire)
 * and belt-and-braces on the frame; the fallback sentence's real reachable cases are an image that
 * failed to load and a target with no address at all.
 *
 * ══ WHAT THE DIALOG BRINGS THAT THE OLD LIGHTBOX LACKED ══
 *
 * `AttachmentLightbox` is 55 lines, images only, hand-rolled: no focus trap, no `role="dialog"`,
 * no `aria-modal`, no scroll lock, and it closes on a backdrop click with nothing returning focus.
 * The Radix Dialog brings all of that, and U17 deletes the lightbox.
 *
 * ══ R47 — NOTHING BUT THE READER DISMISSES IT ══
 *
 * `open` is NEVER derived from stream state. The transcript keeps streaming behind the dialog and
 * is not scrolled; closing returns the reader exactly where they were. `onInteractOutside` and
 * `onEscapeKeyDown` are left alone deliberately — those are the reader's two dismissals, and
 * overriding them is how a modal becomes a trap.
 */
import { useEffect, useState, type FC } from 'react'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

export interface PreviewTarget {
  /** The stored attachment's id — what the same-origin endpoint is addressed by. */
  attachmentId?: string | undefined
  name: string
  mediaType: string
  /** For a staged file that is not on the server yet, its transient data URL. */
  dataUrl?: string | undefined
}

export interface AttachmentPreviewProps {
  target: PreviewTarget | null
  onClose: () => void
}

/** Same-origin, so `frame-src 'self'` allows it. This is the whole mechanism. */
export function attachmentSrc(target: PreviewTarget): string | null {
  if (target.attachmentId) return `/api/attachments/${encodeURIComponent(target.attachmentId)}`
  return target.dataUrl ?? null
}

const AttachmentPreview: FC<AttachmentPreviewProps> = ({ target, onClose }) => {
  const [frameFailed, setFrameFailed] = useState(false)

  // A new target is a new load; without this a previously-failed frame would report failure for a
  // file that is perfectly fine.
  useEffect(() => setFrameFailed(false), [target?.attachmentId, target?.name])

  if (!target) return null
  const src = attachmentSrc(target)
  const isImage = target.mediaType.startsWith('image/')

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent className="max-w-3xl" data-testid="attachment-preview">
        <DialogHeader>
          <DialogTitle className="truncate text-sm">{target.name}</DialogTitle>
          <DialogDescription className="sr-only">
            A preview of {target.name}. The conversation stays open behind it.
          </DialogDescription>
        </DialogHeader>

        {src === null || frameFailed ? (
          <p data-testid="attachment-preview-error" className="py-8 text-center text-sm text-neutral">
            This file could not be opened. It may have been removed, or your session may have
            expired — reload the page and try again.
          </p>
        ) : isImage ? (
          <img
            src={src}
            alt={target.name}
            data-testid="attachment-preview-image"
            onError={() => setFrameFailed(true)}
            className="max-h-[70vh] w-full object-contain"
          />
        ) : (
          <iframe
            src={src}
            title={target.name}
            data-testid="attachment-preview-frame"
            onError={() => setFrameFailed(true)}
            className="h-[70vh] w-full rounded-md border border-bial-border"
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

export default AttachmentPreview
