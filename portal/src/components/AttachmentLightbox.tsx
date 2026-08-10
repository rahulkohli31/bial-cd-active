import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'

/**
 * Full-screen overlay that shows one image attachment at full size. Reuses the
 * app's inline modal idiom (fixed inset-0 backdrop, high z-index); closes on
 * backdrop click, the × button, or Esc. Images only — `src` is a data: URL,
 * which the main-app CSP already allows (it's how thumbnails render).
 */
export interface AttachmentLightboxProps {
  name?: string | null
  src?: string | null
  onClose: () => void
}

export default function AttachmentLightbox({ name, src, onClose }: AttachmentLightboxProps) {
  // Keep onClose in a ref so the keydown listener subscribes ONCE for the
  // lightbox's lifetime. Callers pass an inline `() => setViewer(null)` that's a
  // new closure each render, so depending on it directly would re-subscribe on
  // every parent render (e.g. once per streamed chunk while open).
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (!src) return null

  return (
    <div
      className="fixed inset-0 bg-black/80 z-[60] flex flex-col items-center justify-center p-4"
      onClick={onClose}
    >
      <button
        onClick={onClose}
        title="Close preview"
        aria-label="Close preview"
        className="absolute top-4 right-4 text-white/80 hover:text-white transition p-2"
      >
        <X size={22} />
      </button>
      <img
        src={src}
        alt={name || 'attachment'}
        onClick={(e) => e.stopPropagation()}
        className="max-w-[92vw] max-h-[85vh] object-contain rounded-lg shadow-2xl"
      />
      {name && <p className="mt-3 text-xs text-white/70 truncate max-w-[92vw]">{name}</p>}
    </div>
  )
}
