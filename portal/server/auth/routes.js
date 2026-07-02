/**
 * Auth routes — login / refresh / logout, mounted at /api/auth.
 *
 * Security posture:
 * - Login failures are a single generic 401 (no user enumeration); a dummy
 *   Argon2 verify runs when the user is absent to flatten the timing oracle.
 * - The refresh token self-describes its user (lookup hint only); the SHA-256
 *   hash comparison is the real authorization check, done in constant time.
 * - Refresh rotates on use. A non-matching token returns a generic 401 but does
 *   NOT revoke the stored session (interim: a forged/stale token from an
 *   unauthenticated caller must not be able to evict a user's live session — see
 *   the /refresh handler). Single active session per user: a new login overwrites
 *   the prior refresh hash, which is what invalidates the older device.
 * - Logout is gated by requireAuth so the user comes from the verified `sub`.
 *
 * `repo` is injected so routes are testable against a fake users-repo.
 */
import express from 'express'
import { timingSafeEqual } from 'node:crypto'
import { rateLimit, ipKeyGenerator } from 'express-rate-limit'
import { requireAuth } from './middleware.js'
import { verifyPassword, hashPassword } from './password.js'
import {
  signAccessToken,
  generateRefreshToken,
  parseRefreshToken,
  hashRefreshToken,
  refreshExpiry,
} from './tokens.js'
import { resolveUserLimits, defaultLimits } from '../limits.js'

const MAX_USERNAME = 256
const MAX_PASSWORD = 1024
const GENERIC_LOGIN_401 = 'Incorrect username or password.'
const GENERIC_REFRESH_401 = 'Session expired. Please sign in again.'

// Password sign-in is being decommissioned — Entra ID (the FastAPI control-plane)
// is the way in. Default ENABLED so nothing breaks until ops flips the flag the
// MOMENT the FastAPI Entra path is verified live in production, closing the
// Entra-offboarding-bypass window as early as possible and decoupled from the
// later physical route-removal deploy (ADR-0007 / U9). PASSWORD_LOGIN_ENABLED=false
// (or 0) rejects new password logins AND refreshes; outstanding Express sessions
// are then bounded by the short access-token TTL.
function passwordLoginEnabledFromEnv() {
  const v = process.env.PASSWORD_LOGIN_ENABLED
  return v !== 'false' && v !== '0'
}

const PASSWORD_LOGIN_DISABLED = {
  error: { message: 'Password sign-in is disabled. Please sign in with your Microsoft account.' },
}

/** Login rate limiter keyed by username + IP (not IP alone — BIAL shares egress). */
export function makeLoginLimiter(options = {}) {
  return rateLimit({
    windowMs: 15 * 60 * 1000,
    limit: 10,
    standardHeaders: true,
    legacyHeaders: false,
    keyGenerator: (req) => `${req.body?.username || 'anon'}:${ipKeyGenerator(req.ip || '0.0.0.0')}`,
    handler: (_req, res) =>
      res.status(429).json({ error: { message: 'Too many login attempts. Please try again later.' } }),
    ...options,
  })
}

const TOO_MANY = { error: { message: 'Too many requests. Please try again later.' } }

/**
 * IP-only ceiling limiter. The per-username:IP limiters can't see an attacker
 * spraying ACROSS usernames from one IP (each username is its own bucket), and
 * each absent-user attempt costs a full Argon2 verify. This caps total attempts
 * per IP so credential spraying and the Argon2 amplifier are both bounded.
 *
 * IMPORTANT: this keys on req.ip, so it only protects WITHOUT locking out real
 * users when req.ip is the per-client address (i.e. `trust proxy` matches the
 * real hop count). If clients genuinely share one ingress IP (corporate NAT),
 * set AUTH_IP_RATE_LIMIT=0 to disable this ceiling — the per-username:IP limiters
 * still apply. Default limit 60 / 15min; override with AUTH_IP_RATE_LIMIT.
 */
export function makeIpCeilingLimiter(options = {}) {
  const limit = options.limit ?? Number(process.env.AUTH_IP_RATE_LIMIT ?? 60)
  if (!limit) return (_req, _res, next) => next() // 0 / NaN → disabled
  return rateLimit({
    windowMs: 15 * 60 * 1000,
    standardHeaders: true,
    legacyHeaders: false,
    keyGenerator: (req) => ipKeyGenerator(req.ip || '0.0.0.0'),
    handler: (_req, res) => res.status(429).json(TOO_MANY),
    ...options,
    limit,
  })
}

/**
 * Refresh limiter keyed by the embedded username hint + IP (mirrors login), so
 * flooding /refresh with forged tokens aimed at one user is throttled.
 */
export function makeRefreshLimiter(options = {}) {
  return rateLimit({
    windowMs: 15 * 60 * 1000,
    limit: 30,
    standardHeaders: true,
    legacyHeaders: false,
    keyGenerator: (req) =>
      `${parseRefreshToken(req.body?.refreshToken) || 'anon'}:${ipKeyGenerator(req.ip || '0.0.0.0')}`,
    handler: (_req, res) => res.status(429).json(TOO_MANY),
    ...options,
  })
}

// Precomputed dummy hash so the absent-user path still spends ~one Argon2
// verify, avoiding a timing distinction between "no such user" and "bad pw".
// Computed eagerly at module load (not lazily on the first absent-user hit) so
// concurrent first hits don't each pay an Argon2 hash, and a one-off Argon2
// failure can't cache a rejected promise that turns the absent-user path into a
// 500 — which would re-open the very enumeration oracle this defends against.
const dummyHashPromise = hashPassword('timing-defense-placeholder-secret').catch(() => null)
function dummyVerify(password) {
  return dummyHashPromise.then((phc) => (phc ? verifyPassword(password, phc) : false))
}

function profileOf(user, defaults) {
  return {
    username: user.username,
    name: user.name,
    role: user.role,
    isAdmin: user.role === 'admin',
    // Effective limits so the SPA can drive the per-conversation guardrail
    // (and show the daily ceiling) without a separate fetch.
    limits: resolveUserLimits(user, defaults),
  }
}

function safeHashEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false
  return timingSafeEqual(Buffer.from(a), Buffer.from(b))
}

export function createAuthRouter({
  repo,
  defaults = defaultLimits(),
  loginLimiter = makeLoginLimiter(),
  refreshLimiter = makeRefreshLimiter(),
  ipCeilingLimiter = makeIpCeilingLimiter(),
  passwordLoginEnabled = passwordLoginEnabledFromEnv(),
} = {}) {
  if (!repo) throw new Error('createAuthRouter: repo is required')
  const router = express.Router()

  // The decommission gate: when password login is off, /login and /refresh reject
  // with 503 BEFORE any rate-limit or credential work. Logout stays open so a user
  // can always end a lingering session.
  const passwordGate = (_req, res, next) =>
    passwordLoginEnabled ? next() : res.status(503).json(PASSWORD_LOGIN_DISABLED)

  router.post('/login', passwordGate, ipCeilingLimiter, loginLimiter, async (req, res) => {
    try {
      const { username, password } = req.body || {}
      if (
        typeof username !== 'string' || typeof password !== 'string' ||
        username.length === 0 || password.length === 0 ||
        username.length > MAX_USERNAME || password.length > MAX_PASSWORD
      ) {
        return res.status(400).json({ error: { message: 'Username and password are required.' } })
      }

      const user = await repo.findByUsername(username)
      if (!user) {
        await dummyVerify(password)
        return res.status(401).json({ error: { message: GENERIC_LOGIN_401 } })
      }
      if (!(await verifyPassword(password, user.passwordHash))) {
        return res.status(401).json({ error: { message: GENERIC_LOGIN_401 } })
      }

      const accessToken = signAccessToken({ sub: user.username, username: user.username, role: user.role })
      const refreshToken = generateRefreshToken(user.username)
      // Overwrite any prior session — single active session per user.
      await repo.setRefreshHash(user.username, hashRefreshToken(refreshToken), refreshExpiry())
      return res.json({ accessToken, refreshToken, user: profileOf(user, defaults) })
    } catch (err) {
      console.error('login error:', err.message)
      return res.status(500).json({ error: { message: 'Internal error.' } })
    }
  })

  router.post('/refresh', passwordGate, ipCeilingLimiter, refreshLimiter, async (req, res) => {
    try {
      const presented = req.body?.refreshToken
      const username = parseRefreshToken(presented)
      if (!username) return res.status(401).json({ error: { message: GENERIC_REFRESH_401 } })

      const user = await repo.findByUsername(username)
      if (!user || !user.refreshTokenHash) {
        return res.status(401).json({ error: { message: GENERIC_REFRESH_401 } })
      }

      // The hash comparison — not the embedded username — is the auth check.
      // Interim: a mismatch returns a generic 401 but does NOT revoke the stored
      // session. A forged/stale token from an unauthenticated caller is
      // indistinguishable from a genuine replay, so revoking here let anyone
      // evict a known user's live session (unauthenticated DoS). Real
      // reuse-detection returns later via per-user token-family lineage.
      if (!safeHashEqual(hashRefreshToken(presented), user.refreshTokenHash)) {
        return res.status(401).json({ error: { message: GENERIC_REFRESH_401 } })
      }

      const expired = !user.refreshTokenExpiresAt || Date.parse(user.refreshTokenExpiresAt) <= Date.now()
      if (expired) {
        await repo.clearRefreshHash(user.username)
        return res.status(401).json({ error: { message: GENERIC_REFRESH_401 } })
      }

      // Rotate: new refresh token replaces the stored hash; new access token.
      const refreshToken = generateRefreshToken(user.username)
      await repo.setRefreshHash(user.username, hashRefreshToken(refreshToken), refreshExpiry())
      const accessToken = signAccessToken({ sub: user.username, username: user.username, role: user.role })
      return res.json({ accessToken, refreshToken, user: profileOf(user, defaults) })
    } catch (err) {
      console.error('refresh error:', err.message)
      return res.status(500).json({ error: { message: 'Internal error.' } })
    }
  })

  router.post('/logout', requireAuth, async (req, res) => {
    try {
      await repo.clearRefreshHash(req.user.sub) // idempotent
      return res.json({ ok: true })
    } catch (err) {
      console.error('logout error:', err.message)
      // Never block the client on logout; treat as best-effort.
      return res.json({ ok: true })
    }
  })

  return router
}
