/**
 * AN ATTACHMENT, OPENED OVER THE CONVERSATION.
 *
 * WHY THIS EXISTS: nothing here is framed, and that is diagnosed, not stylistic. A same-origin
 * `/api/attachments/{id}` frame was tried and called "diagnosed against the live configuration"
 * — it wasn't diagnosed far enough. The control plane's middleware sets `X-Frame-Options: DENY`
 * on every response, attachment downloads included (`backend/src/main.py`), and DENY forbids
 * framing by any origin, same-origin included; nginx does not strip it. A staged file's `data:`
 * URL is refused too, by `frame-src 'self'` in `nginx.conf`. A refused frame renders BLANK with
 * no `error` event to explain it — the very defect the frame was meant to fix. So there is no
 * frame: this dialog renders only what it can honestly build from bytes it already holds — an
 * `<img>` (no directive restricts one), decoded text, or a plain "can't preview" sentence. A SENT
 * document is `AttachmentChips`' job (fetch + new tab), never this component's; that's also why
 * there's no stored-address branch — every caller already holds the URL to show.
 *
 * The old `AttachmentLightbox` (55 lines, images only, hand-rolled) had no focus trap, no
 * `role="dialog"`, no scroll lock; the Radix Dialog here does, and the lightbox is gone.
 *
 * `open` is never derived from stream state — the transcript keeps streaming behind the dialog,
 * unscrolled, so closing returns the reader where they were. `onInteractOutside` and
 * `onEscapeKeyDown` are left alone on purpose: those are the reader's own two dismissals.
 */
import { useEffect, useState, type FC } from 'react'

import { TEXT_PREVIEW_MEDIA_TYPES } from '../../utils/attachmentInput'
import { decodeBase64Text } from '../../utils/attachmentStore'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

export interface PreviewTarget {
  name: string
  mediaType: string
  /** Whatever URL the caller is already holding for this file — an object URL for one the chip
   *  fetched, a `data:` URL for one staged in the composer. */
  dataUrl?: string | undefined
}

export interface AttachmentPreviewProps {
  target: PreviewTarget | null
  onClose: () => void
}

/** The decoded text of a text file's data URL, or `null` when there is nothing to decode. */
function decodedText(target: PreviewTarget): string | null {
  const url = target.dataUrl
  if (!url || !url.startsWith('data:')) return null
  const comma = url.indexOf(',')
  if (comma < 0) return null
  try {
    return decodeBase64Text(url.slice(comma + 1))
  } catch {
    return null
  }
}

const AttachmentPreview: FC<AttachmentPreviewProps> = ({ target, onClose }) => {
  const [loadFailed, setLoadFailed] = useState(false)

  // A new target is a new load; without this a previously-failed image would report failure for a
  // file that is perfectly fine.
  useEffect(() => setLoadFailed(false), [target?.dataUrl, target?.name])

  if (!target) return null
  const src = target.dataUrl ?? null
  const isImage = target.mediaType.startsWith('image/')
  // The DISPLAY set, not the transport one: whether this can be rendered in place is a
  // different question from whether it was inlined into the prompt.
  const isText = TEXT_PREVIEW_MEDIA_TYPES.has(target.mediaType)
  const text = isText ? decodedText(target) : null

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

        {/* A TEXT FILE WHOSE BYTES WILL NOT DECODE BELONGS HERE, not under the sentence below:
            there is nothing wrong with having sent it, something is wrong with the file. */}
        {src === null || loadFailed || (isText && text === null) ? (
          <p data-testid="attachment-preview-error" className="py-8 text-center text-sm text-neutral">
            This file could not be opened. It may have been removed, or your session may have
            expired — reload the page and try again.
          </p>
        ) : isImage ? (
          // NO DIRECTIVE RESTRICTS AN IMAGE. `data:`, `blob:` and same-origin all render, which is
          // why this branch never had the framing defect the others did. `onError` is load-bearing
          // HERE and only here: an `<img>` fires `error` on a 404 or an expired session.
          <img
            src={src}
            alt={target.name}
            data-testid="attachment-preview-image"
            onError={() => setLoadFailed(true)}
            className="max-h-[70vh] w-full object-contain"
          />
        ) : text !== null ? (
          <pre
            data-testid="attachment-preview-text"
            className="max-h-[70vh] overflow-auto rounded-md border border-bial-border bg-bial-bg p-3 text-xs leading-relaxed text-tertiary"
          >
            {text}
          </pre>
        ) : (
          // A PDF. There is no address for it the framing policy allows and no way to render one
          // in the page, so it says so rather than showing an empty box — which is what it did,
          // silently, because a refused frame fires no `error`.
          <p data-testid="attachment-preview-pending" className="py-8 text-center text-sm text-neutral">
            “{target.name}” will open here once you have sent it. It is attached and ready to go.
          </p>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default AttachmentPreview
