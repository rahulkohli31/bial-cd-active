import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronUp, LogOut, MessageSquare, Info } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuItem,
} from '../ui/dropdown-menu'
import { getStoredUser, logout } from '../../utils/auth'
import { revokeAllAttachmentUrls } from '../../utils/attachmentApi'
import { useWorkspaceExit } from '../workspace/UnsavedWorkGuard'
import FeedbackModal from '../FeedbackModal'

/**
 * Who is signed in, at the foot of the navigation, with the two things that have nowhere else to
 * live: Feedback and Sign out.
 *
 * FEEDBACK IS HERE BECAUSE IT WAS A PERSISTENT CAPABILITY AND WOULD OTHERWISE HAVE VANISHED with
 * the header that carried it. Its Escape handler comes with it: `FeedbackModal` is hand-rolled
 * (`fixed inset-0`, `role="dialog"`) and its only key handler is a Tab focus trap, so the effect
 * below is the single reason Escape dismisses it at all. Radix owns Escape for the menu; it owns
 * nothing for the modal. `components/__tests__/FeedbackModal.escape.test.jsx` guards this —
 * deleting the effect leaves every menu test green while a shipped behaviour disappears.
 *
 * SIGN OUT GOES THROUGH THE UNSAVED-WORK GUARD, as it did in the header. It did not, once, and a
 * citizen could lose work by pressing the most final button on the screen, in silence. Outside a
 * workspace `exit` is a passthrough, so every other page's sign-out is unchanged.
 *
 * THE SECOND LINE IS THE EMAIL, not a role. The boards draw "Citizen developer" there; the
 * product has never had a role to show in that slot and inventing one would be a claim the
 * platform does not make — `isAdmin` is the only role it models, and the Admin entry above is
 * how that is already visible.
 */
export interface ProfileClusterProps {
  /** Avatar only, centred, at rail width. */
  collapsed?: boolean
  /** Told when the menu opens, so the rail it sits in does not collapse out from under it —
   *  the menu portals outside the nav, so reaching for it reads as the pointer leaving. */
  onMenuOpenChange?: (open: boolean) => void
}

export default function ProfileCluster({ collapsed = false, onMenuOpenChange }: ProfileClusterProps) {
  const navigate = useNavigate()
  const exit = useWorkspaceExit()
  const [menuOpen, setMenuOpen] = useState(false)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [toastMsg, setToastMsg] = useState<string | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const feedbackBtnRef = useRef<HTMLButtonElement>(null)

  const user = getStoredUser()
  const displayName = user?.display_name || user?.email || 'User'
  const secondaryLine = user?.display_name ? user?.email || '' : ''
  const avatarInitial = (user?.display_name || user?.email || 'U').charAt(0).toUpperCase()

  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setFeedbackOpen(false) }
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [])

  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current) }, [])

  const showToast = (msg: string) => {
    setToastMsg(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToastMsg(null), 3000)
  }

  const handleLogout = async () => {
    // Await the server-side revoke (bumps token_version + revokes refresh families + clears
    // cookies). `logout()` never throws. Never trap the intent to sign out — the next statement
    // always leaves.
    const ok = await logout()
    revokeAllAttachmentUrls()
    // A failed revoke is worth telling the user, but this component cannot be the one to say it:
    // `navigate` unmounts the shell in the same tick, so a toast set here would be destroyed
    // before a frame rendered it. Hand it forward as router state and let LoginPage say it.
    navigate('/login', ok ? undefined : { state: { signoutWarning: 'Sign-out may be incomplete on this device.' } })
  }

  return (
    <>
      {/* `modal={false}`, as in the header this replaces: a modal Radix menu puts `aria-hidden`
          on everything outside itself, which would hide the whole navigation from a screen
          reader for as long as this menu is open. */}
      <DropdownMenu
        modal={false}
        open={menuOpen}
        onOpenChange={(open) => {
          setMenuOpen(open)
          onMenuOpenChange?.(open)
        }}
      >
        <DropdownMenuTrigger asChild>
          <button
            data-testid="profile-cluster"
            // THE RAIL'S QUIET ZONE. Reaching this control must not be what opens the navigation:
            // it is visible and pressable at either width, so expanding the panel to reach it is
            // movement that buys nothing. `useNavRail` reads this attribute on entry.
            data-nav-quiet=""
            title={collapsed ? displayName : undefined}
            className={`flex w-full items-center border-t border-bial-border py-3 text-left transition hover:bg-surface-muted ${
              collapsed ? 'justify-center px-0' : 'gap-2.5 px-4'
            }`}
          >
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-bold text-white">
              {avatarInitial}
            </span>
            {/* FOLDED, NOT UNMOUNTED — the same rule the destination labels follow. Dropping the
                name from the DOM would leave this button announcing a single letter, so the rail's
                only route to Sign out would be an unnamed control. It is clipped to zero width
                instead, and the accessible name survives the collapse. */}
            <span
              className={`min-w-0 overflow-hidden transition-[opacity,max-width] duration-200 ${
                collapsed ? 'max-w-0 opacity-0' : 'max-w-[150px] opacity-100'
              }`}
            >
              <span className="block truncate text-[13px] font-semibold text-primary-900">{displayName}</span>
              {secondaryLine && (
                <span className="block truncate text-[11px] text-neutral">{secondaryLine}</span>
              )}
            </span>
            {!collapsed && <ChevronUp size={16} className="ml-auto shrink-0 text-neutral" />}
          </button>
        </DropdownMenuTrigger>

        {/* A CARD THAT FLOATS CLEAR OF THE NAVIGATION, not a continuation of it. It used to be
            pinned to the trigger's own width with no offset, so it rose out of the panel in the
            panel's own white and read as more navigation rather than as a menu — the boundary
            the reader needs in order to know a different thing is being offered.
            `side="right"` clears the column entirely, and the width is the MENU's own. */}
        <DropdownMenuContent
          side="right"
          align="end"
          sideOffset={10}
          className="min-w-[232px] rounded-xl border border-bial-border bg-white p-1.5 shadow-2xl"
        >
          <DropdownMenuLabel
            data-testid="user-menu-identity"
            className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 font-normal"
          >
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-bold text-white">
              {avatarInitial}
            </span>
            <span className="min-w-0">
              <span className="block truncate text-[13px] font-semibold text-primary-900">{displayName}</span>
              <span className="block truncate text-[11px] text-neutral">{secondaryLine}</span>
            </span>
          </DropdownMenuLabel>
          <div role="separator" className="my-1.5 h-px bg-bial-border" />
          <DropdownMenuItem
            onSelect={() => setFeedbackOpen(true)}
            className="gap-2.5 rounded-lg px-2.5 py-2 text-sm text-tertiary hover:bg-surface-muted focus:bg-surface-muted"
          >
            <MessageSquare size={15} />
            Feedback
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => exit(() => void handleLogout())}
            className="gap-2.5 rounded-lg px-2.5 py-2 text-sm text-danger hover:bg-red-50 focus:bg-red-50 focus:text-danger"
          >
            <LogOut size={15} />
            Sign out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <FeedbackModal
        open={feedbackOpen}
        onClose={() => setFeedbackOpen(false)}
        onSubmitted={() => { setFeedbackOpen(false); showToast('Thanks — your feedback was sent.') }}
        triggerRef={feedbackBtnRef}
      />

      {toastMsg && (
        <div className="fixed bottom-6 right-6 z-50 flex items-center gap-2 rounded-xl border border-bial-border bg-white px-4 py-3 text-sm font-medium text-tertiary shadow-xl">
          <Info size={14} className="shrink-0 text-primary" />
          {toastMsg}
        </div>
      )}
    </>
  )
}
