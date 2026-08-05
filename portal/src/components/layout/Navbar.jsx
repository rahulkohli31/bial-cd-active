import { useState, useRef, useEffect } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import {
  Bell, Settings, Search, ChevronDown, LogOut, User,
  Inbox, Boxes,
  UserCircle, BookOpen, Info, Monitor, MessageSquare,
} from 'lucide-react'
import { getStoredUser, isAuthenticated, logout } from '../../utils/auth'
import { fetchUsageToday, onUsageChanged } from '../../utils/usage'
import { revokeAllAttachmentUrls } from '../../utils/attachmentApi'
import FeedbackModal from '../FeedbackModal'
import BIALLogo from '../BIALLogo'

const NAV_LINKS = [
  { label: 'Projects', to: '/projects' },
  { label: 'Help', to: '/help' },
]

const ADMIN_LINK = { label: 'Admin', to: '/admin' }

const SETTINGS_ITEMS = [
  { icon: UserCircle, label: 'Profile Settings' },
  { icon: Bell, label: 'Notification Preferences' },
  { icon: Monitor, label: 'Display & Accessibility' },
  { icon: Info, label: 'About BIAL Citizen Developer' },
]

const SEARCH_PAGES = [
  { label: 'Projects', to: '/projects', icon: Boxes },
  { label: 'Help Center', to: '/help', icon: BookOpen },
]

const SEARCH_ACTIONS = [
  { label: 'View Projects', to: '/projects', icon: Inbox },
]

function useClickOutside(ref, handler) {
  useEffect(() => {
    const listener = (e) => { if (ref.current && !ref.current.contains(e.target)) handler() }
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
const compactTokens = (n) => _compactTokenFormat.format(n)

export default function Navbar() {
  const navigate = useNavigate()
  const [activeDropdown, setActiveDropdown] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [toastMsg, setToastMsg] = useState(null)
  const [usage, setUsage] = useState(null)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  // The cookie-session /auth/me profile is { id, email, display_name } — no
  // name/username/role/isAdmin (RBAC deferred this phase). Derive the display bits
  // from what's actually present.
  const user = getStoredUser() || {}
  const displayName = user.display_name || user.email || 'User'
  const secondaryLine = user.display_name ? user.email || '' : ''
  const avatarInitial = (user.display_name || user.email || 'U').charAt(0).toUpperCase()

  const navRef = useRef(null)
  const toastTimer = useRef(null)
  const feedbackBtnRef = useRef(null)

  useClickOutside(navRef, () => setActiveDropdown(null))

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

  useEffect(() => {
    const onEsc = (e) => { if (e.key === 'Escape') { setActiveDropdown(null); setSearchQuery(''); setFeedbackOpen(false) } }
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [])

  const toggle = (name) => setActiveDropdown((prev) => (prev === name ? null : name))

  const showToast = (msg) => {
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

  const handleNav = (to) => {
    setActiveDropdown(null)
    setSearchQuery('')
    navigate(to)
  }

  const filteredSearch = searchQuery.trim()
    ? {
        pages: SEARCH_PAGES.filter((p) => p.label.toLowerCase().includes(searchQuery.toLowerCase())),
        actions: SEARCH_ACTIONS.filter((a) => a.label.toLowerCase().includes(searchQuery.toLowerCase())),
      }
    : null

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
              {[...NAV_LINKS, ...(user.isAdmin ? [ADMIN_LINK] : [])].map(({ label, to }) => (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    `text-sm font-medium transition pb-0.5 ${
                      isActive ? 'text-primary font-bold border-b-2 border-primary' : 'text-neutral hover:text-primary'
                    }`
                  }
                >
                  {label}
                </NavLink>
              ))}
            </div>
          </div>

          {/* Right cluster */}
          <div className="flex items-center gap-1">
            {/* Search */}
            <div className="relative hidden lg:block">
              <div
                className="flex items-center gap-2 bg-surface-muted border border-bial-border rounded-lg px-3 py-1.5 cursor-text"
                onClick={() => { setActiveDropdown('search'); }}
              >
                <Search size={13} className="text-neutral flex-shrink-0" />
                <input
                  type="text"
                  placeholder="Search pages or actions..."
                  value={searchQuery}
                  onChange={(e) => { setSearchQuery(e.target.value); setActiveDropdown('search') }}
                  className="bg-transparent text-sm text-tertiary placeholder:text-gray-400 focus:outline-none w-48"
                  onFocus={() => setActiveDropdown('search')}
                />
              </div>

              {activeDropdown === 'search' && (
                <div className="absolute top-full right-0 mt-1.5 w-72 bg-white rounded-xl border border-bial-border shadow-xl z-50 py-2 overflow-hidden">
                  {filteredSearch ? (
                    <>
                      {filteredSearch.pages.length > 0 && (
                        <div>
                          <p className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-neutral">Pages</p>
                          {filteredSearch.pages.map((p) => (
                            <button key={p.to} onClick={() => handleNav(p.to)} className="w-full flex items-center gap-2 px-3 py-2 hover:bg-bial-bg transition text-left">
                              <p.icon size={13} className="text-primary flex-shrink-0" />
                              <span className="text-sm text-tertiary">{p.label}</span>
                            </button>
                          ))}
                        </div>
                      )}
                      {filteredSearch.actions.length > 0 && (
                        <div>
                          <p className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-neutral border-t border-bial-border mt-1">Actions</p>
                          {filteredSearch.actions.map((a) => (
                            <button key={a.label} onClick={() => handleNav(a.to)} className="w-full flex items-center gap-2 px-3 py-2 hover:bg-bial-bg transition text-left">
                              <a.icon size={13} className="text-primary flex-shrink-0" />
                              <span className="text-sm text-tertiary">{a.label}</span>
                            </button>
                          ))}
                        </div>
                      )}
                      {!filteredSearch.pages.length && !filteredSearch.actions.length && (
                        <p className="px-4 py-3 text-sm text-neutral text-center">No results for "{searchQuery}"</p>
                      )}
                    </>
                  ) : (
                    <>
                      <p className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-neutral">Pages</p>
                      {SEARCH_PAGES.map((p) => (
                        <button key={p.to} onClick={() => handleNav(p.to)} className="w-full flex items-center gap-2 px-3 py-2 hover:bg-bial-bg transition text-left">
                          <p.icon size={13} className="text-primary flex-shrink-0" />
                          <span className="text-sm text-tertiary">{p.label}</span>
                        </button>
                      ))}
                      <p className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-neutral border-t border-bial-border mt-1">Quick Actions</p>
                      {SEARCH_ACTIONS.map((a) => (
                        <button key={a.label} onClick={() => handleNav(a.to)} className="w-full flex items-center gap-2 px-3 py-2 hover:bg-bial-bg transition text-left">
                          <a.icon size={13} className="text-primary flex-shrink-0" />
                          <span className="text-sm text-tertiary">{a.label}</span>
                        </button>
                      ))}
                    </>
                  )}
                </div>
              )}
            </div>

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

            {/* Bell */}
            <div className="relative">
              <button
                onClick={() => toggle('bell')}
                className="p-2 text-neutral hover:text-primary transition rounded-lg hover:bg-surface-muted relative"
              >
                <Bell size={17} />
              </button>
              {activeDropdown === 'bell' && (
                <div className="absolute right-0 top-11 w-80 bg-white rounded-xl border border-bial-border shadow-xl z-50 overflow-hidden">
                  <div className="px-4 py-3 border-b border-bial-border">
                    <p className="text-sm font-bold text-tertiary">Notifications</p>
                  </div>
                  <div className="flex flex-col items-center justify-center px-4 py-8 text-center">
                    <Bell size={22} className="text-neutral/40 mb-2" />
                    <p className="text-sm font-medium text-tertiary">You're all caught up</p>
                    <p className="text-[11px] text-neutral mt-0.5">No new notifications right now.</p>
                  </div>
                </div>
              )}
            </div>

            {/* Settings */}
            <div className="relative">
              <button
                onClick={() => toggle('settings')}
                className="p-2 text-neutral hover:text-primary transition rounded-lg hover:bg-surface-muted"
              >
                <Settings size={17} />
              </button>
              {activeDropdown === 'settings' && (
                <div className="absolute right-0 top-11 w-52 bg-white rounded-xl border border-bial-border shadow-xl z-50 py-2 overflow-hidden">
                  {SETTINGS_ITEMS.map(({ icon: Icon, label }) => (
                    <button
                      key={label}
                      onClick={() => { setActiveDropdown(null); showToast('Coming soon') }}
                      className="w-full flex items-center gap-2.5 px-4 py-2.5 text-sm text-tertiary hover:bg-bial-bg transition text-left"
                    >
                      <Icon size={14} className="text-neutral flex-shrink-0" />
                      {label}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* User avatar */}
            <div className="relative">
              <button
                onClick={() => toggle('user')}
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

              {activeDropdown === 'user' && (
                <div className="absolute right-0 top-11 w-52 bg-white rounded-xl border border-bial-border shadow-xl py-2 z-50">
                  <div className="px-4 py-2.5 border-b border-bial-border">
                    <p className="text-xs font-bold text-tertiary">{displayName}</p>
                    <p className="text-[10px] text-neutral">{secondaryLine}</p>
                  </div>
                  <button
                    onClick={() => { setActiveDropdown(null); showToast('Coming soon') }}
                    className="w-full flex items-center gap-2.5 px-4 py-2.5 text-sm text-tertiary hover:bg-bial-bg transition"
                  >
                    <User size={13} className="text-neutral flex-shrink-0" />
                    My Profile
                  </button>
                  <div className="border-t border-bial-border mt-1 pt-1">
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
