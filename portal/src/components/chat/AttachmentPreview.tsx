/**
 * AN ATTACHMENT, OPENED OVER THE CONVERSATION (R47, R50, R52, R64).
 *
 * ══ NOTHING HERE IS FRAMED, AND THE AUTHORITY ON THAT IS THE CONTROL PLANE ══
 *
 * This file used to frame a same-origin `/api/attachments/{id}` address and said it had been
 * "diagnosed against the live configuration". It had not been diagnosed far enough. The control
 * plane's own middleware sets `X-Frame-Options: DENY` on EVERY response it makes, the attachment
 * download included (`backend/src/main.py`), and DENY forbids framing by any origin — same-origin
 * included. nginx does not strip it. So the one address that revision recommended is exactly the
 * one the browser refuses, and a refused frame renders BLANK with no `error` event to explain it:
 * the very defect the frame was introduced to fix. A staged file's `data:` URL is refused too, by
 * `frame-src 'self'` in `nginx.conf`.
 *
 * With both addresses refused there is no frame worth keeping, so there is none. What this dialog
 * shows is what it can honestly render from bytes it is already holding:
 *
 *   · AN IMAGE — no directive restricts an `<img>`, so `data:`, `blob:` and same-origin all work.
 *   · A TEXT FILE — decoded and shown as text. It only ever rode in a frame because everything
 *     that was not an image did, and showing the text is the better rendering regardless.
 *   · ANYTHING ELSE — a sentence saying so, where the defect drew an empty rectangle.
 *
 * A SENT DOCUMENT IS NOT THIS COMPONENT'S JOB. `AttachmentChips` fetches it and opens it in a new
 * tab, which is the one presentation the framing policy leaves available; this dialog is never
 * asked for one. That is also why there is no stored-address branch here: every caller hands over
 * a URL it already holds — the object URL the chip fetched, or the staged file's data URL.
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

import { TEXT_MEDIA_TYPES } from '../../utils/attachmentInput'
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
  const isText = TEXT_MEDIA_TYPES.has(target.mediaType)
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
