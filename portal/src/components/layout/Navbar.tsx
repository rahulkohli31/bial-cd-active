import { useState, useRef, useEffect } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useWorkspaceExit } from '../workspace/UnsavedWorkGuard'
// `Info` is NOT left over from the removed settings menu — it is the toast's own icon
// (see the toast render below). The nine icons that went with the deleted header controls
// are gone; these four all have live consumers.
import { ChevronDown, LogOut, Info, MessageSquare, Plug } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuItem,
} from '../ui/dropdown-menu'
import { getStoredUser, isAuthenticated, logout } from '../../utils/auth'
import { fetchUsageToday, onUsageChanged } from '../../utils/usage'
import type { UsageToday } from '../../utils/usage'
import { revokeAllAttachmentUrls } from '../../utils/attachmentApi'
import { fetchAppStatusCounts } from '../../utils/appRegistryApi'
import { projectsListHref, rememberProjectsSearch } from '../../utils/projectsListMemory'
import WaitingCountBadge from '../admin/WaitingCountBadge'
import FeedbackModal from '../FeedbackModal'
import IntegrationsDialog from '../connectors/IntegrationsDialog'
import BIALLogo from '../BIALLogo'

const NAV_LINKS = [
  { label: 'Projects', to: '/projects' },
  { label: 'Marketplace', to: '/marketplace' },
  { label: 'Help', to: '/help' },
]

const ADMIN_LINK = { label: 'Admin', to: '/admin' }

/**
 * Tokens at a glance for the narrow-screen meter: "48K", "1.2M". The full
 * `12,345 / 50,000 tokens` reading stays on md and up — this is the same fact, short enough
 * to survive a phone-width navbar rather than being hidden there.
 */
const _compactTokenFormat = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
})
const compactTokens = (n: number): string => _compactTokenFormat.format(n)

export default function Navbar() {
  const navigate = useNavigate()
  // The workspace's unsaved-work guard, or a pass-through on every page that has no workspace.
  const exit = useWorkspaceExit()

  // THE HALF THE ADDRESS BAR CANNOT DO BY ITSELF. `ProjectsPage` mounts its own instance of this
  // same component, so THIS is the one place already positioned to notice every time the address
  // bar reads `/projects` and remember what it carried. The brand link below reads it back when
  // leaving from somewhere else, rather than resetting the list to page one.
  const location = useLocation()
  useEffect(() => {
    if (location.pathname === '/projects') rememberProjectsSearch(location.search)
  }, [location.pathname, location.search])
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [toastMsg, setToastMsg] = useState<string | null>(null)
  const [usage, setUsage] = useState<UsageToday | null>(null)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  // THE ONLY NEW DOOR (R5, the `OpenIt` board's own annotation). No Settings link, no route:
  // Integrations opens from this menu, on every screen, as a dialog over whatever was underneath.
  // Conditionally mounted like every other dialog in this portal.
  const [integrationsOpen, setIntegrationsOpen] = useState(false)
  // How many apps are waiting for an administrator. `null` = we have not asked, or
  // the ask failed — never rendered as a number, and never asked for at all unless this
  // user is a superadmin (see the effect below).
  const [waiting, setWaiting] = useState<number | null>(null)
  // The /auth/me profile carries { id, email, display_name, is_admin, limits, chat_kinds } —
  // role IS on it, and this component depends on that three lines later: `isAdmin` gates both
  // the waiting-count fetch and the admin link. Only the DISPLAY bits are derived, and only
  // because the profile has no separate name/username field.
  const user = getStoredUser()
  const displayName = user?.display_name || user?.email || 'User'
  const secondaryLine = user?.display_name ? user?.email || '' : ''
  const avatarInitial = (user?.display_name || user?.email || 'U').charAt(0).toUpperCase()

  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const feedbackBtnRef = useRef<HTMLButtonElement>(null)

  // Daily token usage badge: fetch on mount and after each completed turn
  // (notifyUsageChanged). Gated on isAuthenticated so it never fires during
  // logout; null (no token / 401) hides the badge.
  useEffect(() => {
    let active = true
    const load = async () => {
      if (!isAuthenticated()) {
        if (active) setUsage(null)
        return
      }
      const data = await fetchUsageToday()
      if (active) setUsage(data)
    }
    load()
    const off = onUsageChanged(load)
    return () => {
      active = false
      off()
    }
  }, [])

  // The waiting count behind the admin entry's badge. Gated on the SAME condition
  // as the entry itself (`user?.isAdmin`) — deliberately, not incidentally: the route is
  // superadmin-only server-side, so asking for anyone else would spend a request to earn
  // a 403 in every citizen's console. A failure leaves the count null (no badge): a badge
  // that guessed would be worse than no badge on the one surface whose job is to be
  // trusted. One fetch per mount, no polling — the panel refreshes it on every action,
  // and a nav badge that lags by a page navigation is not the failure this design guards
  // against.
  const isAdmin = user?.isAdmin === true
  useEffect(() => {
    if (!isAdmin || !isAuthenticated()) return undefined
    let active = true
    const read = () => {
      void fetchAppStatusCounts()
        .then((counts) => { if (active) setWaiting(counts.pending) })
        .catch(() => { if (active) setWaiting(null) })
    }
    read()
    // RE-READ WHEN THE TAB COMES BACK. The queue changes underneath this badge — an
    // administrator approves on the admin screen, a citizen withdraws, a pipeline routes
    // a drifted version — and a fetch-once badge would sit on a number the registry
    // panel two inches away has already corrected, which is exactly the disagreement
    // this component's own contract forbids. Refresh on re-entry rather than polling:
    // the count route is cheap but it is not free, and nothing here is urgent enough to
    // wake an idle tab for.
    const refresh = () => { if (document.visibilityState === 'visible') read() }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      active = false
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [isAdmin])

  // THE FEEDBACK MODAL'S ONLY ESCAPE, and now the whole of what this handler does.
  //
  // It used to close two things on one line — the avatar menu AND the feedback modal. Radix's
  // `DismissableLayer` owns menu-Escape now, so the menu half is gone; the feedback half must
  // NOT go with it. `FeedbackModal` is hand-rolled (`fixed inset-0`, `role="dialog"`) and its
  // only key handler is a Tab focus trap, so this line is the single reason Escape dismisses
  // it at all. Guarded by `components/__tests__/FeedbackModal.escape.test.jsx` — deleting this
  // effect leaves every menu test green while a shipped behaviour disappears.
  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setFeedbackOpen(false) }
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [])

  const showToast = (msg: string) => {
    setToastMsg(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToastMsg(null), 3000)
  }

  /**
   * Signs out through the workspace's UNSAVED-WORK GUARD — it did not, once, and a citizen
   * could lose work by pressing the single most final button on the screen, in silence. Same
   * guard as every nav link, so the dialog/save-offer/failed-save refusal are already familiar.
   * Outside a workspace, `exit` is a passthrough, so every other page's sign-out is untouched.
   */
  const signOut = () => exit(() => void handleLogout())

  const handleLogout = async () => {
    // Await the server-side revoke (bumps token_version + revokes refresh
    // families + clears cookies). logout() never throws. Never trap the
    // user's intent to sign out — the very next statement always leaves.
    const ok = await logout()
    // Attachment BYTES now live server-side, scoped per user — nothing local to
    // wipe on logout. Release any in-memory attachment object URLs so the next
    // user's tab doesn't inherit cached blob handles (memory hygiene only).
    revokeAllAttachmentUrls()
    // A failed revoke means this browser's session MAY still be live — a fact worth
    // telling the user — but this component cannot be the one to show it. `navigate` below
    // unmounts this page in the same tick, which unmounts the navbar, which owns
    // `toastMsg` — a local toast set here would be destroyed before a single frame renders
    // it (nobody has ever seen it). A message that must outlive its own page cannot be
    // owned by that page: hand it forward as router state instead, so the screen it
    // actually reaches — LoginPage — is the one that renders it, *after* the redirect.
    navigate('/login', ok ? undefined : { state: { signoutWarning: 'Sign-out may be incomplete on this device.' } })
  }

  return (
    <>
      <nav className="bg-white border-b border-bial-border sticky top-0 z-40 flex-shrink-0">
        <div className="px-6 h-14 flex items-center justify-between gap-4">
          {/* Brand + Nav */}
          <div className="flex items-center gap-8">
            {/* THE WORKSPACE'S IN-PLACE EXITS ROUTE THROUGH ITS GUARD.
                `beforeunload` cannot cover these: a single-page navigation is not an unload, so
                leaving the workspace by a nav link used to discard unsaved work in silence. Outside
                a workspace `useWorkspaceExit` hands back a function that simply goes, which is why
                every other page's navigation is untouched by this.

                THE DESTINATION IS `/projects`, not `/dashboard`: that address is a redirect now,
                and the most-clicked element in the product should not pay an extra hop through it.
                The guard is unchanged — only where it lets you go.

                THE DESTINATION CARRIES THE LIST BACK. Read fresh at click time rather than memoised
                at render, so a search typed a moment ago on `/projects` is what this lands on — not
                a page-one reset the citizen never asked for. */}
            <NavLink
              to="/projects"
              onClick={(e) => {
                e.preventDefault()
                exit(() => navigate(projectsListHref()))
              }}
              className="flex items-center whitespace-nowrap"
            >
              <BIALLogo />
            </NavLink>
            <div className="hidden md:flex items-center gap-6">
              {[...NAV_LINKS, ...(isAdmin ? [ADMIN_LINK] : [])].map(({ label, to }) => (
                <NavLink
                  key={to}
                  to={to}
                  onClick={(e) => {
                    e.preventDefault()
                    exit(() => navigate(to))
                  }}
                  className={({ isActive }) =>
                    // THE BOARD DISTINGUISHES THE ACTIVE ITEM BY WEIGHT AND INK, NOTHING ELSE.
                    // No underline, no teal, no size change: `color:#1A2B34;font-weight:600`
                    // against siblings at `color:#6B7280;font-weight:500`. The teal rule this
                    // replaced put brand colour on a nav item on every screen the client sees.
                    `text-sm transition inline-flex items-center gap-1.5 ${
                      isActive ? 'text-primary-900 font-semibold' : 'text-neutral font-medium hover:text-primary-900'
                    }`
                  }
                >
                  {label}
                  {/* The queue nobody can miss. Only on the admin entry, only for a
                      superadmin, and only when there is actually something waiting. */}
                  {to === ADMIN_LINK.to && <WaitingCountBadge count={waiting} where="nav" />}
                </NavLink>
              ))}
            </div>
          </div>

          {/* Right cluster */}
          <div className="flex items-center gap-1">
            {/* Daily token usage — the board's outlined pill bound to the live
                /api/usage/today source. Two-state colour: amber while there is budget,
                danger once it is spent. */}
            {usage && (() => {
              const pct = usage.limit ? Math.min(100, (usage.used / usage.limit) * 100) : 0
              const exhausted = usage.remaining <= 0
              // NO "NEARING" THRESHOLD. It used to turn the bar amber only past 80%, which the
              // board contradicts directly: its worked example draws 537,102 / 1,000,000 — 54% —
              // with an amber fill. Amber IS the meter, and danger is kept for a budget that is
              // actually spent. This is one of exactly two places the canvas uses `accent`.
              const barColor = exhausted ? 'bg-danger' : 'bg-accent'
              return (
                // NEVER `hidden md:flex`. An earlier version removed the in-rail meter on the
                // grounds that "the header already shows real usage" — but the header hid it
                // below 768px, so on a narrow screen there was no usage feedback anywhere at
                // all. It shrinks on small screens instead of vanishing: the count drops to a
                // compact used-of-limit and the bar narrows, so a citizen on a phone can still
                // see their budget running out.
                // THE BOARD'S PILL: a 1px hairline outline with NO fill, so it sits on the
                // white header rather than on a grey chip of its own, and a 3px track in the
                // hairline colour so the UNSPENT part of the budget is visible. It was a
                // #F8F9FA chip with a 6px white track, which made the remainder invisible
                // against the header behind it.
                <div
                  className="flex flex-col justify-center gap-1 border border-bial-border rounded-full px-2 md:px-3.5 py-1.5 mr-1 select-none"
                  title="Daily AI tokens used today · resets at midnight IST"
                  data-testid="usage-meter"
                >
                  <span className={`text-[10px] md:text-[11px] font-semibold leading-none whitespace-nowrap ${exhausted ? 'text-danger' : 'text-primary-900'}`}>
                    <span className="md:hidden">
                      {compactTokens(usage.used)} / {compactTokens(usage.limit)}
                    </span>
                    <span className="hidden md:inline">
                      {usage.used.toLocaleString('en-US')} / {usage.limit.toLocaleString('en-US')} tokens today
                    </span>
                  </span>
                  <div className="h-[3px] w-14 md:w-28 rounded-full bg-bial-border overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${barColor}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              )
            })()}

            {/* Feedback — always visible (every authed user); icon-only on mobile.
                Closes the user menu on the way: the button sits OUTSIDE the menu, so
                clicking it while the menu is open left the menu rendered behind the modal
                (pre-existing, and the dropdown union had it too). */}
            <button
              ref={feedbackBtnRef}
              onClick={() => { setUserMenuOpen(false); setFeedbackOpen(true) }}
              title="Send feedback"
              className="flex items-center gap-1.5 px-2.5 py-2 text-neutral hover:text-primary transition rounded-lg hover:bg-surface-muted text-sm font-medium"
            >
              <MessageSquare size={17} />
              <span className="hidden md:inline">Feedback</span>
            </button>

            {/* User avatar.

                RADIX OWNS THE MENU'S STATE MACHINE NOW, not this component. What the swap buys
                is `role="menu"` / `role="menuitem"` semantics and roving arrow-key focus, which
                the hand-rolled version had no way to get. What it costs is three things that
                have to be got right together, or the menu breaks in ways no test names:

                `modal={false}` — Radix menus are modal by DEFAULT, and modal means an
                outside-pointer guard plus `aria-hidden` on everything outside the menu. The
                Feedback button sits OUTSIDE this menu (a few lines up), so a modal menu would
                make the first press on it dismiss-only and cost a second click, and would hide
                it from a screen reader while the menu is open.

                THE FEEDBACK BUTTON KEEPS ITS OWN `setUserMenuOpen(false)`. Radix dismisses on
                pointer-down, which a real click carries — but the close is the button's stated
                job, not a side effect of a library default, and removing it is not a benefit of
                this swap.

                `useClickOutside(navRef, …)` IS GONE, deliberately and necessarily. The content
                is portalled to `document.body`, so it is no longer inside `<nav>`: that handler
                would have fired on the mousedown of a click on `Sign out` and unmounted the item
                before its own click landed. `DismissableLayer` is the outside-press dismissal
                now — for the whole document, not just outside the nav. */}
            <DropdownMenu modal={false} open={userMenuOpen} onOpenChange={setUserMenuOpen}>
              <DropdownMenuTrigger asChild>
                <button className="flex items-center gap-2 p-1.5 rounded-lg hover:bg-surface-muted transition">
                  <div className="w-8 h-8 rounded-full bg-primary flex items-center justify-center text-white text-xs font-bold">
                    {avatarInitial}
                  </div>
                  <div className="hidden lg:block text-left">
                    <p className="text-xs font-semibold text-tertiary leading-tight">{displayName}</p>
                    <p className="text-[10px] text-neutral leading-tight">{secondaryLine}</p>
                  </div>
                  <ChevronDown size={13} className="text-neutral hidden lg:block" />
                </button>
              </DropdownMenuTrigger>

              {/* `p-0 py-2` undoes the primitive's `p-1`: this menu's rows are full-bleed and
                  carry their own `px-4`, so a gutter would leave the header's divider short of
                  both edges. */}
              <DropdownMenuContent
                align="end"
                className="w-52 rounded-xl border-bial-border bg-white p-0 py-2 shadow-xl"
              >
                {/* ONE HAIRLINE, ON THE HEADER, AND STILL ONLY ONE now that two items sit below
                    it. The `border-b` here is the menu's whole divider; neither item carries a
                    `border-t`, which would draw a second rule a few pixels under the first, and
                    Integrations and Sign out are one group rather than two — the board draws them
                    with no rule between them. */}
                <DropdownMenuLabel
                  data-testid="user-menu-identity"
                  className="px-4 py-2.5 border-b border-bial-border font-normal"
                >
                  <p className="text-xs font-bold text-tertiary">{displayName}</p>
                  <p className="text-[10px] text-neutral">{secondaryLine}</p>
                </DropdownMenuLabel>
                {/* Between the header and Sign out, exactly where `OpenIt` draws it. The `mt-1`
                    moved here from Sign out with the group's first row. */}
                <DropdownMenuItem
                  onSelect={() => setIntegrationsOpen(true)}
                  className="mt-1 gap-2.5 rounded-none px-4 py-2.5 text-sm text-tertiary hover:bg-surface-muted focus:bg-surface-muted"
                >
                  <Plug size={13} />
                  Integrations
                </DropdownMenuItem>
                <DropdownMenuItem
                  onSelect={signOut}
                  className="gap-2.5 rounded-none px-4 py-2.5 text-sm text-danger hover:bg-red-50 focus:bg-red-50 focus:text-danger"
                >
                  <LogOut size={13} />
                  Sign out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </nav>

      {/* Feedback modal — reachable from every authed page's header */}
      <FeedbackModal
        open={feedbackOpen}
        onClose={() => setFeedbackOpen(false)}
        onSubmitted={() => { setFeedbackOpen(false); showToast('Thanks — your feedback was sent.') }}
        triggerRef={feedbackBtnRef}
      />

      {/* Integrations — the same dialog `Manage integrations →` in the workspace rail opens
          (U10), over whatever screen the citizen is standing on. */}
      {integrationsOpen && <IntegrationsDialog onClose={() => setIntegrationsOpen(false)} />}

      {/* Toast */}
      {toastMsg && (
        <div className="fixed bottom-6 right-6 z-50 bg-white border border-bial-border rounded-xl shadow-xl px-4 py-3 text-sm text-tertiary font-medium flex items-center gap-2">
          <Info size={14} className="text-primary flex-shrink-0" />
          {toastMsg}
        </div>
      )}
    </>
  )
}
