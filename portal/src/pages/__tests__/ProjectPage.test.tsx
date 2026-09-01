/**
 * ProjectPage: the project screen IS the app (Plan F, U9 — the deliberate inversion).
 *
 * ═══ WHAT CHANGED HERE, AND WHY IT IS AN INVERSION RATHER THAN A DELETION ═══
 *
 * This suite used to pin the app's ABSENCE from the project screen — the Phase-1 decision that "a
 * stored app is not a running sandbox". That decision was right about what it removed: a passive
 * view of stored code, a lifecycle badge, and a reroute into a chat. What arrives now is not that.
 * It is the RUNNING sandbox, in a pane beside the rail, behind one control a person presses. So
 * the assertions split in two rather than all flipping:
 *
 *   INVERTED — the app's presence. The start control exists on a saved-not-running project and does
 *     not exist on a project with nothing built. Nothing on the screen starts a container.
 *   KEPT AS INERTNESS GUARDS — "View app", "Continue building", "Open app" and the lifecycle badge
 *     stay gone. Those were the passive-artefact affordances, and none of them is coming back.
 *
 * ═══ THE ASSERTIONS THAT WERE VACUOUS, AND WHY THEY ARE NOT THE ONES TO INVERT ═══
 *
 * `queryByTestId('live-preview')` appeared three times and was worthless in both directions: no
 * product code has ever set that testid, and `LivePreview` is additionally stubbed to `() => null`
 * in this file. Those lines could not have gone red however the page changed. They are replaced by
 * assertions on things this suite can actually observe — the start control, and the absence of any
 * server call that would start something.
 *
 * ═══ WHAT THIS SUITE CAN AND CANNOT SEE ═══
 *
 * It renders the page WITHOUT the workspace shell, so there is no pane host and no iframe in the
 * tree at all. That is deliberate: the pane's own behaviour — its identity across a navigation, the
 * stacked crossing, the framed URL — belongs to `ProjectWorkspace.test.tsx`, which renders through
 * the real shell, because a test that mounts this page alone cannot see any of it. What THIS file
 * owns is the page: its data, its beacon, its rail's contents, and the affordances that must and
 * must not be on it.
 *
 * projectApi, conversationApi and buildSessionApi are mocked at the module boundary; the REAL
 * `ProjectWorkspace`, `WorkspaceRail`, `RailComposer` and `ProjectDescriptionEditor` render. A
 * LocationProbe on a catch-all route reports where navigation actually landed. The old stored-app
 * read (`getAppSource`) is retired from appRegistryApi entirely (owner surface gone; pinned by
 * appRegistryApi.test.js).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode } from 'react'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import ProjectPage from '../ProjectPage'
import { ApiError } from '../../utils/apiError'
import { beaconsFrom } from './_observeBeacons'
import type { Project } from '../../utils/projectApi'

const h = vi.hoisted(() => ({
  authFetch: vi.fn(),
  getProject: vi.fn(),
  patchProject: vi.fn(),
  generateDescription: vi.fn(),
  listProjectConversations: vi.fn(),
  deleteConversation: vi.fn(),
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
  relaunchPreview: vi.fn(),
}))

// The REAL `observe` module runs in these tests — its once-per-project-id-per-page-load guard IS
// the thing under test, and a mocked module would prove only that a function was called. Only the
// transport is replaced. Every test below therefore uses its OWN project id: module state is
// per page load, and a shared id would make one test's mark silence the next one's.
vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  authFetch: h.authFetch,
}))
vi.mock('../../utils/projectApi', () => ({
  getProject: h.getProject,
  patchProject: h.patchProject,
  generateDescription: h.generateDescription,
}))
vi.mock('../../utils/conversationApi.js', () => ({
  listProjectConversations: h.listProjectConversations,
  deleteConversation: h.deleteConversation,
}))
// The publish chip owns its own read and its own suite; stubbed to a marker so these tests
// stay about the page while still being able to assert WHERE it is mounted. The stub is
// what makes the header-position cases below meaningful — a null stub would let the chip
// vanish from either header branch without a single assertion noticing.
vi.mock('../../components/PublishStatusChip', () => ({
  default: ({ projectId }: { projectId: string }) => (
    <span data-testid="publish-chip-stub" data-project={projectId} />
  ),
}))
// THE WORKSPACE READ, stubbed at the module boundary. `ProjectWorkspace` polls `fetchPreviewState`
// on a cadence; every scenario below sets its own answer through `h.fetchPreviewState`. The two
// container-exec reads are stubbed as REJECTING rather than resolving, so a regression that starts
// calling them on a stopped project fails loudly here instead of quietly costing an attach.
vi.mock('../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/buildSessionApi')>()),
  fetchPreviewState: h.fetchPreviewState,
  fetchSaveState: h.fetchSaveState,
  fetchCompileState: vi.fn(async () => { throw new Error('a container exec on the project screen') }),
  checkWorkspace: vi.fn(async () => { throw new Error('a container exec on the project screen') }),
  relaunchPreview: h.relaunchPreview,
}))
vi.mock('../../utils/chatHistory.js', () => ({ relativeTime: () => '1h ago' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// ProjectPage no longer mounts LivePreview (the passive View-app preview is hidden, U6).
// A null stub keeps the `queryByTestId('live-preview')` inertness assertions meaningful.
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
// THE BADGE'S WORDS COME FROM THE SERVER NOW (U16): `chatKindFor` reads the kind catalogue off
// the cached bootstrap profile, so a suite that does not stand one up gets the honest fallback
// ("Chat") on every row and every badge assertion below fails for a reason that has nothing to
// do with this page. The words are DELIBERATELY the product ones here — these tests assert what
// a citizen reads on the row, and `chatKind.test.ts` is the suite that proves the sourcing is
// dynamic by mocking words the product does not use.
vi.mock('../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

const makeProject = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'VIP Movement',
  description: 'A tracked movement.',
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  ...over,
})

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{loc.pathname + loc.search}</div>
}

function renderProjectPage(projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={[`/projects/${projectId}`]}>
      <Routes>
        <Route path="/projects/:projectId" element={<ProjectPage />} />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** A preview-state read, in the shape the wire parser produces one. */
const preview = (over: Record<string, unknown> = {}) => ({
  state: 'never_built',
  alive: false,
  previewUrl: null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable: null,
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  h.authFetch.mockResolvedValue({ ok: true } as Response)
  h.listProjectConversations.mockResolvedValue([])
  h.fetchPreviewState.mockResolvedValue(preview())
  h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: false, containerHead: null, savedHead: null })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/** The observation bodies this render posted — see `_observeBeacons` for the mock contract. */
const beacons = () => beaconsFrom(h.authFetch)

describe('ProjectPage — the composer is unconditional', () => {
  it('no-app project: renders the composer, the description block and the recents — and no app affordances', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: null, appStatus: null }))
    renderProjectPage()

    expect(await screen.findByRole('heading', { name: 'VIP Movement' })).toBeTruthy()
    // The composer is present whether or not the project has an app — the exact regression that
    // caused the reverted app-first fold. (F6: no idea-starter cards inside a dedicated project.)
    expect(screen.getByPlaceholderText(/Describe the app you want built/i)).toBeTruthy()
    // The description block — a read view with an Edit button (U7: the pop-up editor).
    expect(within(screen.getByTestId('description-rail')).getByRole('button', { name: /edit/i })).toBeTruthy()
    // The passive-artefact affordances stay gone.
    expect(screen.queryByRole('button', { name: /view app/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
  })

  it('built project: the composer is still on top, and the retired affordances are still retired', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', appStatus: 'draft' }))
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'build', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    // NOT collapsed under an app card — this is the reverted regression the fold must avoid.
    expect(screen.getByPlaceholderText(/Describe the app you want built/i)).toBeTruthy()
    // INERTNESS GUARDS, kept through the inversion rather than deleted with it. What Phase-1
    // removed was a passive view of stored code, a lifecycle badge and a reroute; none of the
    // three comes back with the running sandbox.
    expect(screen.queryByRole('button', { name: /view app/i })).toBeNull()
    expect(screen.queryByText('draft')).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /open app/i })).toBeNull()
  })
})

describe('ProjectPage — the app arrives behind one deliberate press (R3, AE1, AE2)', () => {
  it('AE1: a saved, not-running project offers exactly one start control, and says what IS', async () => {
    // THE INVERSION. This is the assertion the old suite could not have: it used to pin that the
    // project screen offered no way to reach the app at all.
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', hasRelaunchableSnapshot: true }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    // R-16's exact sentence, and no negation of it anywhere on the screen.
    await waitFor(() => expect(screen.getAllByText('Your app is saved.').length).toBeGreaterThan(0))
    expect(document.body.textContent).not.toMatch(/not running/i)
    expect(document.body.textContent).not.toMatch(/\bstopped\b/i)
  })

  it('AE2: a project with nothing built offers no start control at all', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: null, hasRelaunchableSnapshot: false }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'never_built', restorable: false }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /launch application/i })).toBeNull()
  })

  it('★ opening the screen STARTS NOTHING — the read is the only call it makes (R3)', async () => {
    // The whole basis on which the Phase-1 removal is reversed. A screen that started a container
    // by being opened would be the thing that decision was right to refuse.
    //
    // Mutation receipt: make `ProjectWorkspace` call `relaunchPreview` on mount and this goes red.
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', hasRelaunchableSnapshot: true }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalledWith('p1'))
    expect(h.relaunchPreview).not.toHaveBeenCalled()
  })

  it('★ never asks a stopped project whether it has unsaved work (R3)', async () => {
    // `fetchSaveState` runs two `git` executions inside the container. On a stopped workspace that
    // is an attach the screen caused, so the rail shows the status sentence and no save state.
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', hasRelaunchableSnapshot: true }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())
    expect(h.fetchSaveState).not.toHaveBeenCalled()
    expect(screen.queryByTestId('rail-save-state')).toBeNull()
  })

  it('shows the save half only once the workspace is alive (R6, delivered in the running state)', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1', hasRelaunchableSnapshot: true }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'alive', alive: true, previewUrl: 'https://app.example/' }))
    h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: true, containerHead: 'deadbeefcafe', savedHead: 'abc1234def' })
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    const saved = await screen.findByTestId('rail-save-state')
    expect(saved.textContent).toMatch(/not saved yet/i)
    // The short commit, not the full sha.
    expect(saved.textContent).toContain('abc1234')
    expect(saved.textContent).not.toContain('abc1234def')
  })

  it('R62: an unreadable save state says so rather than reporting that everything is saved', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1' }))
    h.fetchPreviewState.mockResolvedValue(preview({ state: 'alive', alive: true, previewUrl: 'https://app.example/' }))
    h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: null, containerHead: null, savedHead: null })
    renderProjectPage()

    const saved = await screen.findByTestId('rail-save-state')
    expect(saved.textContent).toMatch(/could not check/i)
    expect(saved.textContent).not.toMatch(/everything is saved/i)
  })

  it('the removed doors stay gone: no "Open app" link and no "Continue building" anywhere', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'app-123', appStatus: 'approved' }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    expect(screen.queryByRole('link', { name: /open app/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /continue building/i })).toBeNull()
  })
})

describe('ProjectPage — the description rail (R3, U7 pop-up editor)', () => {
  it('shows an Edit button and NO attach / file-input control, with no dialog open by default', async () => {
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    const rail = screen.getByTestId('description-rail')
    expect(within(rail).getByRole('button', { name: /edit/i })).toBeTruthy()
    expect(within(rail).queryByRole('button', { name: /attach|upload/i })).toBeNull()
    expect(rail.querySelector('input[type="file"]')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('clicking Edit opens a pop-up exposing Save and Generate', async () => {
    h.getProject.mockResolvedValue(makeProject())
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    const rail = screen.getByTestId('description-rail')
    fireEvent.click(within(rail).getByRole('button', { name: /edit/i }))

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('button', { name: /^save$/i })).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: /^generate$/i })).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: /^cancel$/i })).toBeTruthy()
  })
})

describe('ProjectPage — publishing is one chip beside the name (R37)', () => {
  it('puts the chip beside the project name and nothing about publishing in the rail', async () => {
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1' }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    expect(screen.getByTestId('publish-chip-stub').getAttribute('data-project')).toBe('p1')

    // Scoped to the rail's own test id, so the absence is about the rail rather than
    // about the whole page — the chip itself is a publish affordance and it is elsewhere.
    const rail = screen.getByTestId('description-rail')
    expect(within(rail).queryByTestId('publish-chip-stub')).toBeNull()
    expect(within(rail).getByRole('button', { name: /edit/i })).toBeTruthy()
    expect(rail.textContent).not.toMatch(/publish/i)
    expect(rail.textContent).not.toMatch(/review/i)
  })

  it('shows the chip for a project with nothing built, rather than showing nothing', async () => {
    // "Nothing built yet" is a state the chip NAMES. A citizen should learn it from the
    // same place they learn everything else about their app, not from an absence — which
    // is why the mount is not gated on `appId` the way the two retired cards were.
    // Mutation receipt: put `project.appId &&` back in front of the chip and this goes red.
    h.getProject.mockResolvedValue(makeProject({ appId: null }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    expect(screen.getByTestId('publish-chip-stub')).toBeTruthy()
  })

  it('does not move the chip, or unmount it, when an inline rename starts', async () => {
    // Both header branches are the same `flex items-center gap-2` row with the chip as the
    // second child, so the rename swap does not shift it. `getByTestId` throws on a
    // duplicate, which is what rules out the other failure — a second mount in the rename
    // branch rather than a moved one.
    h.getProject.mockResolvedValue(makeProject({ appId: 'a1' }))
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    const before = screen.getByTestId('publish-chip-stub')
    const nameBefore = before.previousElementSibling?.tagName

    fireEvent.click(screen.getByRole('button', { name: /rename project/i }))

    const after = screen.getByTestId('publish-chip-stub')
    expect(after).toBeTruthy()
    expect(screen.getByRole('textbox', { name: /project name/i })).toBeTruthy()
    // Still the second child of the row, with the name element before it and the action
    // buttons after it — in both branches.
    expect(nameBefore).toBe('H1')
    expect(after.previousElementSibling?.tagName).toBe('INPUT')
    expect(after.nextElementSibling?.tagName).toBe('BUTTON')
  })
})

describe('ProjectPage — an outlet child that owns its own scroller (Plan A, U3)', () => {
  // THE RISK THIS GUARDS. This surface used to be a `min-h-screen` document scroller with a page
  // frame and a navbar of its own. Inside the workspace shell it is a flex child of a full-height
  // frame that does not scroll — so if it does not declare a scroller, a project with twenty
  // conversations clips its list with no way to reach the bottom, and if the rail keeps its old
  // sticky offset it hangs 80px below a navbar that is no longer inside anything it can see.
  //
  // jsdom does no layout, so what is assertable is the model rather than the pixels: the classes
  // that encode it, and the absence of the frame this surface must no longer bring.
  it('declares its own scroller and brings no page frame of its own', async () => {
    h.getProject.mockResolvedValue(makeProject())
    const { container } = renderProjectPage()
    await screen.findByRole('heading', { name: 'VIP Movement' })

    const main = container.querySelector('main') as HTMLElement
    expect(main).toBeTruthy()
    expect(main.className).toMatch(/overflow-y-auto/)
    // `min-h-0` is what actually lets a flex child scroll: without it the child's min-content
    // height wins and the overflow never has anywhere to happen.
    expect(main.className).toMatch(/min-h-0/)
    // No second full-height frame inside the shell's own.
    expect(container.innerHTML).not.toMatch(/min-h-screen/)
    expect(container.innerHTML).not.toMatch(/100vh/)
  })

  it('★ builds NO second two-column frame of its own', async () => {
    // REPLACES the sticky-offset assertion, which no longer describes anything: the description was
    // a right-hand sticky `aside` beside a two-column grid inside the page, and the rail IS the
    // left column now — there is no second grid for it to stick inside. What replaced that risk is
    // this one, and it is bigger: an implementer who rebuilds rail-plus-pane in here produces a
    // grid nested in the shell's own, and every "the app did not remount" assertion in this plan
    // fails on the first navigation to a chat.
    h.getProject.mockResolvedValue(makeProject())
    const { container } = renderProjectPage()
    await screen.findByRole('heading', { name: 'VIP Movement' })

    // No grid template, and no second iframe host — the pane is the SHELL's sibling of the Outlet.
    expect(container.innerHTML).not.toMatch(/grid-cols-/)
    expect(container.querySelector('iframe')).toBeNull()
  })
})

/** The row container a chat title sits in — the kind badge is its SIBLING (F-10), so this is
 *  what makes "the right badge on the right row" assertable instead of merely "a badge exists". */
function rowFor(title: string): HTMLElement {
  const row = screen.getByRole('button', { name: title }).closest('div.group')
  expect(row, `no conversation row for "${title}"`).toBeTruthy()
  return row as HTMLElement
}

/**
 * KEPT ENTIRELY, AND THE REASON IS RECORDED RATHER THAN IMPLIED. This block was going to move into
 * a chat-history rail; history was withheld by a client call before it was built. So the recents
 * section stays exactly where it is — it is the only project-scoped way to delete a conversation
 * and the only route back to an EXISTING chat, and deleting it with nothing to replace it would
 * retire both silently. Nothing here is inverted and nothing moves.
 */
describe('ProjectPage — conversations as a plain recents list (U3)', () => {
  it('renders icon · title · kind badge · relative date · ⋮ with no new-chat buttons', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c1', kind: 'plan', projectId: 'p1', title: 'Scope the fields', updatedAt: '2026-07-10T00:00:00Z' },
      { id: 'c2', kind: 'build', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    expect(await screen.findByText('Scope the fields')).toBeTruthy()
    expect(h.listProjectConversations).toHaveBeenCalledWith('p1')
    const list = screen.getByTestId('conversations')
    // Liveness first: the rest of the row is still there, so nothing below can pass on a
    // half-rendered list.
    expect(within(list).getAllByText('1h ago').length).toBe(2)
    expect(within(list).getByRole('button', { name: /actions for build the screen/i })).toBeTruthy()
    // R16 — each row SAYS which kind it is, on the row that is that kind.
    expect(within(rowFor('Build the screen')).getByText('Build')).toBeTruthy()
    expect(within(rowFor('Scope the fields')).getByText('Plan')).toBeTruthy()
    // …and still no New-chat buttons anywhere: the other half of the old assertion.
    expect(screen.queryByRole('button', { name: /new build chat/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /new plan chat/i })).toBeNull()
  })

  it('the badge reads as the full phrase, not a bare noun', async () => {
    // The visible word is half of it: the badge's COMPLETE text is what a screen reader says,
    // and "Build" alone is not a sentence about anything. A badge with no visually-hidden
    // completion is the mutant this catches.
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c1', kind: 'plan', projectId: 'p1', title: 'Scope the fields', updatedAt: '2026-07-10T00:00:00Z' },
      { id: 'c2', kind: 'build', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    await screen.findByText('Scope the fields')
    expect(within(rowFor('Build the screen')).getByText('Build').textContent).toBe('Build chat')
    expect(within(rowFor('Scope the fields')).getByText('Plan').textContent).toBe('Plan chat')
  })

  it('a kind this vocabulary does not have gets the fallback word, not the Plan word', async () => {
    // ASM5, the live defect the badge exposes. `ConversationKind` has THREE values and the old
    // `kind === 'builder'` test put `assistant` in the Plan arm — invisible behind an icon,
    // a lie behind a word.
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c3', kind: 'assistant', projectId: 'p1', title: 'Ask about gates', updatedAt: '2026-07-11T00:00:00Z' }, // a retired value, still on old rows
    ])
    renderProjectPage()

    await screen.findByText('Ask about gates')
    const row = rowFor('Ask about gates')
    expect(within(row).getByText('Chat')).toBeTruthy()
    expect(within(row).queryByText('Plan')).toBeNull()
    expect(within(row).queryByText('Build')).toBeNull()
  })

  it('a malformed row (kind coerced to "") still renders, with the fallback word', async () => {
    // `narrowChat` legitimately produces `kind: ''` for a row the API sent malformed. The
    // lookup must answer it rather than throw or invent a kind.
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c4', kind: 42, projectId: 'p1', title: 'Malformed row', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    await screen.findByText('Malformed row')
    expect(within(rowFor('Malformed row')).getByText('Chat')).toBeTruthy()
  })

  it('clicking a row title navigates to /chat/{id}', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'build', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Build the screen' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat/c2'))
  })

  it('the ⋮ menu exposes Open and Delete; Delete removes the row optimistically', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'build', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    h.deleteConversation.mockResolvedValue(true)
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: /actions for build the screen/i }))
    expect(screen.getByRole('button', { name: 'Open' })).toBeTruthy()
    const del = screen.getByRole('button', { name: 'Delete' })

    fireEvent.click(del)
    await waitFor(() => expect(screen.queryByText('Build the screen')).toBeNull())
    expect(h.deleteConversation).toHaveBeenCalledWith('c2')
  })

  it('the ⋮ button is a SIBLING of the title button, not an interactive descendant (F-10)', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([
      { id: 'c2', kind: 'build', projectId: 'p1', title: 'Build the screen', updatedAt: '2026-07-11T00:00:00Z' },
    ])
    renderProjectPage()

    const title = await screen.findByRole('button', { name: 'Build the screen' })
    // No interactive element nests inside the title button.
    expect(title.querySelector('button')).toBeNull()
    // The actions button exists elsewhere in the row, not under the title button.
    const actions = screen.getByRole('button', { name: /actions for build the screen/i })
    expect(title.contains(actions)).toBe(false)
  })

  it('empty state shows the exact copy', async () => {
    h.getProject.mockResolvedValue(makeProject())
    h.listProjectConversations.mockResolvedValue([])
    renderProjectPage()

    await screen.findByRole('heading', { name: 'VIP Movement' })
    expect(screen.getByText('No conversations yet — start a build or plan above.')).toBeTruthy()
  })
})

describe('ProjectPage — identity + guard rails carried over', () => {
  it('blocks renaming to "" and to "   " before any request fires', async () => {
    h.getProject.mockResolvedValue(makeProject({ name: 'VIP Movement' }))
    renderProjectPage()

    fireEvent.click(await screen.findByRole('button', { name: /rename project/i }))
    const input = screen.getByRole('textbox', { name: /project name/i })

    fireEvent.change(input, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /save name/i }))
    expect(h.patchProject).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: /save name/i }))
    expect(h.patchProject).not.toHaveBeenCalled()

    expect(screen.getByRole('alert').textContent).toMatch(/cannot be empty/i)
  })

  it('redirects to /projects when the project 404s (deleted elsewhere)', async () => {
    h.getProject.mockRejectedValue(new ApiError('Project not found.', 404))
    renderProjectPage()

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects'))
  })
})

describe('ProjectPage — the project-open mark (U4; R104, R105)', () => {
  /** The page under React's development double-mount, which is how it actually runs in dev. */
  function renderTwiceOver(projectId: string) {
    return render(
      <StrictMode>
        <MemoryRouter initialEntries={[`/projects/${projectId}`]}>
          <Routes>
            <Route path="/projects/:projectId" element={<ProjectPage />} />
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
  }

  it('marks the project open ONCE under StrictMode’s double mount', async () => {
    // R105's denominator, in the mode the app actually runs in during development.
    //
    // TWO THINGS PROTECT THIS AND THEY ARE NOT THE SAME THING, which is worth saying so nobody
    // reads a green here as proof of the guard: the load effect's own `active` flag already
    // drops the first invocation's continuation, so this passes with the module guard removed.
    // What it pins is the OUTCOME, and the guard itself is pinned by the next test and by
    // `observe.test.ts` — where removing it goes red.
    h.getProject.mockResolvedValue(makeProject({ id: 'p-strict', appId: 'a1' }))
    renderTwiceOver('p-strict')

    await screen.findByRole('heading', { name: 'VIP Movement' })
    await waitFor(() => expect(beacons()).toEqual([{ name: 'project_opened' }]))
  })

  it('★ counts ONE visit when the citizen comes back to the same project in one page load', async () => {
    // The case the `active` flag above does NOT cover, and the reason the guard lives in the
    // module rather than in the page: a real second mount, with its own effect that runs to
    // completion. "A visit" is one project id per page LOAD — a citizen who opens a project,
    // goes to their list, and comes back has visited once.
    //
    // Mutation check: remove the once-per-project-id guard and this goes red.
    h.getProject.mockResolvedValue(makeProject({ id: 'p-return', appId: 'a1' }))
    renderProjectPage('p-return')
    await screen.findByRole('heading', { name: 'VIP Movement' })
    await waitFor(() => expect(beacons()).toEqual([{ name: 'project_opened' }]))

    cleanup()
    renderProjectPage('p-return')
    await screen.findByRole('heading', { name: 'VIP Movement' })

    expect(beacons()).toEqual([{ name: 'project_opened' }])
  })

  it('★ starts no first-view clock for a project with nothing built', async () => {
    // The project is still OPENED — it belongs in R105's denominator — but it has no app to
    // first-see, so a later reveal must record nothing. Emitting for it would make this number
    // and the sandbox-first number answer different questions.
    h.getProject.mockResolvedValue(makeProject({ id: 'p-noapp', appId: null }))
    renderProjectPage('p-noapp')

    await screen.findByRole('heading', { name: 'VIP Movement' })
    await waitFor(() => expect(beacons()).toEqual([{ name: 'project_opened' }]))

    const { markAppVisible } = await import('../../utils/observe')
    markAppVisible('p-noapp')
    expect(beacons()).toEqual([{ name: 'project_opened' }])
  })

  it('marks nothing at all when the project cannot be loaded', async () => {
    // A visit that never resolved a project is not a visit to one.
    h.getProject.mockRejectedValue(new ApiError('boom', 500))
    renderProjectPage('p-broken')

    await screen.findByText(/couldn.t load this project/i)
    expect(beacons()).toEqual([])
  })

  it('★ fires the beacon from exactly ONE production call site (the double-fire guard)', async () => {
    // A STRUCTURAL assertion, and it catches what the four behavioural ones above cannot. The
    // realistic way this breaks is not defeating `observe.ts`'s per-project guard — that guard
    // makes a repeated call a safe no-op — but BYPASSING it: a second tracker added inside
    // `ProjectWorkspace.tsx`, which independently needs `project.appId` for the rail's status line
    // and is therefore exactly where somebody would put one. A second mechanism is not covered by
    // the guard, and nothing in the UI reflects the number, so it would be wrong in silence.
    const files = import.meta.glob('../../{pages,components}/**/*.{ts,tsx}', {
      query: '?raw',
      import: 'default',
      eager: true,
    })
    const callers = Object.entries(files as Record<string, string>)
      // Vite normalises a glob key relative to THIS file, so a sibling suite comes back as
      // `./ChatRoute.test.tsx` with no `__tests__` left in it — filter on the suffix, not the dir.
      .filter(([path]) => !/\.test\.tsx?$/.test(path) && !path.includes('__tests__'))
      .filter(([, source]) => /markProjectOpened\s*\(/.test(source))
      .map(([path]) => path.replace(/^.*\/src\//, '').replace(/^\.\.\/\.\.\//, ''))
      .sort()

    expect(callers).toEqual(['../ProjectPage.tsx'])
  })
})
