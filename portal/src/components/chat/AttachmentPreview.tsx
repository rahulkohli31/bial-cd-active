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

/** Whatever this file can be addressed by — same-origin first, then whatever transient URL the
 *  caller is holding. Used by the IMAGE branch, which no directive restricts. */
export function attachmentSrc(target: PreviewTarget): string | null {
  if (target.attachmentId) return `/api/attachments/${encodeURIComponent(target.attachmentId)}`
  return target.dataUrl ?? null
}

/**
 * WHAT MAY BE PUT IN A FRAME, WHICH IS NOT THE SAME QUESTION (plan 002, U11).
 *
 * THE DEFECT: a STAGED file — one the citizen has attached but not sent — has no id yet, so its
 * only address is a `data:` URL. The live policy is `frame-src 'self' https://${APPS_HOSTNAME}`
 * (`nginx.conf`, declared three times), and `data:` matches neither. So the frame was BLOCKED, and
 * a blocked frame renders BLANK: `<iframe>` fires no `error` for a CSP refusal, so the fallback
 * sentence never appeared either. A citizen opening a PDF they had just attached got an empty box.
 *
 * DIAGNOSED AGAINST THE LIVE CONFIGURATION, not against the comments. The resource directives most
 * of this repo's comments still cite were deleted with the retired serving path and `csp.py` no
 * longer exists; `nginx.conf` and the Caddyfile in front of it are the only authorities left, and
 * both say the same thing about framing.
 *
 * So only a same-origin address is framed. Everything else is rendered by a branch that does not
 * need a frame at all, or says plainly why it cannot be shown yet.
 */
function framableSrc(target: PreviewTarget): string | null {
  return target.attachmentId ? `/api/attachments/${encodeURIComponent(target.attachmentId)}` : null
}

/** The decoded text of a staged file's data URL, or `null` for anything else. */
function stagedText(target: PreviewTarget): string | null {
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
  const [frameFailed, setFrameFailed] = useState(false)

  // A new target is a new load; without this a previously-failed frame would report failure for a
  // file that is perfectly fine.
  useEffect(() => setFrameFailed(false), [target?.attachmentId, target?.name])

  if (!target) return null
  const src = attachmentSrc(target)
  const isImage = target.mediaType.startsWith('image/')
  const isText = TEXT_MEDIA_TYPES.has(target.mediaType)
  const framable = framableSrc(target)
  const text = isText ? stagedText(target) : null

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
          // NO DIRECTIVE RESTRICTS AN IMAGE. `data:`, `blob:` and same-origin all render, which is
          // why this branch never had the framing defect the others did.
          <img
            src={src}
            alt={target.name}
            data-testid="attachment-preview-image"
            onError={() => setFrameFailed(true)}
            className="max-h-[70vh] w-full object-contain"
          />
        ) : text !== null ? (
          // A TEXT FILE NEEDS NO FRAME AT ALL. It rode in a frame only because everything that was
          // not an image did, and a `data:` frame is exactly what the policy blocks. The bytes are
          // already here; showing them is both correct and better than a PDF viewer would be.
          <pre
            data-testid="attachment-preview-text"
            className="max-h-[70vh] overflow-auto rounded-md border border-bial-border bg-bial-bg p-3 text-xs leading-relaxed text-tertiary"
          >
            {text}
          </pre>
        ) : framable !== null ? (
          <iframe
            src={framable}
            title={target.name}
            data-testid="attachment-preview-frame"
            onError={() => setFrameFailed(true)}
            className="h-[70vh] w-full rounded-md border border-bial-border"
          />
        ) : (
          // A STAGED PDF. There is no address for it that the framing policy allows and no way to
          // render one in the page, so it says so rather than showing an empty box — which is what
          // it did, silently, because a CSP-blocked frame fires no `error`.
          <p data-testid="attachment-preview-pending" className="py-8 text-center text-sm text-neutral">
            “{target.name}” will open here once you have sent it. It is attached and ready to go.
          </p>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default AttachmentPreview
