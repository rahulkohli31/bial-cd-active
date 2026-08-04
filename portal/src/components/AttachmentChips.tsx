import { useState, useEffect } from 'react'
import { FileText, FileSpreadsheet, ImageOff, Presentation } from 'lucide-react'
import { fetchAttachmentObjectUrl } from '../utils/attachmentApi'
import { openUrlInNewTab, downloadObjectUrl } from '../utils/attachmentViewer'
import AttachmentLightbox from './AttachmentLightbox'
import type { AttachmentDescriptor } from '../utils/attachmentStore'

/**
 * Render one persisted attachment descriptor `{ attachmentId, kind, name,
 * mediaType, format?, truncated? }` (derived from a message's parts). Images
 * fetch their bytes from the server object store as an object URL and show an
 * inline thumbnail that opens a lightbox; PDFs open in a new tab on click;
 * text/CSV show a labelled file-icon chip (no byte read — the content travelled
 * inline in the prompt); Word/Excel show a labelled chip that re-downloads the
 * ORIGINAL file (the model only ever saw extracted text); a PowerPoint deck
 * shows a labelled chip that re-downloads the ORIGINAL .pptx (the conversion is
 * internal — the chip never reveals it); an image whose bytes are gone/forbidden
 * shows an "unavailable" placeholder.
 */
function AttachmentChip({ att }: { att: AttachmentDescriptor }) {
  const isText = att.kind === 'text'
  const isOffice = att.kind === 'office'
  const isDeck = att.kind === 'deck'
  const isPdf = att.kind === 'document' || att.mediaType === 'application/pdf'
  const [src, setSrc] = useState<string | null>(null)
  const [missing, setMissing] = useState(false)
  const [zoomed, setZoomed] = useState(false)

  useEffect(() => {
    // Only images preview from bytes.
    if (isPdf || isText || isOffice || isDeck) return undefined
    let active = true
    fetchAttachmentObjectUrl(att.attachmentId).then((url) => {
      if (!active) return
      if (url) setSrc(url)
      else setMissing(true)
    })
    return () => {
      active = false
    }
  }, [att.attachmentId, isPdf, isText, isOffice, isDeck])

  if (isText) {
    const Icon = att.mediaType === 'text/csv' ? FileSpreadsheet : FileText
    return (
      <span
        title={att.name}
        className="inline-flex items-center gap-1.5 bg-white/10 border border-white/20 rounded-lg px-2 py-1 text-[11px] max-w-[12rem]"
      >
        <Icon size={12} className="flex-shrink-0" />
        <span className="truncate">{att.name}</span>
      </span>
    )
  }

  if (isOffice) {
    const Icon = att.format === 'excel' ? FileSpreadsheet : FileText
    // When the AI received a shortened version, spell out what was dropped on hover
    // (with the real row counts when available); fall back for older parts.
    const truncMsg = att.truncated
      ? att.truncationNote || 'This file was shortened for the AI. Download the original for the full content.'
      : ''
    return (
      <button
        type="button"
        onClick={async () => {
          const url = await fetchAttachmentObjectUrl(att.attachmentId)
          if (url) downloadObjectUrl(url, att.name)
        }}
        title={truncMsg ? `${truncMsg} (Click to download the original.)` : `Download ${att.name}`}
        className="inline-flex items-center gap-1.5 bg-white/10 hover:bg-white/20 border border-white/20 rounded-lg px-2 py-1 text-[11px] max-w-[14rem] cursor-pointer transition"
      >
        <Icon size={12} className="flex-shrink-0" />
        <span className="truncate">{att.name}</span>
        {att.truncated && (
          <span title={truncMsg} className="flex-shrink-0 opacity-70">· truncated</span>
        )}
      </button>
    )
  }

  if (isDeck) {
    // A deck re-downloads the ORIGINAL .pptx (exactly like office). The chip shows
    // only the .pptx name + a Presentation icon and the tooltip never mentions PDF
    // — the conversion is internal (invisible-conversion user story).
    return (
      <button
        type="button"
        data-testid="deck-download-chip"
        onClick={async () => {
          const url = await fetchAttachmentObjectUrl(att.attachmentId)
          if (url) downloadObjectUrl(url, att.name)
        }}
        title={`Download ${att.name}`}
        className="inline-flex items-center gap-1.5 bg-white/10 hover:bg-white/20 border border-white/20 rounded-lg px-2 py-1 text-[11px] max-w-[14rem] cursor-pointer transition"
      >
        <Presentation size={12} className="flex-shrink-0" />
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
        }}
        title={`Open ${att.name}`}
        className="inline-flex items-center gap-1.5 bg-white/10 hover:bg-white/20 border border-white/20 rounded-lg px-2 py-1 text-[11px] max-w-[12rem] cursor-pointer transition"
      >
        <FileText size={12} className="flex-shrink-0" />
        <span className="truncate">{att.name}</span>
      </button>
    )
  }

  if (missing) {
    return (
      <span className="inline-flex items-center gap-1.5 bg-white/10 border border-white/20 rounded-lg px-2 py-1 text-[11px] opacity-70">
        <ImageOff size={12} className="flex-shrink-0" />
        attachment unavailable
      </span>
    )
  }

  return (
    <>
      <img
        src={src || undefined}
        alt={att.name}
        title={`View ${att.name}`}
        onClick={() => src && setZoomed(true)}
        className="h-16 w-16 object-cover rounded-lg border border-white/20 bg-white/10 cursor-zoom-in hover:opacity-90 transition"
      />
      {zoomed && <AttachmentLightbox name={att.name} src={src} onClose={() => setZoomed(false)} />}
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
