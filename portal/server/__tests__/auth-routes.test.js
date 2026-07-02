import { describe, it, expect, beforeAll, vi } from 'vitest'
import express from 'express'
import request from 'supertest'
import { createAuthRouter, makeLoginLimiter, makeRefreshLimiter, makeIpCeilingLimiter } from '../auth/routes.js'
import { createUsersRepo } from '../users-repo.js'
import { hashPassword } from '../auth/password.js'
import * as password from '../auth/password.js'
import { makeFakeContainer } from './fakeCosmos.js'

beforeAll(() => {
  process.env.JWT_SECRET = 'test-secret-test-secret-test-secret-test-secret-test-secret-1234'
})

async function userDoc(username, password, role = 'user') {
  return {
    _id: username,
    username,
    email: `${username}@bial.test`,
    name: username,
    role,
    passwordHash: await hashPassword(password),
    refreshTokenHash: null,
    refreshTokenExpiresAt: null,
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
  }
}

async function makeApp({
  docs = [],
  loginLimiter,
  refreshLimiter,
  ipCeilingLimiter,
  passwordLoginEnabled,
} = {}) {
  const container = makeFakeContainer(docs)
  const repo = createUsersRepo(container)
  const app = express()
  app.use(express.json())
  app.use(
    '/api/auth',
    createAuthRouter({ repo, loginLimiter, refreshLimiter, ipCeilingLimiter, passwordLoginEnabled }),
  )
  return { app, repo, container }
}

describe('POST /api/auth/login', () => {
  it('AE1: correct credentials → 200 with tokens and profile (isAdmin from role)', async () => {
    const { app, container } = await makeApp({ docs: [await userDoc('admin', 'pw-correct', 'admin')] })
    const res = await request(app).post('/api/auth/login').send({ username: 'admin', password: 'pw-correct' })
    expect(res.status).toBe(200)
    expect(res.body.accessToken).toBeTypeOf('string')
    expect(res.body.refreshToken).toBeTypeOf('string')
    expect(res.body.user).toMatchObject({ username: 'admin', role: 'admin', isAdmin: true })
    // refresh hash was stored (only the hash, never the token)
    expect(container._get('admin').refreshTokenHash).toBeTypeOf('string')
    expect(container._get('admin').refreshTokenHash).not.toBe(res.body.refreshToken)
  })

  it('AE2: wrong password and unknown username both → identical generic 401', async () => {
    const { app } = await makeApp({ docs: [await userDoc('alice', 'right-pw')] })
    const wrongPw = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'nope' })
    const unknown = await request(app).post('/api/auth/login').send({ username: 'ghost', password: 'whatever' })
    expect(wrongPw.status).toBe(401)
    expect(unknown.status).toBe(401)
    expect(wrongPw.body).toEqual(unknown.body) // no user enumeration
  })

  it('absent-user login still spends an Argon2 verify (timing-defense, not just identical bodies)', async () => {
    const spy = vi.spyOn(password, 'verifyPassword')
    const { app } = await makeApp({ docs: [] })
    const res = await request(app).post('/api/auth/login').send({ username: 'ghost', password: 'whatever' })
    expect(res.status).toBe(401)
    // dummyVerify must run a real verify on the absent path so timing can't
    // distinguish "no such user" from "wrong password". If the dummyVerify call
    // were removed, this absent-user path would short-circuit and never verify.
    expect(spy).toHaveBeenCalled()
    spy.mockRestore()
  })

  it('empty / oversized fields → 400', async () => {
    const { app } = await makeApp({ docs: [] })
    const empty = await request(app).post('/api/auth/login').send({ username: '', password: '' })
    const huge = await request(app).post('/api/auth/login').send({ username: 'a'.repeat(5000), password: 'x' })
    expect(empty.status).toBe(400)
    expect(huge.status).toBe(400)
  })

  it('rate limiter returns 429 after the configured number of rapid attempts', async () => {
    const { app } = await makeApp({
      docs: [await userDoc('alice', 'right-pw')],
      loginLimiter: makeLoginLimiter({ windowMs: 60_000, limit: 2 }),
    })
    const send = () => request(app).post('/api/auth/login').send({ username: 'alice', password: 'wrong' })
    expect((await send()).status).toBe(401)
    expect((await send()).status).toBe(401)
    expect((await send()).status).toBe(429)
  })

  it('IP ceiling limiter caps spraying ACROSS usernames from one IP', async () => {
    const { app } = await makeApp({
      docs: [],
      ipCeilingLimiter: makeIpCeilingLimiter({ windowMs: 60_000, limit: 2 }),
    })
    // distinct usernames never trip the per-username limiter, but the IP ceiling does
    const send = (u) => request(app).post('/api/auth/login').send({ username: u, password: 'x' })
    expect((await send('u1')).status).toBe(401)
    expect((await send('u2')).status).toBe(401)
    expect((await send('u3')).status).toBe(429)
  })
})

describe('POST /api/auth/refresh', () => {
  it('current token → 200 new access + rotated refresh; old hash no longer matches', async () => {
    const { app, container } = await makeApp({ docs: [await userDoc('alice', 'pw')] })
    const login = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    const oldHash = container._get('alice').refreshTokenHash

    const res = await request(app).post('/api/auth/refresh').send({ refreshToken: login.body.refreshToken })
    expect(res.status).toBe(200)
    expect(res.body.accessToken).toBeTypeOf('string')
    expect(res.body.refreshToken).not.toBe(login.body.refreshToken)
    expect(container._get('alice').refreshTokenHash).not.toBe(oldHash)
  })

  it('interim: a rotated-out (stale) token → 401 but does NOT revoke the live session', async () => {
    const { app, container } = await makeApp({ docs: [await userDoc('alice', 'pw')] })
    const login = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    // rotate once → login.refreshToken is now stale; r2 holds the live token
    const r2 = await request(app).post('/api/auth/refresh').send({ refreshToken: login.body.refreshToken })
    const liveHash = container._get('alice').refreshTokenHash

    const stale = await request(app).post('/api/auth/refresh').send({ refreshToken: login.body.refreshToken })
    expect(stale.status).toBe(401)
    // Interim behavior: a mismatch is NOT treated as reuse, so the live session
    // survives (a forged/stale token can't evict it).
    expect(container._get('alice').refreshTokenHash).toBe(liveHash)
    const stillWorks = await request(app).post('/api/auth/refresh').send({ refreshToken: r2.body.refreshToken })
    expect(stillWorks.status).toBe(200)
  })

  it('SECURITY: a forged token for a logged-in user → 401 and does NOT revoke their session (no DoS)', async () => {
    const { app, container } = await makeApp({ docs: [await userDoc('victim', 'pw')] })
    const login = await request(app).post('/api/auth/login').send({ username: 'victim', password: 'pw' })
    const liveHash = container._get('victim').refreshTokenHash
    expect(liveHash).toBeTypeOf('string')

    // Unauthenticated attacker forges base64url(victim).base64url(random):
    // valid shape, wrong hash. Must NOT be able to evict the victim's session.
    const forged = `${Buffer.from('victim').toString('base64url')}.${Buffer.from('attacker-rand').toString('base64url')}`
    const res = await request(app).post('/api/auth/refresh').send({ refreshToken: forged })
    expect(res.status).toBe(401)
    expect(container._get('victim').refreshTokenHash).toBe(liveHash) // session intact

    // the victim's real token still refreshes normally
    const ok = await request(app).post('/api/auth/refresh').send({ refreshToken: login.body.refreshToken })
    expect(ok.status).toBe(200)
  })

  it('an expired refresh token → 401 and the session is revoked', async () => {
    const { app, container } = await makeApp({ docs: [await userDoc('alice', 'pw')] })
    const login = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    // force the stored expiry into the past (the token hash still matches)
    container._store.get('alice').refreshTokenExpiresAt = '2000-01-01T00:00:00.000Z'

    const res = await request(app).post('/api/auth/refresh').send({ refreshToken: login.body.refreshToken })
    expect(res.status).toBe(401)
    expect(container._get('alice').refreshTokenHash).toBeNull() // expiry DOES revoke
  })

  it('refresh limiter returns 429 after the configured number of rapid attempts', async () => {
    const { app } = await makeApp({
      docs: [await userDoc('alice', 'pw')],
      refreshLimiter: makeRefreshLimiter({ windowMs: 60_000, limit: 2 }),
    })
    const send = () => request(app).post('/api/auth/refresh').send({ refreshToken: 'garbage' })
    expect((await send()).status).toBe(401)
    expect((await send()).status).toBe(401)
    expect((await send()).status).toBe(429)
  })

  it('swapped-username token → hash mismatch → 401, no cross-user access', async () => {
    const { app } = await makeApp({
      docs: [await userDoc('alice', 'pw'), await userDoc('bob', 'pw2')],
    })
    const bobLogin = await request(app).post('/api/auth/login').send({ username: 'bob', password: 'pw2' })
    // swap the embedded username hint to 'alice'
    const swapped = `${Buffer.from('alice').toString('base64url')}.${bobLogin.body.refreshToken.split('.')[1]}`
    const res = await request(app).post('/api/auth/refresh').send({ refreshToken: swapped })
    expect(res.status).toBe(401)
  })

  it('malformed / missing refresh token → 401', async () => {
    const { app } = await makeApp({ docs: [] })
    expect((await request(app).post('/api/auth/refresh').send({ refreshToken: 'garbage' })).status).toBe(401)
    expect((await request(app).post('/api/auth/refresh').send({})).status).toBe(401)
  })
})

describe('POST /api/auth/logout', () => {
  it('without a valid access token → 401', async () => {
    const { app } = await makeApp({ docs: [] })
    expect((await request(app).post('/api/auth/logout')).status).toBe(401)
  })

  it('with a valid token → 200 and the user hash cleared (AE5)', async () => {
    const { app, container } = await makeApp({ docs: [await userDoc('alice', 'pw')] })
    const login = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    expect(container._get('alice').refreshTokenHash).toBeTypeOf('string')

    const logout = await request(app)
      .post('/api/auth/logout')
      .set('Authorization', `Bearer ${login.body.accessToken}`)
    expect(logout.status).toBe(200)
    expect(container._get('alice').refreshTokenHash).toBeNull()

    // AE5: refresh with the prior token after logout → 401
    const refresh = await request(app).post('/api/auth/refresh').send({ refreshToken: login.body.refreshToken })
    expect(refresh.status).toBe(401)
  })

  it('is idempotent — logout twice still 200', async () => {
    const { app } = await makeApp({ docs: [await userDoc('alice', 'pw')] })
    const login = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    const auth = `Bearer ${login.body.accessToken}`
    expect((await request(app).post('/api/auth/logout').set('Authorization', auth)).status).toBe(200)
    expect((await request(app).post('/api/auth/logout').set('Authorization', auth)).status).toBe(200)
  })
})

describe('AE6: second login invalidates the first', () => {
  it('device B login overwrites the stored hash so device A refresh → 401', async () => {
    const { app } = await makeApp({ docs: [await userDoc('alice', 'pw')] })
    const deviceA = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    const deviceB = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    expect(deviceB.status).toBe(200)

    // device B is the active session; refresh it first to prove it holds.
    const bRefresh = await request(app).post('/api/auth/refresh').send({ refreshToken: deviceB.body.refreshToken })
    expect(bRefresh.status).toBe(200)
    // device A's stale token no longer matches the hash device B rotated → 401.
    // (Interim does NOT revoke on mismatch; A fails only because B's login
    // overwrote the stored hash — single active session.)
    const aRefresh = await request(app).post('/api/auth/refresh').send({ refreshToken: deviceA.body.refreshToken })
    expect(aRefresh.status).toBe(401)
  })
})

describe('U9 decommission gate (PASSWORD_LOGIN_ENABLED)', () => {
  it('rejects POST /login with 503 when password login is disabled', async () => {
    const { app } = await makeApp({
      docs: [await userDoc('alice', 'pw')],
      passwordLoginEnabled: false,
    })
    const res = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    expect(res.status).toBe(503)
    expect(res.body.error.message).toMatch(/Microsoft account/i)
  })

  it('rejects POST /refresh with 503 when password login is disabled', async () => {
    const { app } = await makeApp({
      docs: [await userDoc('alice', 'pw')],
      passwordLoginEnabled: false,
    })
    // First mint a real refresh token WITH the gate on, then prove the gate blocks
    // a refresh once flipped off.
    const { app: openApp } = await makeApp({ docs: [await userDoc('alice', 'pw')] })
    const login = await request(openApp).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    const res = await request(app).post('/api/auth/refresh').send({ refreshToken: login.body.refreshToken })
    expect(res.status).toBe(503)
  })

  it('login still works by default (gate enabled) — decommission is opt-in', async () => {
    const { app } = await makeApp({ docs: [await userDoc('alice', 'pw')] })
    const res = await request(app).post('/api/auth/login').send({ username: 'alice', password: 'pw' })
    expect(res.status).toBe(200)
  })
})
