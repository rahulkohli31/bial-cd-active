import { useState, useEffect } from 'react'
import { FileText, FileSpreadsheet, ImageOff, Presentation, type LucideIcon } from 'lucide-react'
import { fetchAttachmentObjectUrl } from '../utils/attachmentApi'
import { openUrlInNewTab, downloadObjectUrl } from '../utils/attachmentViewer'
import { CODE_LANE_MEDIA_TYPES } from '../utils/attachmentInput'
import AttachmentPreview from './chat/AttachmentPreview'
import type { AttachmentDescriptor } from '../utils/attachmentStore'

/** Which icon a code-lane chip wears; anything else in the lane reads as a document. */
const LANE_ICONS: Record<string, LucideIcon | undefined> = {
  'text/csv': FileSpreadsheet,
  'text/tab-separated-values': FileSpreadsheet,
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': FileSpreadsheet,
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': Presentation,
}

/**
 * Renders one persisted attachment descriptor by `kind`. Images fetch bytes as an object URL for
 * an inline lightbox thumbnail; PDFs open in a new tab; a code-lane file downloads the stored
 * original, which is the same file the reader opens in the workspace; a missing image falls back
 * to an "unavailable" placeholder.
 */
function AttachmentChip({ att }: { att: AttachmentDescriptor }) {
  // THREE KINDS, ONE PER THING A CHIP CAN DO. The server derives these from the media
  // type in `chip_kind_for`, so the chip a citizen sees on reload is the same shape as the one
  // they watched appear. `document`/`image` are also matched on the media type directly, because
  // parts staged in the composer carry a locally-assigned kind.
  const isPdf = att.kind === 'document' || att.mediaType === 'application/pdf'
  const isFile = att.kind === 'file' || (!isPdf && CODE_LANE_MEDIA_TYPES.includes(att.mediaType))
  const [src, setSrc] = useState<string | null>(null)
  const [missing, setMissing] = useState(false)
  const [zoomed, setZoomed] = useState(false)

  useEffect(() => {
    // Only images preview from bytes.
    if (isPdf || isFile) return undefined
    let active = true
    fetchAttachmentObjectUrl(att.attachmentId).then((url) => {
      if (!active) return
      if (url) setSrc(url)
      else setMissing(true)
    })
    return () => {
      active = false
    }
  }, [att.attachmentId, isPdf, isFile])

  // ★ FIRST, BEFORE EVERY FORMAT BRANCH. It used to sit below them, and both
  // the file and PDF chips return above it — so `setMissing(true)` set state, the component
  // re-rendered, the early return fired again, and this was never reached. The press was
  // still absorbed in silence for exactly the two formats the branches handle, which is the
  // one thing a control must never do and the defect the state was added to fix.
  if (missing) {
    // It said only "attachment unavailable", with no name and no next step, and it
    // announced nothing — a chip that changes under a citizen's press without the page changing
    // is a change assistive technology has no other way to notice. `role="status"` is what
    // carries it; `aria-live="polite"` waits for a pause rather than interrupting.
    return (
      <span
        role="status"
        aria-live="polite"
        className="inline-flex items-center gap-1.5 bg-white/10 border border-white/20 rounded-lg px-2 py-1 text-[11px] opacity-70"
      >
        <ImageOff size={12} className="flex-shrink-0" />
        <span className="truncate max-w-[14rem]">
          {att.name ? `${att.name} is no longer available` : 'This attachment is no longer available'}
          {' — attach it again to use it.'}
        </span>
      </span>
    )
  }

  if (isFile) {
    // THE CODE LANE RETURNS THE FILE. A spreadsheet, document or deck cannot be
    // rendered in a browser without a converter this platform does not host, so pressing the chip
    // hands the citizen their own file back - under its own name and type, never anonymous bytes.
    //
    // This replaces three near-identical branches (text, office, deck). The text one was a dead
    // `<span>`: pressing it did nothing at all, because the content used to ride inline in the
    // prompt and there was nothing to fetch. Every attachment is an uploaded file now, so there
    // always is.
    const Icon = LANE_ICONS[att.mediaType] ?? FileText
    return (
      <button
        type="button"
        data-testid="attachment-download-chip"
        onClick={async () => {
          const url = await fetchAttachmentObjectUrl(att.attachmentId)
          // A FAILED FETCH SAYS SO. It used to do nothing at all, which reads as a broken
          // button - the one thing a control must never do is absorb a press silently.
          if (url) downloadObjectUrl(url, att.name)
          else setMissing(true)
        }}
        // The accessible name says WHICH file and WHAT pressing it does. A chip that
        // downloads and a chip that previews are otherwise the same shape to a screen reader.
        aria-label={`Download ${att.name}`}
        title={`Download ${att.name}`}
        className="inline-flex items-center gap-1.5 bg-white/10 hover:bg-white/20 border border-white/20 rounded-lg px-2 py-1 text-[11px] max-w-[14rem] cursor-pointer transition"
      >
        <Icon size={12} className="flex-shrink-0" />
        <span className="truncate">{att.name}</span>
      </button>
    )
  }


  if (isPdf) {
    return (
      <button
        type="button"
        onClick={async () => {
          const url = await fetchAttachmentObjectUrl(att.attachmentId)
          if (url) openUrlInNewTab(url, att.name)
          else setMissing(true)
        }}
        // Which file, and what pressing it does — a chip that opens and a chip that
        // downloads are otherwise indistinguishable to a screen reader.
        aria-label={`Open ${att.name}`}
        title={`Open ${att.name}`}
        className="inline-flex items-center gap-1.5 bg-white/10 hover:bg-white/20 border border-white/20 rounded-lg px-2 py-1 text-[11px] max-w-[12rem] cursor-pointer transition"
      >
        <FileText size={12} className="flex-shrink-0" />
        <span className="truncate">{att.name}</span>
      </button>
    )
  }


  return (
    <>
      {/* A BUTTON, NOT A CLICKABLE IMAGE. It opens a modal, so it has to be reachable and
          operable from the keyboard — an `<img onClick>` is neither, and a screen reader announced
          it as an image with no indication that pressing it did anything. */}
      <button
        type="button"
        onClick={() => src && setZoomed(true)}
        aria-label={`View ${att.name}`}
        title={`View ${att.name}`}
        className="rounded-lg cursor-zoom-in hover:opacity-90 transition focus:outline-none focus:ring-2 focus:ring-white/60"
      >
        <img
          src={src || undefined}
          alt={att.name}
          className="h-16 w-16 object-cover rounded-lg border border-white/20 bg-white/10"
        />
      </button>
      {/* The hand-rolled full-screen overlay this used to open is gone. The dialog
          that replaces it brings a focus trap, `role="dialog"`, `aria-modal` and a scroll lock —
          none of which the 55-line overlay had, and all of which a modal owes a keyboard user.
          The ALREADY-FETCHED object URL is handed over rather than the attachment id: the
          thumbnail above has the bytes, so re-addressing the same file through the API would be a
          second read of something this component is already holding. */}
      {zoomed && src && (
        <AttachmentPreview
          target={{ name: att.name, mediaType: att.mediaType, dataUrl: src }}
          onClose={() => setZoomed(false)}
        />
      )}
    </>
  )
}

export interface AttachmentChipsProps {
  attachments?: AttachmentDescriptor[]
}

export default function AttachmentChips({ attachments }: AttachmentChipsProps) {
  if (!attachments?.length) return null
  return (
    <div className="flex flex-wrap gap-2 mb-2">
      {attachments.map((att) => (
        <AttachmentChip key={att.attachmentId} att={att} />
      ))}
    </div>
  )
}
