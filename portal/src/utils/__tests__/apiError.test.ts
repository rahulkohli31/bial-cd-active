import { describe, it, expect } from 'vitest'
import { ApiError, extractApiCode, extractApiMessage, isSuspended, readApiError } from '../apiError'

describe('extractApiMessage — envelope 1: {error:{message, code?}}', () => {
  it('returns the domain message', () => {
    expect(extractApiMessage({ error: { message: 'Project not found.' } }, 404, 'Failed to load project')).toBe('Project not found.')
  })
  it('falls back when `error` is present but carries no message', () => {
    expect(extractApiMessage({ error: {} }, 500, 'Failed to load project')).toBe('Failed to load project (500).')
  })
  it('falls back when `error.message` is an empty string', () => {
    expect(extractApiMessage({ error: { message: '' } }, 500, 'Failed to load project')).toBe('Failed to load project (500).')
  })
  it('falls back when `error` is not an object', () => {
    expect(extractApiMessage({ error: 'boom' }, 500, 'Failed to load project')).toBe('Failed to load project (500).')
  })
})

describe('extractApiMessage — envelope 2: {detail: string}', () => {
  it('returns the string detail', () => {
    expect(extractApiMessage({ detail: 'Account suspended' }, 403, 'Request failed')).toBe('Account suspended')
  })
  it('returns the 401 detail', () => {
    expect(extractApiMessage({ detail: 'Not authenticated' }, 401, 'Request failed')).toBe('Not authenticated')
  })
  it('falls back on an empty string detail', () => {
    expect(extractApiMessage({ detail: '' }, 500, 'Request failed')).toBe('Request failed (500).')
  })
})

describe('extractApiMessage — envelope 3: {detail:[{type,loc,msg}]}', () => {
  it('surfaces the Pydantic field message', () => {
    const body = { detail: [{ type: 'string_too_long', loc: ['body', 'name'], msg: 'String should have at most 120 characters' }] }
    expect(extractApiMessage(body, 422, 'Failed to create project')).toContain('at most 120 characters')
  })
  it('joins two entries with "; "', () => {
    const body = {
      detail: [
        { type: 'string_too_long', loc: ['body', 'name'], msg: 'String should have at most 120 characters' },
        { type: 'string_too_long', loc: ['body', 'description'], msg: 'String should have at most 2000 characters' },
      ],
    }
    expect(extractApiMessage(body, 422, 'Failed to create project')).toBe(
      'String should have at most 120 characters; String should have at most 2000 characters',
    )
  })
  it('falls back when the array carries no usable msg', () => {
    expect(extractApiMessage({ detail: [{ type: 'x', loc: ['body'] }] }, 422, 'Failed to create project')).toBe('Failed to create project (422).')
  })
  it('falls back on an empty detail array', () => {
    expect(extractApiMessage({ detail: [] }, 422, 'Failed to create project')).toBe('Failed to create project (422).')
  })
})

describe('extractApiMessage — no recognizable envelope', () => {
  it.each([[{}], [null], [undefined], ['not json'], [[]], [42]])('falls back for %o', (body) => {
    expect(extractApiMessage(body, 503, 'Failed to reach service')).toBe('Failed to reach service (503).')
  })
  it('never returns undefined', () => {
    expect(typeof extractApiMessage({ error: {} }, 500, 'x')).toBe('string')
  })
  it('prefers `error.message` over `detail` when both are present', () => {
    expect(extractApiMessage({ error: { message: 'domain' }, detail: 'fastapi' }, 400, 'x')).toBe('domain')
  })
})

describe('extractApiCode', () => {
  it('reads the domain code', () => {
    expect(extractApiCode({ error: { message: 'Over budget', code: 'daily_token_limit_exceeded' } })).toBe('daily_token_limit_exceeded')
  })
  it('is null when absent', () => {
    expect(extractApiCode({ error: { message: 'x' } })).toBeNull()
    expect(extractApiCode({ detail: 'x' })).toBeNull()
    expect(extractApiCode(null)).toBeNull()
  })
})

describe('isSuspended', () => {
  it('is true only for 403 + the exact backend copy', () => {
    expect(isSuspended({ detail: 'Account suspended' }, 403)).toBe(true)
  })
  it('is false for 403 CSRF failure', () => {
    expect(isSuspended({ detail: 'CSRF check failed' }, 403)).toBe(false)
  })
  it('is false for 403 super-admin gate', () => {
    expect(isSuspended({ detail: 'Super-admin privileges required.' }, 403)).toBe(false)
  })
  it('is false for 401 "Not authenticated"', () => {
    expect(isSuspended({ detail: 'Not authenticated' }, 401)).toBe(false)
  })
  it('is false when the same body arrives on a non-403 status — status alone must never trigger it', () => {
    expect(isSuspended({ detail: 'Account suspended' }, 200)).toBe(false)
    expect(isSuspended({ detail: 'Account suspended' }, 500)).toBe(false)
  })
  it('is false for a non-JSON / non-object body', () => {
    expect(isSuspended('Account suspended', 403)).toBe(false)
    expect(isSuspended(null, 403)).toBe(false)
    expect(isSuspended(undefined, 403)).toBe(false)
  })
})

describe('ApiError', () => {
  it('carries status and code', () => {
    const err = new ApiError('Over budget', 429, 'daily_token_limit_exceeded')
    expect(err).toBeInstanceOf(Error)
    expect(err.message).toBe('Over budget')
    expect(err.status).toBe(429)
    expect(err.code).toBe('daily_token_limit_exceeded')
    expect(err.name).toBe('ApiError')
  })
  it('defaults code to null', () => {
    expect(new ApiError('x', 500).code).toBeNull()
  })
})

describe('readApiError', () => {
  const res = (status: number, json: () => Promise<unknown>) => ({ status, json }) as unknown as Response

  it('builds an ApiError from the domain envelope', async () => {
    const err = await readApiError(res(404, async () => ({ error: { message: 'Project not found.' } })), 'Failed to load project')
    expect(err.status).toBe(404)
    expect(err.message).toBe('Project not found.')
    expect(err.code).toBeNull()
  })
  it('carries the domain code through', async () => {
    const err = await readApiError(
      res(429, async () => ({ error: { message: 'Daily limit reached', code: 'daily_token_limit_exceeded' } })),
      'Failed',
    )
    expect(err.code).toBe('daily_token_limit_exceeded')
  })
  it('builds from the Pydantic 422 envelope', async () => {
    const err = await readApiError(res(422, async () => ({ detail: [{ msg: 'String should have at most 120 characters' }] })), 'Failed to create project')
    expect(err.message).toContain('at most 120 characters')
    expect(err.status).toBe(422)
  })
  it('falls back when the body is not JSON', async () => {
    const err = await readApiError(
      res(502, async () => {
        throw new SyntaxError('Unexpected token')
      }),
      'Failed to reach service',
    )
    expect(err.message).toBe('Failed to reach service (502).')
    expect(err.status).toBe(502)
  })
})
