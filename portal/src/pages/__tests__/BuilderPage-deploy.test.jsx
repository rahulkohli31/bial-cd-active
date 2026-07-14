import { describe, it, expect } from 'vitest'
import { describeAppFailure } from '../../utils/chatErrors'
import { ApiError } from '../../utils/apiError'

// U5 retired the single-file build path, so BuilderPage no longer exports `extractDataSchema`
// (the single-collection pin died with the `finalCode = extractPreviewCode(result)` that fed it).
// `describeAppFailure` remains the copy for a failed conversation/app write, still exercised here.

describe('BuilderPage — app-failure copy', () => {
  it('describeAppFailure names the 409 owner conflict instead of a generic save failure', () => {
    expect(describeAppFailure(new ApiError('conflict', 409))).toMatch(/belongs to another user/i)
    expect(describeAppFailure(new ApiError('gone', 404))).toMatch(/project was deleted/i)
    expect(describeAppFailure(new Error('network'))).toBe('Your generated app could not be saved.')
  })
})
