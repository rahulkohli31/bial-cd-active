import { describe, it, expect } from 'vitest'
import { extractDataSchema } from '../BuilderPage.jsx'
import { describeAppFailure } from '../../utils/chatErrors'
import { ApiError } from '../../utils/apiError'

describe('BuilderPage — data-wiring detection helpers (U11)', () => {
  // `usesDataService` is gone with the gate it guarded. Provision no longer waits for a
  // BIALData call: app_registry.current_code is the PROJECT's durable source, so a build
  // with no data wiring must still reach it or the project's next chat seeds from nothing.

  it('describeAppFailure names the 409 owner conflict instead of a generic save failure', () => {
    expect(describeAppFailure(new ApiError('conflict', 409))).toMatch(/belongs to another user/i)
    expect(describeAppFailure(new ApiError('gone', 404))).toMatch(/project was deleted/i)
    expect(describeAppFailure(new Error('network'))).toBe('Your generated app could not be saved.')
  })

  it('extractDataSchema pins the first BIALData collection name', () => {
    expect(extractDataSchema("BIALData.save('inspections', { gate: 4 })")).toEqual({ collection: 'inspections' })
    expect(extractDataSchema('BIALData.list("equipment", { limit: 50 })')).toEqual({ collection: 'equipment' })
    expect(extractDataSchema('no data calls here')).toBeNull()
    expect(extractDataSchema(undefined)).toBeNull()
  })
})
