import { useState, useRef, useEffect } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import {
  Bell, Settings, Search, ChevronDown, LogOut, User,
  FileText, Plus, Inbox, Bot,
  UserCircle, BookOpen, Info, Monitor, MessageSquare,
} from 'lucide-react'
import { getStoredUser, getAccessToken, clearSession, SIGNOUT_REASONS } from '../../utils/auth'
import { revokeAllAttachmentUrls } from '../../utils/attachmentApi'
import { CHAT_ENABLED } from '../../config/features'
import FeedbackModal from '../FeedbackModal'
import BIALLogo from '../BIALLogo'

const NAV_LINKS = [
  { label: 'App Builder', to: '/workspace' },
  ...(CHAT_ENABLED ? [{ label: 'BIAL Chat', to: '/chat' }] : []),
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
  { label: 'App Builder', to: '/workspace', icon: FileText },
  ...(CHAT_ENABLED ? [{ label: 'BIAL Chat', to: '/chat', icon: Bot }] : []),
  { label: 'Help Center', to: '/help', icon: BookOpen },
]

const SEARCH_ACTIONS = [
  { label: 'Create New App', to: '/workspace/sandbox', icon: Plus },
  { label: 'View Drafts', to: '/workspace', icon: Inbox },
]

function useClickOutside(ref, handler) {
  useEffect(() => {
    const listener = (e) => { if (ref.current && !ref.current.contains(e.target)) handler() }
    document.addEventListener('mousedown', listener)
    return () => document.removeEventListener('mousedown', listener)
  }, [ref, handler])
}

export default function Navbar() {
  const navigate = useNavigate()
  const [activeDropdown, setActiveDropdown] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [toastMsg, setToastMsg] = useState(null)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const user = getStoredUser() || {}

  const navRef = useRef(null)
  const toastTimer = useRef(null)
  const feedbackBtnRef = useRef(null)

  useClickOutside(navRef, () => setActiveDropdown(null))

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

  const handleLogout = () => {
    // Best-effort server-side revoke; keepalive lets it finish after we leave.
    // Never block the client on the network.
    const token = getAccessToken()
    if (token) {
      fetch('/api/auth/logout', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
        keepalive: true,
      }).catch(() => {})
    }
    // Attachment BYTES now live server-side, scoped per user — nothing local to
    // wipe on logout. Release any in-memory attachment object URLs so the next
    // user's tab doesn't inherit cached blob handles (memory hygiene only).
    revokeAllAttachmentUrls()
    clearSession(SIGNOUT_REASONS.LOGGED_OUT)
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
                  {(user.name || 'U').charAt(0).toUpperCase()}
                </div>
                <div className="hidden lg:block text-left">
                  <p className="text-xs font-semibold text-tertiary leading-tight">{user.name || user.username || 'User'}</p>
                  <p className="text-[10px] text-neutral leading-tight">{user.role || 'User'}</p>
                </div>
                <ChevronDown size={13} className="text-neutral hidden lg:block" />
              </button>

              {activeDropdown === 'user' && (
                <div className="absolute right-0 top-11 w-52 bg-white rounded-xl border border-bial-border shadow-xl py-2 z-50">
                  <div className="px-4 py-2.5 border-b border-bial-border">
                    <p className="text-xs font-bold text-tertiary">{user.name || user.username || 'User'}</p>
                    <p className="text-[10px] text-neutral">{user.role || 'User'}</p>
                  </div>
                  <button
                    onClick={() => { setActiveDropdown(null); showToast('Coming soon') }}
                    className="w-full flex items-center gap-2.5 px-4 py-2.5 text-sm text-tertiary hover:bg-bial-bg transition"
                  >
                    <User size={13} className="text-neutral flex-shrink-0" />
                    My Profile
                  </button>
                  <button
                    onClick={() => handleNav('/workspace')}
                    className="w-full flex items-center gap-2.5 px-4 py-2.5 text-sm text-tertiary hover:bg-bial-bg transition"
                  >
                    <FileText size={13} className="text-neutral flex-shrink-0" />
                    My Drafts
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
