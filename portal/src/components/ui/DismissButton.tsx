import { X } from 'lucide-react'

/** The × that closes a red message: a 24px target in the message's own colour. */
export function DismissButton({ onDismiss }: { onDismiss: () => void }) {
  return (
    <button
      type="button"
      aria-label="Dismiss"
      onClick={onDismiss}
      className="-my-1 inline-flex h-6 w-6 flex-shrink-0 items-center justify-center rounded text-danger/70 transition hover:bg-danger/10 hover:text-danger"
    >
      <X size={12} />
    </button>
  )
}
