/**
 * A centered pop-up that renders the project's current app ON DEMAND.
 *
 * Why a modal and not an inline panel: the live preview is heavy (a same-origin
 * /preview iframe). Keeping it mounted on the project landing every time is wasteful
 * when most visits are to build, not to look — so the app lives behind a "View app"
 * button in the right rail and only mounts while this dialog is open.
 *
 * Read-only: LivePreview is fed the app's stored current code with NO data-service
 * `config`/token/user, so this passive view issues no writes into the app's real
 * store (same posture the old inline landing preview had). LivePreview owns its own
 * Desktop/Mobile toggle and View-Code panel, so relocating it here keeps every
 * affordance the old inline block had.
 */
import { useEffect } from 'react'
import { X } from 'lucide-react'
import LivePreview from '../LivePreview'

export interface AppPreviewModalProps {
  projectName: string
  previewCode: string | null
  onClose: () => void
}

export default function AppPreviewModal({
  projectName,
  previewCode,
  onClose,
}: AppPreviewModalProps): React.JSX.Element {
  // Escape closes — parity with the app's other dialogs (ProjectDeleteDialog et al.).
  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope"
      role="dialog"
      aria-modal="true"
      aria-label={`Preview of ${projectName}`}
    >
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-5xl h-[85vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-5 py-3 border-b border-bial-border flex-shrink-0">
          <h3 className="text-sm font-bold text-tertiary truncate">{projectName}</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close app preview"
            className="p-1.5 rounded-lg text-neutral hover:text-tertiary hover:bg-surface-muted transition"
          >
            <X size={16} />
          </button>
        </div>
        <div className="flex-1 min-h-0">
          <LivePreview
            previewCode={previewCode}
            generating={false}
            generationStage={0}
            config={undefined}
            accessToken={undefined}
            user={undefined}
          />
        </div>
      </div>
    </div>
  )
}
