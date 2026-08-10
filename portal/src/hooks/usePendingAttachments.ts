import { useState, useRef, useCallback, useEffect } from 'react'
import type { ChangeEvent } from 'react'
import { validateAttachmentFiles, fileToBase64, newAttachmentId, resolveMediaType, textAttachmentBytes } from '../utils/attachmentInput'
import type { PendingAttachment } from '../utils/attachmentInput'

export interface UsePendingAttachmentsResult {
  pendingAttachments: PendingAttachment[]
  handleFileSelect: (e: ChangeEvent<HTMLInputElement>) => Promise<void>
  removePending: (id: string) => void
  clearPending: () => void
  attachToast: string | null
  showAttachToast: (msg: string) => void
}

/**
 * Shared composer state for image/PDF attachments (used by ChatPage and
 * BuilderPage). Owns the pending-attachment list (each item carries transient
 * base64 until the message is sent) and a short-lived validation/cap toast.
 * Each page renders the toast + preview row in its own style; the behaviour
 * lives here so the two composers can't drift.
 */
export function usePendingAttachments(): UsePendingAttachmentsResult {
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([])
  const [attachToast, setAttachToast] = useState<string | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current)
  }, [])

  const showAttachToast = useCallback((msg: string) => {
    setAttachToast(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setAttachToast(null), 3500)
  }, [])

  const handleFileSelect = useCallback(
    async (e: ChangeEvent<HTMLInputElement>) => {
      const incoming = Array.from(e.target.files || [])
      e.target.value = '' // allow re-selecting the same file later
      if (incoming.length === 0) return
      // Pass the bytes of text files already pending so the text budget is
      // enforced cumulatively across picks, not reset per selection.
      const result = validateAttachmentFiles(incoming, pendingAttachments.length, textAttachmentBytes(pendingAttachments))
      if ('error' in result) {
        showAttachToast(result.error)
        return
      }
      try {
        const read = await Promise.all(
          incoming.map(async (file): Promise<PendingAttachment> => ({
            id: newAttachmentId(),
            name: file.name,
            // Resolve so an OS-mislabeled CSV stores its canonical text/csv type
            // — the same type the validator allowed it under (Decision 3).
            mediaType: resolveMediaType(file),
            size: file.size,
            base64: await fileToBase64(file),
          })),
        )
        setPendingAttachments((prev) => [...prev, ...read])
      } catch {
        showAttachToast('Could not read the selected file.')
      }
    },
    [pendingAttachments, showAttachToast],
  )

  const removePending = useCallback((id: string) => {
    setPendingAttachments((prev) => prev.filter((a) => a.id !== id))
  }, [])

  const clearPending = useCallback(() => setPendingAttachments([]), [])

  return { pendingAttachments, handleFileSelect, removePending, clearPending, attachToast, showAttachToast }
}
