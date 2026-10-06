/**
 * The gate a framed app's report passes before anything acts on it.
 *
 * Every generated app shares one origin, so no single half of the gate tells this frame's app
 * from another: each half is pinned on its own, with the other two passing.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { MOUNTED_TYPE, isFromFrame, isTrustedFrameReport, originOf } from '../frameReport'

const APPS = 'https://apps.example'
const VIEW = `${APPS}/a/shr-0123456789abcdef0123456789ab/`
const VIEW_PATH = '/a/shr-0123456789abcdef0123456789ab'

function aFrame(): Window {
  const el = document.createElement('iframe')
  document.body.appendChild(el)
  if (!el.contentWindow) throw new Error('jsdom gave the frame no window')
  return el.contentWindow
}

function message(source: Window | null, data: unknown, origin: string = APPS): MessageEvent {
  return new MessageEvent('message', { data, origin, source })
}

afterEach(() => {
  document.body.replaceChildren()
})

describe('a report trusted only from its own frame, at its origin and its path', () => {
  it('accepts the framed document reporting itself', () => {
    const frame = aFrame()
    const e = message(frame, { type: MOUNTED_TYPE, path: `${VIEW_PATH}/inbox/` })
    expect(isTrustedFrameReport(e, VIEW, frame, MOUNTED_TYPE)).toBe(true)
  })

  it('refuses another origin', () => {
    const frame = aFrame()
    const e = message(frame, { type: MOUNTED_TYPE, path: VIEW_PATH }, 'https://elsewhere.example')
    expect(isTrustedFrameReport(e, VIEW, frame, MOUNTED_TYPE)).toBe(false)
  })

  it('refuses another window at the same origin', () => {
    const frame = aFrame()
    const e = message(aFrame(), { type: MOUNTED_TYPE, path: VIEW_PATH })
    expect(isTrustedFrameReport(e, VIEW, frame, MOUNTED_TYPE)).toBe(false)
  })

  it('refuses a source-less message while no frame is mounted', () => {
    // `null == undefined`: a loose comparison here would trust every message with no source.
    const e = message(null, { type: MOUNTED_TYPE, path: VIEW_PATH })
    expect(isTrustedFrameReport(e, VIEW, undefined, MOUNTED_TYPE)).toBe(false)
  })

  it('refuses another app`s path, and another report type', () => {
    const frame = aFrame()
    const elsewhere = message(frame, { type: MOUNTED_TYPE, path: '/a/shr-ffffffffffffffffffffffffffff' })
    const painting = message(frame, { type: 'bial:app-painting', path: VIEW_PATH })
    expect(isTrustedFrameReport(elsewhere, VIEW, frame, MOUNTED_TYPE)).toBe(false)
    expect(isTrustedFrameReport(painting, VIEW, frame, MOUNTED_TYPE)).toBe(false)
  })

  it('trusts nothing for an address it cannot read, or one with an opaque origin', () => {
    // An opaque document's messages arrive with the origin string "null", which is truthy.
    const frame = aFrame()
    expect(isFromFrame(message(frame, {}, 'null'), originOf('data:text/html,x'), frame)).toBe(false)
    expect(isFromFrame(message(frame, {}), originOf('not a url'), frame)).toBe(false)
  })
})
