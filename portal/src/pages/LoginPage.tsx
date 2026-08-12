import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Zap, Shield, Cloud } from 'lucide-react'
import BIALLogo from '../components/BIALLogo'
import { consumeSignoutReason, SIGNOUT_REASONS, LOGIN_URL, bootstrapSession } from '../utils/auth'

// The app's authenticated landing route — the primary RequireAuth-wrapped page in
// App.jsx (`/` and any unknown path both redirect to `/login`; `/dashboard` is the
// first real signed-in screen). A signed-in visitor is forwarded here.
const HOME_ROUTE = '/dashboard'

// Signout-reason banners (client-recorded on logout / expiry). Record<string,
// string> since consumeSignoutReason() reads back a plain string (whatever
// was written to localStorage), not the narrower SignoutReason type.
const SIGNOUT_BANNERS: Record<string, string> = {
  [SIGNOUT_REASONS.EXPIRED]: 'Your session expired. Please sign in again.',
  [SIGNOUT_REASONS.LOGGED_OUT]: 'You have been signed out.',
}

// Sign-in failure banners, keyed by the FastAPI callback's ?authError=<reason>.
// wrong_tenant and account_suspended each get distinct copy; everything else
// (invalid callback, cancelled consent) collapses to a generic retry message.
// account_suspended is NOT a sign-in failure the user can fix by retrying — an
// administrator paused their access — so the copy is non-alarming and points at
// the admin, never at the user. It reaches here two ways: the login-callback
// 302 (?authError=account_suspended) and the mid-session interceptor's hard
// redirect to the same URL (handleSuspendedSession).
// Record<string, string>: the real key is whatever string the ?authError query
// param carries — an unrecognized value is expected (falls through to the
// generic message below), not a type error.
const AUTH_ERROR_BANNERS: Record<string, string> = {
  wrong_tenant:
    'That account isn’t part of the BIAL organization. Please sign in with your BIAL account.',
  account_suspended:
    'Your access has been paused by an administrator. Please contact your BIAL administrator to restore it.',
}
const GENERIC_AUTH_ERROR = 'Sign-in failed. Please try again.'

export default function LoginPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [notice, setNotice] = useState('')

  // Forward an ALREADY-authenticated visitor into the app instead of dead-ending
  // on the sign-in screen. A successful Microsoft sign-in lands the browser on the
  // SPA origin root, which routes here; resolving the cookie session
  // (bootstrapSession → GET /auth/me) and redirecting on a CONFIRMED user is what
  // turns that round-trip into a real landing. Guarded on a truthy user ONLY
  // (never a null / still-bootstrapping / expired result), so /login ↔ /dashboard
  // cannot ping-pong: RequireAuth on the target reuses this same cached session.
  useEffect(() => {
    let cancelled = false
    bootstrapSession().then((user) => {
      if (!cancelled && user) navigate(HOME_ROUTE, { replace: true })
    })
    return () => {
      cancelled = true
    }
  }, [navigate])

  // One-time contextual banner: a sign-in failure (?authError) takes precedence,
  // else the recorded signout reason (session expired / logged out).
  //
  // Both lookups use Object.hasOwn rather than a bare index: authError is an
  // attacker-chosen string straight off the URL, and AUTH_ERROR_BANNERS/
  // SIGNOUT_BANNERS are plain object literals with Object.prototype in their
  // chain — `?authError=__proto__` or `?authError=constructor` resolves to a
  // real (truthy) prototype value, so the `|| GENERIC_AUTH_ERROR` fallback
  // never fires and `notice` gets set to an object/function. React throws
  // rendering either as a child, and with no error boundary anywhere in
  // portal/src, that white-screens the sign-in page for anyone who clicks a
  // crafted link — no auth required, since /login is the unauthenticated route.
  useEffect(() => {
    const authError = searchParams.get('authError')
    if (authError) {
      setNotice(
        Object.hasOwn(AUTH_ERROR_BANNERS, authError) ? AUTH_ERROR_BANNERS[authError] : GENERIC_AUTH_ERROR,
      )
      return
    }
    const reason = consumeSignoutReason()
    if (reason && Object.hasOwn(SIGNOUT_BANNERS, reason)) setNotice(SIGNOUT_BANNERS[reason])
  }, [searchParams])

  // Full-page navigation to the FastAPI control-plane, which runs the OIDC
  // Authorization-Code + PKCE flow and redirects back to the app (or here with
  // ?authError on failure). Not an in-SPA fetch — the browser must leave for
  // login.microsoftonline.com and return.
  const signIn = () => {
    window.location.href = LOGIN_URL
  }

  return (
    <div className="min-h-screen flex font-manrope">
      {/* Left panel */}
      <div
        className="hidden lg:flex lg:w-1/2 flex-col justify-between p-10 relative overflow-hidden"
        style={{ background: 'linear-gradient(155deg, #00818A 0%, #1A2B34 70%, #0a1a22 100%)' }}
      >
        <div className="absolute top-[-80px] right-[-80px] w-72 h-72 rounded-full bg-white opacity-5" />
        <div className="absolute bottom-[-100px] left-[-60px] w-80 h-80 rounded-full bg-white opacity-5" />
        <div
          className="absolute inset-0 opacity-10"
          style={{
            backgroundImage: `url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none'%3E%3Cg fill='%23ffffff' fill-opacity='0.4'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E")`,
          }}
        />

        <div className="relative z-10">
          <BIALLogo dark />
        </div>

        <div className="relative z-10 space-y-5">
          <span className="inline-block bg-secondary text-white text-xs font-worksans font-semibold tracking-widest uppercase px-3 py-1 rounded-full">
            Staff Internal Portal
          </span>
          <h1 className="text-5xl font-extrabold text-white leading-tight">
            Citizen<br />Developer
          </h1>
        </div>

        <div className="relative z-10 flex gap-6">
          {[
            { icon: Zap, label: 'High Efficiency' },
            { icon: Shield, label: 'Secure Access' },
            { icon: Cloud, label: 'Cloud Integrated' },
          ].map(({ icon: Icon, label }) => (
            <div key={label} className="flex items-center gap-2 text-teal-200 text-sm">
              <Icon size={14} strokeWidth={1.5} />
              <span>{label}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Right panel */}
      <div className="flex-1 flex flex-col items-center justify-center px-6 py-12 bg-white">
        <div className="lg:hidden mb-8">
          <BIALLogo />
        </div>

        <div className="w-full max-w-sm">
          <div className="flex justify-center mb-6">
            <div className="w-14 h-14 rounded-2xl bg-primary flex items-center justify-center shadow-lg shadow-primary/20">
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M5 17H3a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v3" />
                <rect x="9" y="11" width="14" height="10" rx="2" />
                <line x1="13" y1="16" x2="15" y2="16" />
              </svg>
            </div>
          </div>

          <h2 className="text-center text-2xl font-bold text-tertiary mb-1">Welcome Back</h2>
          <p className="text-center text-neutral text-sm mb-8">
            Sign in with your BIAL Microsoft account to access your workspace.
          </p>

          {notice && (
            <div
              data-testid="login-notice"
              className="mb-4 px-4 py-3 rounded-xl bg-amber-50 border border-amber-200 text-amber-700 text-sm"
            >
              {notice}
            </div>
          )}

          <button
            type="button"
            data-testid="login-microsoft"
            onClick={signIn}
            className="w-full flex items-center justify-center gap-3 border border-bial-border hover:border-primary hover:bg-surface-muted text-tertiary font-semibold py-3 rounded-xl transition shadow-sm"
          >
            {/* Microsoft four-square logo */}
            <svg width="18" height="18" viewBox="0 0 21 21" aria-hidden="true">
              <rect x="1" y="1" width="9" height="9" fill="#F25022" />
              <rect x="11" y="1" width="9" height="9" fill="#7FBA00" />
              <rect x="1" y="11" width="9" height="9" fill="#00A4EF" />
              <rect x="11" y="11" width="9" height="9" fill="#FFB900" />
            </svg>
            Sign in with Microsoft
          </button>

          <p className="mt-6 text-center text-xs text-neutral">
            Access is limited to BIAL staff. Your organization manages this sign-in.
          </p>
        </div>
      </div>
    </div>
  )
}
