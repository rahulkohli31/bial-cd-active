/**
 * The `⋯` menu on an owned application, driven through the row and the tile that mount it.
 *
 * Live means the row's `isServing`. The address is read from the deployment each time the menu
 * opens, and until a usable one arrives both address entries are disabled. Every test that picks
 * an entry also checks the row's own open handler stayed silent: the name is the way into the
 * workspace, and the menu must never become a second one.
 */
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import type { DeploymentView } from '../../../utils/deployApi'
import type { Project } from '../../../utils/projectApi'

const h = vi.hoisted(() => ({ getDeployment: vi.fn() }))
vi.mock('../../../utils/deployApi', () => ({ getDeployment: h.getDeployment }))

import ProjectRow from '../ProjectRow'
import ProjectCard from '../ProjectCard'

const LIVE_URL = 'https://app-visitor-log.example.azurecontainerapps.io/'

const project = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'Visitor Log',
  description: 'A short description',
  appId: 'a1',
  appStatus: 'draft',
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  isServing: true,
  isPublishing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
  ...over,
})

const deployment = (liveUrl: string | null): DeploymentView => ({
  deploymentId: 'd1',
  appId: 'a1',
  status: 'succeeded',
  step: null,
  url: liveUrl,
  headSha: null,
  failureCode: null,
  failureDetail: null,
  startedAt: null,
  finishedAt: null,
  unpublishedAt: null,
  liveUrl,
  approval: null,
  publishState: 'live_current',
  approvedRetryCommit: null,
  savedHead: null,
  savedAt: null,
  savedState: 'saved',
})

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => {}
  const promise = new Promise<T>((r) => (resolve = r))
  return { promise, resolve }
}

type Where = 'row' | 'tile'

function mount(where: Where, over: Partial<Project> = {}) {
  const onOpen = vi.fn()
  const onSettings = vi.fn()
  const p = project(over)
  render(
    where === 'row' ? (
      <ProjectRow project={p} onOpen={onOpen} onSettings={onSettings} />
    ) : (
      <ProjectCard project={p} onOpen={onOpen} onSettings={onSettings} />
    ),
  )
  return { onOpen, onSettings, trigger: screen.getByTestId(`app-menu-${where}`) }
}

/** Radix opens on POINTERDOWN, and arms its dismiss listeners a macrotask later. */
async function openMenu(trigger: HTMLElement): Promise<HTMLElement> {
  fireEvent.pointerDown(trigger)
  const menu = await screen.findByRole('menu')
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
  return menu
}

async function closeMenu(): Promise<void> {
  fireEvent.keyDown(document.body, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
}

const entries = (): (string | null)[] =>
  screen.getAllByRole('menuitem').map((item) => item.textContent)

const openApplication = (): Promise<HTMLElement> =>
  screen.findByRole('menuitem', { name: 'Open Application' })

const writeText = (): Mock => navigator.clipboard.writeText as Mock

// jsdom would try to follow the link; the address it points at is what is under test.
const keepJsdomHere = (event: MouseEvent): void => {
  if (event.target instanceof Element && event.target.closest('a') !== null) event.preventDefault()
}
beforeEach(() => {
  h.getDeployment.mockReset()
  document.addEventListener('click', keepJsdomHere)
})
afterEach(() => {
  document.removeEventListener('click', keepJsdomHere)
  cleanup()
})

describe.each<Where>(['row', 'tile'])('a live application, in the %s', (where) => {
  beforeEach(() => h.getDeployment.mockResolvedValue(deployment(LIVE_URL)))

  it('offers Open Application and Copy Production URL once the address arrives, then Settings…', async () => {
    const { trigger } = mount(where)
    await openMenu(trigger)
    await openApplication()

    expect(entries()).toEqual(['Open Application', 'Copy Production URL', 'Settings…'])
    const separator = screen.getByRole('separator')
    expect(separator.previousElementSibling?.textContent).toBe('Copy Production URL')
    expect(separator.nextElementSibling?.textContent).toBe('Settings…')
    expect(h.getDeployment).toHaveBeenCalledTimes(1)
    expect(h.getDeployment).toHaveBeenCalledWith('p1')
  })

  it('opens exactly the live address in a new tab with no opener, never the workspace', async () => {
    const { trigger, onOpen } = mount(where)
    await openMenu(trigger)
    const link = await openApplication()

    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe(LIVE_URL)
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')?.split(' ')).toEqual(
      expect.arrayContaining(['noopener', 'noreferrer']),
    )

    fireEvent.click(link)
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('copies exactly the live address and says so', async () => {
    const { trigger, onOpen } = mount(where)
    await openMenu(trigger)
    await openApplication()

    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy Production URL' }))

    await waitFor(() =>
      expect(screen.getByTestId(`app-menu-${where}-copy-outcome`).textContent).toBe(
        'Production URL copied',
      ),
    )
    expect(writeText()).toHaveBeenCalledTimes(1)
    expect(writeText()).toHaveBeenCalledWith(LIVE_URL)
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('reports a refused copy, with the address to copy by hand, and throws nothing', async () => {
    writeText().mockRejectedValueOnce(new DOMException('denied', 'NotAllowedError'))
    const { trigger } = mount(where)
    await openMenu(trigger)
    await openApplication()

    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy Production URL' }))

    const outcome = screen.getByTestId(`app-menu-${where}-copy-outcome`)
    await waitFor(() => expect(outcome.textContent).toContain('Could not copy the address'))
    expect(outcome.textContent).toContain(LIVE_URL)
    expect(outcome.textContent).not.toContain('Production URL copied')
  })

  it('never fires the open handler, whichever entry is chosen', async () => {
    const { trigger, onOpen, onSettings } = mount(where)
    for (const name of ['Open Application', 'Copy Production URL', 'Settings…']) {
      await openMenu(trigger)
      await openApplication()
      fireEvent.click(screen.getByRole('menuitem', { name }))
      if (screen.queryByRole('menu') !== null) await closeMenu()
    }

    expect(onSettings).toHaveBeenCalledTimes(1)
    expect(writeText()).toHaveBeenCalledTimes(1)
    expect(onOpen).not.toHaveBeenCalled()
  })
})

describe.each<Where>(['row', 'tile'])('an application that is not live, in the %s', (where) => {
  it.each<[string, Partial<Project>]>([
    ['a draft', { appStatus: 'draft' }],
    ['in review', { appStatus: 'pending' }],
    ['never built', { appId: null, appStatus: null }],
    ['taken down', { appStatus: 'approved' }],
  ])('%s: Open Application is disabled, Copy is absent, Settings… works', async (_state, over) => {
    const { trigger, onOpen, onSettings } = mount(where, { ...over, isServing: false })
    const menu = await openMenu(trigger)

    const open = screen.getByRole('menuitem', { name: 'Open Application Not live yet' })
    expect(open.getAttribute('aria-disabled')).toBe('true')
    expect(screen.queryByRole('menuitem', { name: /Copy Production URL/ })).toBeNull()
    expect(menu.querySelector('a')).toBeNull()
    expect(h.getDeployment).not.toHaveBeenCalled()

    fireEvent.click(open)
    fireEvent.keyDown(open, { key: 'Enter' })
    fireEvent.keyDown(open, { key: ' ' })
    // A selected entry closes the menu, so its still being open is the proof nothing activated.
    expect(screen.getByRole('menu')).toBe(menu)
    expect(onOpen).not.toHaveBeenCalled()
    expect(onSettings).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('menuitem', { name: 'Settings…' }))
    expect(onSettings).toHaveBeenCalledTimes(1)
    expect(onOpen).not.toHaveBeenCalled()
  })
})

describe('a live application whose address is not usable', () => {
  it.each<[string, () => Promise<DeploymentView>]>([
    ['the read fails', () => Promise.reject(new Error('Failed to read the deployment'))],
    ['there is no address', () => Promise.resolve(deployment(null))],
    ['the address is not http(s)', () => Promise.resolve(deployment('javascript:alert(1)'))],
    ['the address is relative', () => Promise.resolve(deployment('/apps/visitor-log'))],
  ])('%s: both entries say "Address unavailable", and reopening retries', async (_case, read) => {
    h.getDeployment.mockImplementationOnce(read)
    const { trigger, onSettings } = mount('row')
    await openMenu(trigger)
    await waitFor(() => expect(h.getDeployment).toHaveBeenCalledTimes(1))
    await act(async () => {})

    for (const name of ['Open Application', 'Copy Production URL']) {
      const item = screen.getByRole('menuitem', { name: `${name} Address unavailable` })
      expect(item.getAttribute('aria-disabled')).toBe('true')
    }
    expect(screen.getByRole('menu').querySelector('a')).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy Production URL Address unavailable' }))
    expect(writeText()).not.toHaveBeenCalled()

    await closeMenu()
    h.getDeployment.mockResolvedValueOnce(deployment(LIVE_URL))
    await openMenu(trigger)

    expect((await openApplication()).getAttribute('href')).toBe(LIVE_URL)
    expect(screen.getByRole('menuitem', { name: 'Copy Production URL' })).toBeTruthy()
    expect(h.getDeployment).toHaveBeenCalledTimes(2)
    expect(onSettings).not.toHaveBeenCalled()
  })

  it('says "Address unavailable" while the read is still out', async () => {
    h.getDeployment.mockReturnValueOnce(new Promise(() => {}))
    const { trigger } = mount('tile')
    await openMenu(trigger)

    expect(
      screen.getByRole('menuitem', { name: 'Open Application Address unavailable' }).getAttribute(
        'aria-disabled',
      ),
    ).toBe('true')
    expect(screen.getByRole('menu').querySelector('a')).toBeNull()
  })

  it('ignores an answer from an earlier open, and keeps nothing between opens', async () => {
    const first = deferred<DeploymentView>()
    const second = deferred<DeploymentView>()
    h.getDeployment.mockResolvedValueOnce(deployment(LIVE_URL))
    const { trigger } = mount('row')

    await openMenu(trigger)
    expect((await openApplication()).getAttribute('href')).toBe(LIVE_URL)
    await closeMenu()

    h.getDeployment.mockReturnValueOnce(first.promise)
    await openMenu(trigger)
    // The last open's address is not reused while this one's read is out.
    expect(screen.getByRole('menuitem', { name: 'Open Application Address unavailable' })).toBeTruthy()
    await closeMenu()

    h.getDeployment.mockReturnValueOnce(second.promise)
    await openMenu(trigger)
    await act(async () => second.resolve(deployment(LIVE_URL)))
    await act(async () => first.resolve(deployment('https://stale.example.azurecontainerapps.io/')))

    expect((await openApplication()).getAttribute('href')).toBe(LIVE_URL)
    expect(h.getDeployment).toHaveBeenCalledTimes(3)
  })
})

it('reads the deployment for the row whose menu was opened, and only that one', async () => {
  h.getDeployment.mockResolvedValue(deployment(LIVE_URL))
  render(
    <>
      <ProjectRow project={project({ id: 'p1', name: 'Visitor Log' })} onOpen={vi.fn()} onSettings={vi.fn()} />
      <ProjectRow project={project({ id: 'p2', name: 'Gate Roster' })} onOpen={vi.fn()} onSettings={vi.fn()} />
    </>,
  )

  await openMenu(screen.getByRole('button', { name: 'More actions for Gate Roster' }))
  await openApplication()

  expect(h.getDeployment).toHaveBeenCalledTimes(1)
  expect(h.getDeployment).toHaveBeenCalledWith('p2')
})
