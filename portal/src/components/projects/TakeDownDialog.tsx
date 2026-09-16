/**
 * Confirmation for taking a live application out of production.
 *
 * WHY A TAKE-DOWN GETS ASKED ABOUT AT ALL. It is the one control on the list that changes what
 * every other person at BIAL can reach — the application stops answering for all of them, at the
 * moment it is pressed. It sat one click away, unguarded, in a menu whose neighbour (Delete) asks
 * for a written reason; the friction was calibrated backwards against the blast radius.
 *
 * AND WHY IT ASKS THIS LIGHTLY. A take-down keeps everything: the version, the data, the chats,
 * the review it already passed. Publishing again puts the same application back. Asking for a
 * typed reason would price a reversible act like an irreversible one and teach people to type
 * past both.
 */
import { PowerOff } from 'lucide-react'
import ConfirmDialog from '../ui/ConfirmDialog'

export interface TakeDownDialogProps {
  appName: string
  onClose: () => void
  onConfirm: () => void | Promise<void>
}

export default function TakeDownDialog({
  appName,
  onClose,
  onConfirm,
}: TakeDownDialogProps): React.JSX.Element {
  return (
    <ConfirmDialog
      testId="take-down"
      title={`Take “${appName}” out of production?`}
      body="Everyone using it now loses access, straight away. Nothing is deleted — the version, its data and its chats all stay, and publishing again puts it back."
      icon={<PowerOff size={17} className="text-status-amber-fg" />}
      iconClassName="bg-status-amber-bg"
      confirmLabel="Take it down"
      tone="danger"
      onClose={onClose}
      onConfirm={onConfirm}
    />
  )
}
