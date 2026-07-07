import { useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import Navbar from '../components/layout/Navbar'
import UsersLimitsPanel from '../components/admin/UsersLimitsPanel'
import FeedbackPanel from '../components/admin/FeedbackPanel'
import AppRegistryPanel from '../components/admin/AppRegistryPanel'
import { Info, Lock } from 'lucide-react'
import { DEPLOY_ENABLED } from '../config/features'
import { getStoredUser } from '../utils/auth'

const TABS = [
  ...(DEPLOY_ENABLED ? [{ id: 'apps', label: 'App Registry' }] : []),
  { id: 'users', label: 'Users & Limits' },
  { id: 'feedback', label: 'Feedback' },
]

/**
 * Admin Console — App Registry (approve/reject/disable/clear-data/audit, backed
 * by the real /api/admin/apps endpoints), per-user usage limits, and feedback.
 * The old mock app vocabulary (active/under_review/flagged/archived) and its
 * empty local state are gone; each tab is a self-contained, API-backed panel.
 */
export default function AdminPage() {
  const navigate = useNavigate()
  // Read the SAME cookie-session profile the Navbar uses (getStoredUser → cached /auth/me,
  // which now carries `isAdmin`). The old `localStorage['bial_user']` is a purged legacy key
  // (LEGACY_KEYS), so it was always `{}` here → the superadmin got Access Denied.
  const user = getStoredUser() || {}

  const [activeTab, setActiveTab] = useState(TABS[0].id)
  const [toast, setToast] = useState(null)
  const toastTimer = useRef(null)

  const showToast = (msg) => {
    setToast(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3000)
  }

  if (!user.isAdmin) {
    return (
      <div className="min-h-screen bg-bial-bg flex flex-col font-manrope">
        <Navbar />
        <div className="flex-1 flex items-center justify-center px-6">
          <div className="text-center max-w-sm">
            <div className="w-14 h-14 rounded-2xl bg-red-100 flex items-center justify-center mx-auto mb-4">
              <Lock size={22} className="text-red-500" />
            </div>
            <h2 className="text-lg font-bold text-tertiary mb-2">Access Denied</h2>
            <p className="text-sm text-neutral leading-relaxed">
              You don't have permission to access the Admin Console. Contact IT if you believe this is an error.
            </p>
            <button
              onClick={() => navigate('/dashboard')}
              className="mt-6 px-5 py-2.5 rounded-xl bg-primary text-white text-sm font-semibold hover:bg-primary/90 transition"
            >
              Back to Dashboard
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-bial-bg flex flex-col font-manrope">
      <Navbar />

      <div className="flex-1 px-6 py-8 max-w-7xl mx-auto w-full">
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-tertiary">Admin Console</h1>
          <p className="text-sm text-neutral mt-1">
            Review and govern citizen-developed apps, manage usage limits, and read feedback.
          </p>
        </div>

        <div className="bg-white rounded-2xl border border-bial-border shadow-sm overflow-hidden mb-6">
          <div className="flex border-b border-bial-border px-4">
            {TABS.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-3.5 text-sm font-medium border-b-2 transition -mb-px whitespace-nowrap ${
                  activeTab === tab.id ? 'text-primary border-primary' : 'text-neutral border-transparent hover:text-tertiary'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div className="p-4">
            {DEPLOY_ENABLED && activeTab === 'apps' && <AppRegistryPanel onToast={showToast} />}
            {activeTab === 'users' && <UsersLimitsPanel onToast={showToast} />}
            {activeTab === 'feedback' && <FeedbackPanel />}
          </div>
        </div>
      </div>

      {toast && (
        <div className="fixed bottom-6 right-6 z-50 bg-white border border-bial-border rounded-xl shadow-xl px-4 py-3 text-sm text-tertiary font-medium flex items-center gap-2">
          <Info size={14} className="text-primary flex-shrink-0" />
          {toast}
        </div>
      )}
    </div>
  )
}
