/**
 * THE BOUNDARY THE CITIZEN CAN MOVE (plan 002, U7), through the real shell.
 *
 * Every scenario here is about the RELATIONSHIP between two columns, so none of them is visible to
 * a test that mounts one. What this file pins is the board's four numbers, its two stops, its
 * "disappears rather than becoming a control that cannot help" rule below the stacking threshold —
 * and, above all, that the app never reloads while any of it happens.
 *
 * THE STOPS ARE ASSERTED ON THE PROPERTY, NOT ON A PIXEL. jsdom has no layout engine, so what can
 * be observed is the width the shell PUBLISHES, which is the only thing the handle produces.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, Link } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishHeading,
  usePublishPaneView,
  useWorkspaceProject,
  type PaneView,
} from '../workspaceChannel'
import { RAIL_MAX, RAIL_MIN } from '../railWidth'

vi.mock('../../layout/Navbar', () => ({ default: () => <div data-testid="navbar" /> }))
vi.mock('../../PublishStatusChip', () => ({ default: () => <span data-testid="publish-chip-stub" /> }))
vi.mock('../../LivePreview', () => ({ default: () => <div data-testid="live-preview" /> }))

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'

const EMPTY_PANE: PaneView = {
  iterating: false, reconnecting: false,
  relaunching: false, relaunchError: null, lastBuildFailed: false,
  restoredFromFailedBuild: false, completedLive: true, hasSavedBuild: null,
  previewState: null, occupyingProjectName: null, turnRunning: false,
  compileState: null, workspaceLost: false,
}

function Surface({ pane = true }: { pane?: boolean }) {
  useWorkspaceProject('pA')
  usePublishHeading({ projectId: 'pA', projectName: 'Visitor Log', chatTitle: null, chatKind: null })
  usePublishAddress({ url: APP_URL, status: 'ready' }, 'pA')
  usePublishPaneView(EMPTY_PANE)
  useAppPaneVisible(pane)
  return <div data-testid="surface" />
}

function Workspace({ pane = true }: { pane?: boolean }) {
  return (
    <MemoryRouter initialEntries={['/projects/pA']}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={
              <>
                <Link to="/chat/c1">to chat</Link>
                <Surface pane={pane} />
              </>
            }
          />
          <Route path="/chat/:chatId" element={<Surface pane={pane} />} />
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

const rail = () => screen.getByTestId('workspace-outlet')
const handle = () => screen.getByTestId('rail-resize-handle')
const widthOf = () => rail().style.getPropertyValue('--rail-w')
/** `LivePreview` is stubbed here — this suite is about the shell, not the frame — so the thing
 *  that must not be torn down is the stub's own node. Identity is the whole assertion either way:
 *  a remount produces a NEW element, and that is what a reload of the citizen's app looks like
 *  from outside. */
const frame = () => screen.queryByTestId('live-preview')

/** jsdom implements none of the pointer-capture API the handle relies on. */
beforeEach(() => {
  const captured = new Set<number>()
  Element.prototype.setPointerCapture = function setPointerCapture(id: number) {
    captured.add(id)
  }
  Element.prototype.hasPointerCapture = function hasPointerCapture(id: number) {
    return captured.has(id)
  }
  Element.prototype.releasePointerCapture = function releasePointerCapture(id: number) {
    captured.delete(id)
  }
  window.localStorage.clear()
})
afterEach(() => {
  cleanup()
  window.localStorage.clear()
})

const drag = (to: number) => {
  fireEvent.pointerDown(handle(), { pointerId: 1, clientX: 400 })
  fireEvent.pointerMove(handle(), { pointerId: 1, clientX: to })
  fireEvent.pointerUp(handle(), { pointerId: 1, clientX: to })
}

describe('the handle drags between the board\'s stops, and stops there', () => {
  it('follows the pointer inside the range', () => {
    render(<Workspace />)
    drag(500)
    expect(widthOf()).toBe('500px')
  })

  it('★ will not go below the narrowest, where the composer and the status rows start wrapping', () => {
    render(<Workspace />)
    drag(120)
    expect(widthOf()).toBe(`${RAIL_MIN}px`)
  })

  it('★ will not go past the widest, where the app becomes a sliver', () => {
    render(<Workspace />)
    drag(1400)
    expect(widthOf()).toBe(`${RAIL_MAX}px`)
  })

  it('★ losing pointer capture mid-drag leaves the rail at a VALID width, not back where it started', () => {
    render(<Workspace />)
    fireEvent.pointerDown(handle(), { pointerId: 1, clientX: 400 })
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: 470 })
    fireEvent.lostPointerCapture(handle(), { pointerId: 1 })
    expect(widthOf()).toBe('470px')
  })
})

describe('the keyboard reaches every width the pointer does', () => {
  it('moves the boundary with the arrow keys, and says where it is', () => {
    render(<Workspace />)
    expect(handle().getAttribute('role')).toBe('separator')
    expect(handle().getAttribute('aria-orientation')).toBe('vertical')
    expect(handle().getAttribute('aria-controls')).toBe(rail().id)
    expect(handle().getAttribute('aria-valuemin')).toBe(String(RAIL_MIN))
    expect(handle().getAttribute('aria-valuemax')).toBe(String(RAIL_MAX))
    const before = Number(handle().getAttribute('aria-valuenow'))

    fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    expect(Number(handle().getAttribute('aria-valuenow'))).toBeGreaterThan(before)

    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(Number(handle().getAttribute('aria-valuenow'))).toBe(before)
  })

  it('reaches both ends without 28 presses, and clamps there', () => {
    render(<Workspace />)
    fireEvent.keyDown(handle(), { key: 'End' })
    expect(widthOf()).toBe(`${RAIL_MAX}px`)
    fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    expect(widthOf()).toBe(`${RAIL_MAX}px`)

    fireEvent.keyDown(handle(), { key: 'Home' })
    expect(widthOf()).toBe(`${RAIL_MIN}px`)
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(widthOf()).toBe(`${RAIL_MIN}px`)
  })

  it('is reachable by keyboard at all', () => {
    render(<Workspace />)
    expect(handle().getAttribute('tabindex')).toBe('0')
  })
})

describe('what the handle remembers, and what it does not', () => {
  it('★ the chosen width survives a route change from the project screen to a chat', () => {
    // It could not while the width was a per-mode literal: moving to a chat replaced 400 with 520
    // and the citizen's choice was gone. Their width replaces both opening widths now.
    render(<Workspace />)
    drag(600)
    fireEvent.click(screen.getByText('to chat'))
    expect(widthOf()).toBe('600px')
  })

  it('★ a press on the divider that never moved remembers nothing', () => {
    // THE DEFECT THIS IS WRITTEN AGAINST. A remembered width replaces BOTH opening widths, so
    // committing on every `pointerup` meant one stray click on the 9px divider inside a chat
    // pinned every project screen at the chat's 520px — a preference the citizen never expressed.
    render(<Workspace />)
    fireEvent.pointerDown(handle(), { pointerId: 1, clientX: 400 })
    fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 400 })

    expect(window.localStorage.getItem('bial:rail-width')).toBeNull()
    // LIVENESS: the same handle in the same render DOES remember a real drag, so the assertion
    // above is a guard holding rather than a handle that stopped working.
    drag(560)
    expect(window.localStorage.getItem('bial:rail-width')).toBe('560')
  })

  it('★ one drag writes the preference once, including the browser\'s own capture-loss echo', () => {
    // `end` is wired to `pointerup`, `pointercancel` AND `lostpointercapture`, and releasing
    // capture inside it makes the browser queue a `lostpointercapture` of its own — so a single
    // drag entered the commit twice. jsdom's capture stubs fire no such event, which is why the
    // echo is dispatched here explicitly: it is what a real browser does after every drag.
    const writes = vi.spyOn(Storage.prototype, 'setItem')
    render(<Workspace />)

    fireEvent.pointerDown(handle(), { pointerId: 1, clientX: 400 })
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: 560 })
    fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 560 })
    fireEvent.lostPointerCapture(handle(), { pointerId: 1 })

    expect(writes.mock.calls.filter(([key]) => key === 'bial:rail-width')).toEqual([
      ['bial:rail-width', '560'],
    ])
    writes.mockRestore()
  })

  it('opens at the board\'s own width when nothing is remembered', () => {
    render(<Workspace />)
    expect(widthOf()).toBe('400px')
  })

  it('opens at the remembered one once there is one, for every project', () => {
    window.localStorage.setItem('bial:rail-width', '585')
    render(<Workspace />)
    expect(widthOf()).toBe('585px')
  })

  it('ignores a stored value it cannot read, rather than clamping a guess out of it', () => {
    // `null` means "we do not know", and the two opening widths differ — inventing 360 from a
    // corrupt entry would silently narrow every workspace this person opens.
    window.localStorage.setItem('bial:rail-width', 'not-a-number')
    render(<Workspace />)
    expect(widthOf()).toBe('400px')
  })
})

describe('where the handle is NOT', () => {
  it('★ is absent on a surface with no pane — there is no boundary to move', () => {
    render(<Workspace pane={false} />)
    expect(screen.queryByTestId('rail-resize-handle')).toBeNull()
    // LIVENESS: the workspace rendered, it simply has one column.
    expect(rail().className).toMatch(/flex-1/)
  })

  it('★ is absent while the rail is collapsed, and the collapse ZEROES the width', async () => {
    // THE TRAP THE PLAN NAMES. The hide treatment keeps the element's layout box, so a leftover
    // `--rail-w` leaves an invisible 400px gap exactly where the rail was — a strip of nothing
    // the citizen cannot see and cannot click past. Mutation receipt: keep the width on the
    // collapsed arm and this goes red.
    render(<Workspace />)
    drag(600)
    fireEvent.click(screen.getByRole('button', { name: 'Hide details' }))

    await waitFor(() => expect(widthOf()).toBe('0px'))
    expect(screen.queryByTestId('rail-resize-handle')).toBeNull()
    expect(rail().className).toMatch(/invisible/)
  })

  it('disappears below the stacking threshold rather than becoming a control that cannot help', () => {
    // jsdom evaluates no media queries, so what is assertable is the MECHANISM: the handle is
    // hidden by a class below the threshold and shown above it, which is the same responsive-class
    // approach the grid itself uses — no `matchMedia`, no `ResizeObserver`.
    render(<Workspace />)
    expect(handle().className).toMatch(/(^|\s)hidden(\s|$)/)
    expect(handle().className).toMatch(/wide:flex/)
  })
})

describe('the app never reloads while any of this happens', () => {
  it('★ dragging, keying and collapsing all leave the frame untouched', async () => {
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    drag(620)
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    fireEvent.click(screen.getByRole('button', { name: 'Hide details' }))
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }))

    expect(frame()).toBe(original)
  })
})
