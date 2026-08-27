import { useState, useRef, useEffect } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
// `Info` is NOT left over from the removed settings menu — it is the toast's own icon
// (see the toast render below). The nine icons that went with #157's dead header controls
// are gone; these four all have live consumers.
import { ChevronDown, LogOut, Info, MessageSquare } from 'lucide-react'
import type { RefObject } from 'react'
import { getStoredUser, isAuthenticated, logout } from '../../utils/auth'
import { fetchUsageToday, onUsageChanged } from '../../utils/usage'
import type { UsageToday } from '../../utils/usage'
import { revokeAllAttachmentUrls } from '../../utils/attachmentApi'
import { fetchAppStatusCounts } from '../../utils/appRegistryApi'
import WaitingCountBadge from '../admin/WaitingCountBadge'
import FeedbackModal from '../FeedbackModal'
import BIALLogo from '../BIALLogo'

const NAV_LINKS = [
  { label: 'Projects', to: '/projects' },
  { label: 'Help', to: '/help' },
]

const ADMIN_LINK = { label: 'Admin', to: '/admin' }

function useClickOutside(ref: RefObject<HTMLElement | null>, handler: () => void) {
  useEffect(() => {
    const listener = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) handler() }
    document.addEventListener('mousedown', listener)
    return () => document.removeEventListener('mousedown', listener)
  }, [ref, handler])
}

/**
 * Tokens at a glance for the narrow-screen meter: "48K", "1.2M". The full
 * `12,345 / 50,000 tokens` reading stays on md and up — this is the same fact, short enough
 * to survive a phone-width navbar rather than being hidden there (N4).
 */
const _compactTokenFormat = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
})
const compactTokens = (n: number): string => _compactTokenFormat.format(n)


export default function Navbar() {
  const navigate = useNavigate()
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [toastMsg, setToastMsg] = useState<string | null>(null)
  const [usage, setUsage] = useState<UsageToday | null>(null)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  // How many apps are waiting for an administrator (P1). `null` = we have not asked, or
  // the ask failed — never rendered as a number, and never asked for at all unless this
  // user is a superadmin (see the effect below).
  const [waiting, setWaiting] = useState<number | null>(null)
  // The cookie-session /auth/me profile is { id, email, display_name } — no
  // name/username/role/isAdmin (RBAC deferred this phase). Derive the display bits
  // from what's actually present.
  const user = getStoredUser()
  const displayName = user?.display_name || user?.email || 'User'
  const secondaryLine = user?.display_name ? user?.email || '' : ''
  const avatarInitial = (user?.display_name || user?.email || 'U').charAt(0).toUpperCase()

  const navRef = useRef<HTMLElement>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const feedbackBtnRef = useRef<HTMLButtonElement>(null)

  useClickOutside(navRef, () => setUserMenuOpen(false))

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

  // The waiting count behind the admin entry's badge (P1). Gated on the SAME condition
  // as the entry itself (`user?.isAdmin`) — deliberately, not incidentally: the route is
  // superadmin-only server-side, so asking for anyone else would spend a request to earn
  // a 403 in every citizen's console. A failure leaves the count null (no badge): a badge
  // that guessed would be worse than no badge on the one surface whose job is to be
  // trusted. One fetch per mount, no polling — the panel refreshes it on every action,
  // and a nav badge that lags by a page navigation is not the failure P1 is about.
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

  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') { setUserMenuOpen(false); setFeedbackOpen(false) } }
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [])


  const showToast = (msg: string) => {
    setToastMsg(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToastMsg(null), 3000)
  }

  const handleLogout = async () => {
    // Await the server-side revoke (bumps token_version + revokes refresh
    // families + clears cookies). logout() records the LOGGED_OUT banner and
    // never throws — on failure we still leave, surfacing a toast so the user
    // knows this device's session may linger until it expires. Never trap the
    // user's intent to sign out.
    const ok = await logout()
    // Attachment BYTES now live server-side, scoped per user — nothing local to
    // wipe on logout. Release any in-memory attachment object URLs so the next
    // user's tab doesn't inherit cached blob handles (memory hygiene only).
    revokeAllAttachmentUrls()
    if (!ok) showToast('Sign-out may be incomplete on this device.')
    navigate('/login')
  }



  return (
    <>
      <nav ref={navRef} className="bg-white border-b border-bial-border sticky top-0 z-40 flex-shrink-0">
        <div className="px-6 h-14 flex items-center justify-between gap-4">
          {/* Brand + Nav */}
          <div className="flex items-center gap-8">
            <NavLink to="/dashboard" className="flex items-center whitespace-nowrap">
              <BIALLogo />
            </NavLink>
            <div className="hidden md:flex items-center gap-6">
              {[...NAV_LINKS, ...(isAdmin ? [ADMIN_LINK] : [])].map(({ label, to }) => (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    `text-sm font-medium transition pb-0.5 inline-flex items-center gap-1.5 ${
                      isActive ? 'text-primary font-bold border-b-2 border-primary' : 'text-neutral hover:text-primary'
                    }`
                  }
                >
                  {label}
                  {/* The queue nobody can miss (P1). Only on the admin entry, only for a
                      superadmin, and only when there is actually something waiting. */}
                  {to === ADMIN_LINK.to && <WaitingCountBadge count={waiting} where="nav" />}
                </NavLink>
              ))}
            </div>
          </div>

          {/* Right cluster */}
          <div className="flex items-center gap-1">
            {/* Daily token usage — prominent status chip bound to the live
                /api/usage/today source. Three-state colour: healthy (primary) →
                nearing the limit (accent/amber) → exhausted (danger). */}
            {usage && (() => {
              const pct = usage.limit ? Math.min(100, (usage.used / usage.limit) * 100) : 0
              const exhausted = usage.remaining <= 0
              const nearing = !exhausted && pct >= 80
              const barColor = exhausted ? 'bg-danger' : nearing ? 'bg-accent' : 'bg-primary'
              return (
                // N4: NEVER `hidden md:flex`. F7 removed the in-rail meter on the grounds that
                // "the header already shows real usage" — but the header hid it below 768px, so
                // on a narrow screen there was no usage feedback anywhere at all. It shrinks on
                // small screens instead of vanishing: the count drops to a compact
                // used-of-limit and the bar narrows, so a citizen on a phone can still see
                // their budget running out.
                <div
                  className="flex flex-col justify-center gap-1 bg-surface-muted border border-bial-border rounded-full px-2 md:px-3 py-1.5 mr-1 select-none"
                  title="Daily AI tokens used today · resets at midnight IST"
                  data-testid="usage-meter"
                >
                  <span className={`text-[10px] md:text-xs font-semibold leading-none whitespace-nowrap ${exhausted ? 'text-danger' : 'text-tertiary'}`}>
                    <span className="md:hidden">
                      {compactTokens(usage.used)} / {compactTokens(usage.limit)}
                    </span>
                    <span className="hidden md:inline">
                      {usage.used.toLocaleString('en-US')} / {usage.limit.toLocaleString('en-US')} tokens
                    </span>
                  </span>
                  <div className="h-1.5 w-14 md:w-28 rounded-full bg-white overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${barColor}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              )
            })()}

            {/* Feedback — always visible (every authed user); icon-only on mobile */}
            <button
              ref={feedbackBtnRef}
              onClick={() => setFeedbackOpen(true)}
              title="Send feedback"
              className="flex items-center gap-1.5 px-2.5 py-2 text-neutral hover:text-primary transition rounded-lg hover:bg-surface-muted text-sm font-medium"
            >
              <MessageSquare size={17} />
              <span className="hidden md:inline">Feedback</span>
            </button>

            {/* User avatar */}
            <div className="relative">
              <button
                onClick={() => setUserMenuOpen((open) => !open)}
                className="flex items-center gap-2 p-1.5 rounded-lg hover:bg-surface-muted transition"
              >
                <div className="w-8 h-8 rounded-full bg-primary flex items-center justify-center text-white text-xs font-bold">
                  {avatarInitial}
                </div>
                <div className="hidden lg:block text-left">
                  <p className="text-xs font-semibold text-tertiary leading-tight">{displayName}</p>
                  <p className="text-[10px] text-neutral leading-tight">{secondaryLine}</p>
                </div>
                <ChevronDown size={13} className="text-neutral hidden lg:block" />
              </button>

              {userMenuOpen && (
                <div className="absolute right-0 top-11 w-52 bg-white rounded-xl border border-bial-border shadow-xl py-2 z-50">
                  <div className="px-4 py-2.5 border-b border-bial-border">
                    <p className="text-xs font-bold text-tertiary">{displayName}</p>
                    <p className="text-[10px] text-neutral">{secondaryLine}</p>
                  </div>
                  {/* No border of its own: the name/email header above already carries the one
                      divider this menu needs. It sat under "My Profile" until that placeholder was
                      removed (#157 A4); keeping `border-t` would now render a second hairline a few
                      pixels below the first. */}
                  <div className="mt-1">
                    <button
                      onClick={handleLogout}
                      className="w-full flex items-center gap-2.5 px-4 py-2.5 text-sm text-danger hover:bg-red-50 transition"
                    >
                      <LogOut size={13} />
                      Sign out
                    </button>
                  </div>
                </div>
              )}
            </div>
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
