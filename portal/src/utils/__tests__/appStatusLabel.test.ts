import { describe, it, expect } from 'vitest'
import { statusFor } from '../appStatusLabel'
import type { Project } from '../projectApi'

type Facts = Pick<Project, 'appStatus' | 'isServing' | 'isPublishing'>

const facts = (over: Partial<Facts>): Facts => ({
  appStatus: 'draft',
  isServing: false,
  isPublishing: false,
  ...over,
})

describe('statusFor', () => {
  it('says Starting up while a publish runs, over the lifecycle label', () => {
    expect(statusFor(facts({ isPublishing: true }))).toEqual({ label: 'Starting up', tone: 'live' })
  })

  it('says Starting up over Live while a live app gets a new version', () => {
    expect(statusFor(facts({ isPublishing: true, isServing: true }))).toEqual({
      label: 'Starting up',
      tone: 'live',
    })
  })

  it('says Live once the publish finishes, and the lifecycle label when nothing serves', () => {
    expect(statusFor(facts({ isServing: true }))).toEqual({ label: 'Live', tone: 'live' })
    expect(statusFor(facts({}))).toEqual({ label: 'Draft', tone: 'idle' })
  })
})
