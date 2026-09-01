/**
 * ONE ROW OF ACTIVITY (R35b, R36's rendering half).
 *
 * The row vocabulary is VERB + TARGET + STATE, and the row is allowed to read exactly two things:
 * the server's friendly label and the state. It could not leak a file path if it tried —
 * `convertMessage` never copies `detail`, `args` or `result` onto the part — but the guarantees
 * this file pins are the ones a redesign would quietly drop.
 *
 * ══ THE 11,558px BUG ══
 *
 * `sr-only` is `position: absolute` with no inset. Without a POSITIONED ANCESTOR the "failed" span
 * anchors to the document and lands wherever the page happens to be tall — measured at 11,558px
 * against an 836px viewport. That is why `relative` is on the row and why it is asserted here: it
 * looks like a stray utility class and is load-bearing.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

import { ToolActivityLine } from '../ToolActivityLine'
import ActivityRow from '../ActivityRow'
import { UNRECOGNISED_STEP } from '../ActivityGroup'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const row = () => document.querySelector('[data-kind="tool-activity"]') as HTMLElement

/** jsdom has no `matchMedia` matcher by default; the setup file provides a non-matching stub. */
function prefersReducedMotion(reduce: boolean) {
  vi.spyOn(window, 'matchMedia').mockImplementation(
    (query: string) =>
      ({
        matches: reduce,
        media: query,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  )
}

describe('the label and the state, and nothing else', () => {
  it('renders the server’s label with a check when a step succeeded', () => {
    render(<ToolActivityLine label="Updated the home page" state="ok" />)
    expect(screen.getByText('Updated the home page')).toBeTruthy()
    expect(row().getAttribute('data-state')).toBe('ok')
  })

  it('carries failure as TEXT, not colour alone (WCAG 1.4.1)', () => {
    // A red tint is invisible to a reader who cannot distinguish it and to anything reading the
    // DOM. The glyph SHAPE changes and an sr-only word says so; the label itself stays neutral,
    // because colouring the label would be the colour-only signal wearing a different hat.
    render(<ToolActivityLine label="Ran the type check" state="failed" />)
    expect(screen.getByText('failed')).toBeTruthy()
    expect(screen.getByText('failed').className).toContain('sr-only')
    expect(screen.getByText('Ran the type check').className).not.toContain('danger')
  })

  it('the sr-only span has a POSITIONED ancestor — the 11,558px regression guard', () => {
    render(<ToolActivityLine label="Ran the type check" state="failed" />)
    // Asserted on the row that CONTAINS the span, so this fails if `relative` is dropped from it
    // even though the span itself is unchanged.
    expect(row().className).toContain('relative')
    expect(row().contains(screen.getByText('failed'))).toBe(true)
  })

  it('says nothing extra on a succeeding row', () => {
    render(<ToolActivityLine label="Updated the home page" state="ok" />)
    expect(screen.queryByText('failed')).toBeNull()
  })
})

describe('prefers-reduced-motion', () => {
  it('spins while running by default', () => {
    prefersReducedMotion(false)
    render(<ToolActivityLine label="Installing dependencies" state="started" />)
    expect(row().querySelector('.animate-spin')).toBeTruthy()
  })

  it('does NOT spin when the reader has asked for less motion', () => {
    // The one piece of chrome on this surface that moves continuously, so the preference means it.
    prefersReducedMotion(true)
    render(<ToolActivityLine label="Installing dependencies" state="started" />)
    expect(row().querySelector('.animate-spin')).toBeNull()
    // LIVENESS: the running glyph is still there, just still.
    expect(row().getAttribute('data-state')).toBe('started')
  })
})

describe('ActivityRow — what a converted part becomes', () => {
  /**
   * A tool-call part as the library hands one over, rendered through a locally-typed alias.
   *
   * `ToolCallMessagePartComponent` is a union that includes a class component, so it cannot be
   * read with `Parameters<…>`; and the library's part type carries a dozen fields this row never
   * reads, so spelling them out would be a fixture maintained against a dependency for no gain.
   *
   * The cases below deliberately pass SHAPES THE TYPE FORBIDS — a missing label, an unknown state
   * — which is the point: the wire is not typed and the row has to survive them.
   */
  const Row = ActivityRow as (props: Record<string, unknown>) => JSX.Element
  const part = (args: unknown) => ({
    type: 'tool-call',
    toolCallId: 'step-1',
    toolName: 'activity',
    args,
  })

  it('reads the label and the state off the part', () => {
    render(<Row {...part({ label: 'Wrote the form', state: 'ok' })} />)
    expect(screen.getByText('Wrote the form')).toBeTruthy()
    expect(row().getAttribute('data-state')).toBe('ok')
  })

  it('an absent or empty label renders the unrecognised phrase — never an empty row', () => {
    // …and never the tool's own name. An unrecognised command reaching the screen as argv is the
    // failure the server's classifier fails closed to prevent; this is the second wall.
    render(<Row {...part({ state: 'ok' })} />)
    expect(screen.getByText(UNRECOGNISED_STEP)).toBeTruthy()

    cleanup()
    render(<Row {...part({ label: '   ', state: 'ok' })} />)
    expect(screen.getByText(UNRECOGNISED_STEP)).toBeTruthy()
  })

  it('an unknown state renders as running rather than throwing', () => {
    render(<Row {...part({ label: 'Something new', state: 'who knows' })} />)
    expect(row().getAttribute('data-state')).toBe('started')
  })

  it('renders nothing from a part carrying no args at all', () => {
    render(<Row {...part(undefined)} />)
    expect(screen.getByText(UNRECOGNISED_STEP)).toBeTruthy()
  })
})
