/**
 * ProjectCard — the open/delete affordances and the no-nested-interactive invariant.
 *
 * The component is purely presentational (the page injects `onOpen`/`onDelete`), so these
 * render it with plain spies and no router. The point of the test is the *structure*: the
 * card must expose open and delete without nesting one interactive control inside another.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

/** Forces every element's clip measurement in one direction — jsdom's own scrollWidth and
 *  clientWidth are both always 0, which reads as never-clipped, so the tooltip branch
 *  needs this to be reachable at all. */
function stubClip(clipped: boolean) {
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(clipped ? 200 : 100)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(100)
}
import ProjectCard from '../ProjectCard'
import type { Project } from '../../../utils/projectApi'

const mkProject = (name: string, over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name,
  description: 'A tool',
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
  ...over,
})

afterEach(() => cleanup())

describe('ProjectCard', () => {
  it('opens via the title control, without also firing delete', () => {
    const onOpen = vi.fn()
    const onDelete = vi.fn()
    render(<ProjectCard project={mkProject('Roster')} onOpen={onOpen} onSettings={vi.fn()} onDelete={onDelete} />)

    fireEvent.click(screen.getByRole('button', { name: 'Roster' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(onDelete).not.toHaveBeenCalled()
  })

  it('deletes from the menu, without also triggering open', async () => {
    const onOpen = vi.fn()
    const onDelete = vi.fn()
    render(<ProjectCard project={mkProject('Roster')} onOpen={onOpen} onSettings={vi.fn()} onDelete={onDelete} />)

    fireEvent.pointerDown(screen.getByTestId('app-menu-tile'))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }))
    expect(onDelete).toHaveBeenCalledTimes(1)
    // The menu is a sibling of the open control, so reaching Delete never opens the project.
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('opening the menu does not trigger the tile\'s open action', () => {
    const onOpen = vi.fn()
    render(<ProjectCard project={mkProject('Roster')} onOpen={onOpen} onSettings={vi.fn()} onDelete={vi.fn()} />)
    fireEvent.pointerDown(screen.getByTestId('app-menu-tile'))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('exposes open and the menu as two separate button tab stops', () => {
    render(<ProjectCard project={mkProject('Roster')} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)
    const open = screen.getByRole('button', { name: 'Roster' })
    const menu = screen.getByTestId('app-menu-tile')
    // Both are real, natively keyboard-activatable <button>s (Enter/Space for free) — and they
    // are distinct elements, not one wrapping the other.
    expect(open.tagName).toBe('BUTTON')
    expect(menu.tagName).toBe('BUTTON')
    expect(open).not.toBe(menu)
    expect(open.contains(menu)).toBe(false)
  })

  it('does not nest the menu inside any interactive element', () => {
    const { container } = render(
      <ProjectCard project={mkProject('Roster')} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />,
    )
    const menu = screen.getByTestId('app-menu-tile')
    // Nothing in the card claims the button role beyond the two real <button>s themselves.
    expect(container.querySelector('[role="button"]')).toBeNull()
    // The menu's only <button> ancestor is itself: no interactive element wraps it, so its
    // accessible name can never be absorbed by an outer role="button".
    expect(menu.parentElement?.closest('button')).toBeNull()
  })

  it('offers the menu without a hover — a hover-only control has no keyboard or touch route', () => {
    render(<ProjectCard project={mkProject('Roster')} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)
    expect(screen.getByTestId('app-menu-tile').className).not.toMatch(/opacity-0/)
  })

  it('opens a tooltip on a really clipped name, with the full text', async () => {
    // Mutation receipt: delete the Tooltip branch from NameButton and this is the test that
    // goes red — nothing else in the suite can see it, since jsdom's scrollWidth/clientWidth
    // are both 0 and `clipped` would already read `false`.
    stubClip(true)
    render(
      <ProjectCard
        project={mkProject('A Very Long Project Name That Has To Clip In This Tile')}
        onOpen={vi.fn()}
        onSettings={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    fireEvent.focus(screen.getByRole('button', { name: 'A Very Long Project Name That Has To Clip In This Tile' }))

    expect(await screen.findByRole('tooltip')).toBeTruthy()
  })

  it('opens nothing extra when the name is NOT clipped', () => {
    stubClip(false)
    render(<ProjectCard project={mkProject('Visitor Log')} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)

    fireEvent.focus(screen.getByRole('button', { name: 'Visitor Log' }))

    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  /**
   * The description is clipped by CSS, never by JavaScript, and it advertises no interaction
   * of its own.
   *
   * The tile already had this shape; these pin it, because the obvious "fix" applied to its
   * sibling row (cut the string, show the rest on hover) would arrive here next and would take
   * the full text out of the accessible tree in the process.
   */
  const LONG =
    'A visitor log for the north gate that records arrivals, departures, badge numbers and the ' +
    'host each visitor came to see, with a weekly export for the security desk.'

  it('★ keeps the WHOLE description in the accessible tree, clipped only visually', () => {
    render(<ProjectCard project={mkProject('Roster', { description: LONG })} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)

    // Found by its complete text — a JavaScript truncation would fail this outright, and an
    // ellipsis appended in JS would fail it too.
    const description = screen.getByText(LONG)
    expect(description.textContent).toBe(LONG)
    // The clipping is the BOX, and it is CSS.
    expect(description.className).toMatch(/line-clamp-/)
  })

  it('★ advertises no interaction on the description — and the tile still opens by its name', () => {
    // Paired on purpose: `queryByRole('tooltip') === null` passes just as well on a render that
    // crashed, so the second half proves the tile is alive and the affordance it DOES have works.
    stubClip(true)
    const onOpen = vi.fn()
    render(<ProjectCard project={mkProject('Roster', { description: LONG })} onOpen={onOpen} onSettings={vi.fn()} onDelete={vi.fn()} />)

    const description = screen.getByText(LONG)
    expect(description.className).not.toMatch(/cursor-pointer/)
    expect(description.getAttribute('title')).toBeNull()
    fireEvent.focus(description)
    fireEvent.mouseOver(description)
    expect(screen.queryByRole('tooltip')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Roster' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('falls back to "Untitled application" for an empty name, in the menu\'s label too', () => {
    render(<ProjectCard project={mkProject('')} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Untitled application' })).toBeTruthy()
    // A page of tiles must not be a page of controls all called "More actions for".
    expect(screen.getByRole('button', { name: 'More actions for Untitled application' })).toBeTruthy()
  })

  it('shows both dates as the board draws them — created, then updated', () => {
    render(
      <ProjectCard
        project={{
          ...mkProject('Roster'),
          createdAt: '2026-08-28T09:00:00Z',
          updatedAt: '2026-09-15T09:00:00Z',
        }}
        onOpen={vi.fn()}
        onSettings={vi.fn()}
        onDelete={vi.fn()}
      />,
    )
    expect(screen.getByText('28 Aug → 15 Sep')).toBeTruthy()
  })
})
