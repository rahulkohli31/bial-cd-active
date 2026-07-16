import { useState } from 'react'
import { Clock3 } from 'lucide-react'
import BIALLogo from '../components/BIALLogo'
import { logout } from '../utils/auth'

/**
 * Rendered directly by RequireAuth (App.jsx) when the cached/bootstrapped
 * session resolves `status: 'pending'` — a real, authenticated session that
 * simply hasn't been approved yet. NOT a redirect (the user IS signed in),
 * and NOT a route of its own: the URL bar keeps whatever protected path the
 * user landed on.
 */
export default function AwaitingApprovalPage() {
  const [signingOut, setSigningOut] = useState(false)

  const handleSignOut = async () => {
    setSigningOut(true)
    // Mirrors Navbar's handleLogout: never throws, never traps the user's
    // intent to leave — a failed server-side revoke still bounces to /login,
    // just with cookies/refresh-family cleanup possibly incomplete on this device.
    await logout()
    window.location.href = '/login'
  }

  return (
    <div className="min-h-screen flex flex-col items-center justify-center font-manrope bg-bial-bg px-4">
      <div className="mb-8">
        <BIALLogo />
      </div>
      <div className="max-w-md w-full bg-white rounded-2xl shadow-xl border border-bial-border p-8 text-center">
        <div className="mx-auto w-14 h-14 rounded-full bg-secondary/10 flex items-center justify-center mb-5">
          <Clock3 size={24} className="text-secondary" />
        </div>
        <h1 className="text-lg font-bold text-tertiary mb-2">Awaiting approval</h1>
        <p className="text-sm text-neutral leading-relaxed">
          Your account has been created, but a super-admin still needs to approve it before you can
          use the Citizen Developer Portal. Check back soon, or contact your administrator.
        </p>
        <button
          onClick={handleSignOut}
          disabled={signingOut}
          data-testid="awaiting-approval-signout"
          className="mt-6 inline-flex items-center justify-center px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg disabled:opacity-50 transition"
        >
          {signingOut ? 'Signing out…' : 'Sign out'}
        </button>
      </div>
    </div>
  )
}
