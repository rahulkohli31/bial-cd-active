/**
 * THE REVEAL IS MOSTLY TIMING, SO THE TIMING IS WHAT IS ASSERTED.
 *
 * The chat panel begins at the very edge this zone sits on, so a pointer crosses it constantly.
 * "Opens on hover" and "opens once the pointer has rested there" are the same code path with one
 * number changed, and only the second is usable. Every timing test below therefore asserts the
 * NEGATIVE half first — not open yet — because that is the half a "wait long enough and look"
 * test silently drops, and it is the half that tells the two apart.
 *
 * THE OTHER HALF IS WHAT MUST NOT HAPPEN: no click swallowed while hidden, no reflow of the panes
 * when it arrives, no edge zone at all while the chat is away, and no keyboard trap.
 */
import { useEffect } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, act, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MotionGlobalConfig } from 'motion/react'

const h = vi.hoisted(() => ({
  fetchUsageToday: vi.fn(),
  onUsageChanged: vi.fn(),
  isAuthenticated: vi.fn(() => true),
  getStoredUser: vi.fn(),
  logout: vi.fn(),
  fetchAppStatusCounts: vi.fn(),
}))

vi.mock('../../../utils/usage', () => ({
  fetchUsageToday: h.fetchUsageToday,
  onUsageChanged: h.onUsageChanged,
}))
vi.mock('../../../utils/auth', () => ({
  isAuthenticated: h.isAuthenticated,
  getStoredUser: h.getStoredUser,
  logout: h.logout,
}))
vi.mock('../../../utils/attachmentApi', () => ({ revokeAllAttachmentUrls: vi.fn() }))
vi.mock('../../../utils/appRegistryApi', () => ({ fetchAppStatusCounts: h.fetchAppStatusCounts }))

import NavReveal, { NavMenuButton, useNavReveal } from '../NavReveal'
import { EDGE_ZONE_PX, GRACE_MS, REST_MS } from '../../../lib/motion'

MotionGlobalConfig.skipAnimations = true

const USER = { email: 'asha@bial.aero', display_name: 'Asha Rao', isAdmin: false }

/** Comfortably inside a delay, so a slow machine cannot turn "not yet" into a false failure. */
const EARLY = 120

/** How many times the subtree beside the panel has been BUILT, not rendered. A running app lives
 *  down there and some of it is an iframe, which does not survive being rebuilt. */
let mounts = 0

/** Stands in for the application underneath — and for the toolbar's menu button, which reaches
 *  the reveal through context exactly as the real toolbar does. */
function Underneath({ chatHidden = false }: { chatHidden?: boolean }) {
  const reveal = useNavReveal()
  const setChatHidden = reveal?.setChatHidden
  useEffect(() => {
    mounts += 1
  }, [])
  // The workspace is the only writer of the collapsed state, so the test reports it the same way —
  // and from an effect keyed on the prop, so a test can COLLAPSE the chat mid-flight and not only
  // start with it collapsed.
  useEffect(() => {
    setChatHidden?.(chatHidden)
  }, [setChatHidden, chatHidden])
  return (
    <div data-testid="content">
      <NavMenuButton />
      <button type="button" data-testid="underneath" onClick={() => undefined}>
        the chat
      </button>
    </div>
  )
}

function renderReveal(props: { chatHidden?: boolean } = {}) {
  return render(
    <MemoryRouter initialEntries={['/chat/c1']}>
      <NavReveal hideable>
        <Underneath {...props} />
      </NavReveal>
    </MemoryRouter>,
  )
}

/** Move the pointer to an x position and let React settle. */
const pointerAt = (clientX: number) =>
  act(() => { document.dispatchEvent(new PointerEvent('pointermove', { clientX, bubbles: true })) })

/**
 * A REAL CLOCK, DELIBERATELY — and worth knowing before anyone "fixes" it into a fake one.
 * `motion` captures `requestAnimationFrame` at import, before a fake clock could be installed, so
 * under fake timers `AnimatePresence` never finishes its exit and the panel stays mounted for
 * ever. A faked clock can prove the panel OPENS on time but can never prove it LEAVES, which is
 * half of what this file is for. Both were measured before choosing.
 */
const after = (ms: number) =>
  act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)) })

const isOpen = () => screen.queryByTestId('nav-floating') !== null
const settles = (open: boolean) => waitFor(() => expect(isOpen()).toBe(open))

beforeEach(() => {
  vi.clearAllMocks()
  mounts = 0
  window.localStorage.clear()
  h.isAuthenticated.mockReturnValue(true)
  h.getStoredUser.mockReturnValue(USER)
  h.fetchUsageToday.mockResolvedValue(null)
  h.onUsageChanged.mockReturnValue(() => {})
  h.fetchAppStatusCounts.mockResolvedValue({ draft: 0, pending: 0, approved: 0, rejected: 0, disabled: 0 })
})
afterEach(() => cleanup())

describe('three ways in, and one of them is always visible', () => {
  it('the menu button opens with NO delay — the rest belongs to the edge zone alone', () => {
    renderReveal()
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    expect(isOpen()).toBe(true)
  })

  it('⌘\\ opens and closes it from anywhere inside the application', async () => {
    renderReveal()
    act(() => { fireEvent.keyDown(document, { key: '\\', metaKey: true }) })
    expect(isOpen()).toBe(true)
    act(() => { fireEvent.keyDown(document, { key: '\\', metaKey: true }) })
    await settles(false)
  })

  it('Ctrl+\\ works too, for the people not on a Mac', () => {
    renderReveal()
    act(() => { fireEvent.keyDown(document, { key: '\\', ctrlKey: true }) })
    expect(isOpen()).toBe(true)
  })
})

describe('the edge zone tells intent from a pass-by', () => {
  it('a pointer that crosses without stopping opens nothing', async () => {
    renderReveal()
    pointerAt(EDGE_ZONE_PX - 1)
    pointerAt(400)
    await after(REST_MS * 2)
    expect(isOpen()).toBe(false)
  })

  it('a pointer that rests there opens the panel, and not before the delay', async () => {
    renderReveal()
    pointerAt(EDGE_ZONE_PX - 1)
    await after(EARLY)
    expect(isOpen()).toBe(false)
    await settles(true)
  })

  it('a pointer just outside the zone is not in it', async () => {
    renderReveal()
    pointerAt(EDGE_ZONE_PX + 1)
    await after(REST_MS * 2)
    expect(isOpen()).toBe(false)
  })

  it('while hidden the strip passes clicks straight through to what is under it', () => {
    renderReveal()
    const zone = screen.getByTestId('nav-edge-zone')
    // `pointer-events: none` IS the mechanism, and jsdom dispatches events regardless of CSS — so
    // the honest assertion is on the property that makes the click land underneath, not on a
    // synthetic click that would "pass" against a strip that really did swallow it.
    expect(zone.className).toMatch(/pointer-events-none/)
    expect(screen.getByTestId('underneath')).not.toBeNull()
  })
})

describe('it leaves on its own terms', () => {
  it('stays for the grace period after the pointer leaves, so a wobble does not slam it', async () => {
    renderReveal()
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    fireEvent.pointerLeave(screen.getByTestId('nav-floating'))
    await after(EARLY)
    expect(isOpen()).toBe(true)
    await settles(false)
  })

  it('stays indefinitely while the pointer is on the panel itself', async () => {
    renderReveal()
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    fireEvent.pointerLeave(screen.getByTestId('nav-floating'))
    await after(EARLY)
    fireEvent.pointerEnter(screen.getByTestId('nav-floating'))
    await after(GRACE_MS * 2)
    expect(isOpen()).toBe(true)
  })

  it('Escape closes it and gives focus back to where it came from', async () => {
    renderReveal()
    const underneath = screen.getByTestId('underneath')
    underneath.focus()
    act(() => { fireEvent.keyDown(document, { key: '\\', metaKey: true }) })
    expect(isOpen()).toBe(true)

    // FOCUS HAS TO ACTUALLY MOVE FIRST, or this proves nothing: with focus never leaving the
    // composer, "it came back" and "it never went" are the same DOM, and the assertion passes
    // against a build that restores nothing at all.
    act(() => { screen.getByTestId('nav-projects').focus() })
    expect(document.activeElement).not.toBe(underneath)

    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
    await settles(false)
    // A keyboard user dumped on `<body>` has lost their place entirely — which is the composer in
    // almost every real case. Awaited, because the restore deliberately waits for the panel to
    // finish leaving: handing focus back while the panel is still on screen loses it again the
    // moment the panel is removed.
    await waitFor(() => expect(document.activeElement).toBe(underneath))
  })

  it('focus landing on a navigation item holds it open — a panel that vanishes under the keyboard is a trap', async () => {
    renderReveal()
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    fireEvent.pointerLeave(screen.getByTestId('nav-floating'))
    fireEvent.focus(screen.getByTestId('nav-projects'))
    await after(GRACE_MS * 2)
    expect(isOpen()).toBe(true)
  })
})

describe('nothing reflows when it arrives', () => {
  it('the panel is fixed and outside the content, so the panes keep their widths', () => {
    renderReveal()
    const content = screen.getByTestId('content')
    const parentBefore = content.parentElement
    const classBefore = content.className
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    // jsdom measures nothing, so the claim is asserted through the MECHANISM that makes it true:
    // the panel is `fixed`, and the content it floats over is untouched — same parent, same
    // classes, nothing new in its flow. A panel that pushed the panes would have to change one of
    // those, and a measured-width assertion here would be measuring numbers jsdom invented.
    expect(screen.getByTestId('nav-floating').className).toMatch(/(^|\s)fixed(\s|$)/)
    expect(content.parentElement).toBe(parentBefore)
    expect(content.className).toBe(classBefore)
  })
})

describe('with the chat hidden the edge zone is not installed at all', () => {
  it('resting the pointer at the edge opens nothing', async () => {
    renderReveal({ chatHidden: true })
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('nav-edge-zone')).toBeNull()
    pointerAt(EDGE_ZONE_PX - 1)
    await after(REST_MS * 2)
    expect(isOpen()).toBe(false)
  })

  it('but the button and the shortcut still work — liveness on the same fixture', async () => {
    renderReveal({ chatHidden: true })
    await act(async () => { await Promise.resolve() })
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    expect(isOpen()).toBe(true)
    act(() => { fireEvent.keyDown(document, { key: '\\', metaKey: true }) })
    await settles(false)
    act(() => { fireEvent.keyDown(document, { key: '\\', metaKey: true }) })
    expect(isOpen()).toBe(true)
  })
})

describe('pin is a preference about a screen, not a property of an application', () => {
  it('docks the panel and remembers the choice', async () => {
    renderReveal()
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    fireEvent.click(screen.getByTestId('nav-pin'))
    expect(screen.getByTestId('nav-docked')).not.toBeNull()
    expect(window.localStorage.getItem('bial:nav-pinned')).toBe('1')
    // The float does not vanish, it LEAVES — the panel settles into the dock rather than being
    // cut. What matters is that it does not stay: a floating copy left open behind the docked one
    // is what drops a second panel over the application at the next unpin.
    await settles(false)
  })

  it('survives a reload, and applies to a different application', () => {
    window.localStorage.setItem('bial:nav-pinned', '1')
    render(
      <MemoryRouter initialEntries={['/projects/another-one']}>
        <NavReveal hideable>
          <Underneath />
        </NavReveal>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('nav-docked')).not.toBeNull()
  })

  it('while pinned there is no edge zone — there is nothing left to reveal', () => {
    window.localStorage.setItem('bial:nav-pinned', '1')
    renderReveal()
    expect(screen.queryByTestId('nav-edge-zone')).toBeNull()
  })

  it('★ docking the panel does not tear down and rebuild the application beside it', async () => {
    // WHAT IS UNDERNEATH IS A RUNNING APPLICATION, and part of it is an iframe. React reconciles
    // by position and element type, so returning a different tree shape for pinned than for
    // floating unmounts everything below — the iframe reloads from its src, and whatever the
    // person was looking at is gone. Pin is a preference about chrome; it may not cost a screen.
    renderReveal()
    expect(mounts).toBe(1)

    fireEvent.click(screen.getByTestId('nav-menu-button'))
    fireEvent.click(screen.getByTestId('nav-pin'))
    await settles(false)
    expect(screen.getByTestId('nav-docked')).not.toBeNull()
    expect(mounts).toBe(1)

    fireEvent.click(screen.getByTestId('nav-pin'))
    expect(screen.queryByTestId('nav-docked')).toBeNull()
    expect(mounts).toBe(1)
  })

  it('⌘\\ undocks a pinned panel rather than toggling one that is already there', () => {
    window.localStorage.setItem('bial:nav-pinned', '1')
    renderReveal()
    act(() => { fireEvent.keyDown(document, { key: '\\', metaKey: true }) })
    expect(screen.queryByTestId('nav-docked')).toBeNull()
    expect(window.localStorage.getItem('bial:nav-pinned')).toBe('0')
  })

  it('★ unpinning leaves the application alone instead of dropping a panel over it', async () => {
    renderReveal()
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    fireEvent.click(screen.getByTestId('nav-pin'))
    await settles(false)

    fireEvent.click(screen.getByTestId('nav-pin'))
    expect(screen.queryByTestId('nav-docked')).toBeNull()
    // The float is how the pin was reached in the first place. If pinning had left it open behind
    // the docked panel, THIS is where it would come back — over the application, asked for by
    // nobody, because a state nobody could see was still running.
    expect(isOpen()).toBe(false)
  })

  it('★ the menu button describes the panel a person can see, and undocks the one they have', () => {
    window.localStorage.setItem('bial:nav-pinned', '1')
    renderReveal()
    const button = screen.getByTestId('nav-menu-button')
    // Read aloud. A docked panel is on screen, so "collapsed" is not a nuance — it is false.
    expect(button.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(button)
    expect(screen.queryByTestId('nav-docked')).toBeNull()
    expect(window.localStorage.getItem('bial:nav-pinned')).toBe('0')
  })
})

describe('the brand is drawn once', () => {
  it('★ the panel header carries one wordmark, not two stacked on each other', () => {
    // `BIALLogo` already contains the wordmark, and the header set a second one beside it. At the
    // panel's 248px both wrapped, and the two collided — "Develope" drawn over "Developer", with
    // the pin glyph across the text. On every screen in the product.
    renderReveal()
    fireEvent.click(screen.getByTestId('nav-menu-button'))
    const header = screen.getByTestId('nav-panel').firstElementChild
    const drawn = (header?.textContent ?? '').match(/BIAL\s*Citizen\s*Developer/g) ?? []
    expect(drawn).toHaveLength(1)
  })
})

describe('a reach can be interrupted by the screen itself, not only by the pointer', () => {
  it('★ the chat collapsing mid-reach abandons it, rather than opening a moment later', async () => {
    const view = renderReveal()
    pointerAt(4)
    await after(EARLY)
    expect(isOpen()).toBe(false)

    // The edge zone goes away under a pointer that is still resting on it. Dropping the listener
    // stops NEW intent; the timer already armed is the one that would open a panel over a screen
    // that no longer has an edge to have reached from.
    view.rerender(
      <MemoryRouter initialEntries={['/chat/c1']}>
        <NavReveal hideable>
          <Underneath chatHidden />
        </NavReveal>
      </MemoryRouter>,
    )
    await after(REST_MS)
    expect(isOpen()).toBe(false)
  })
})
