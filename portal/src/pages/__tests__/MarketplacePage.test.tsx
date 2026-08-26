/**
 * MarketplacePage — the behaviours a reader would most easily get wrong (#145).
 *
 * The theme running through these: **anything that changes the result SET resets to page 1**.
 * A new query, a new page size, a new sort. Miss one and the user lands on page 4 of a
 * three-page result and sees nothing, with no clue why — a bug that only shows up once
 * someone has paged deep enough to hit it.
 *
 * The empty state is also decided from the query that produced the CURRENT items, never the
 * input value, which runs ahead by the debounce window.
 *
 * Both dropdowns are Radix `<Select>`s, not native `<select>`s (#147 review: a native
 * option list is drawn by the OS and cannot be branded). That changes how tests drive them:
 * `fireEvent.change` on a `role="combobox"` button does not throw, it silently no-ops — so
 * a stale interaction here surfaces as a `waitFor` timeout rather than an obvious error.
 * Use `pickSelect`.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import MarketplacePage from '../MarketplacePage'
import type { MarketplacePage as Page } from '../../utils/marketplaceApi'

const h = vi.hoisted(() => ({ listMarketplace: vi.fn() }))
vi.mock('../../utils/marketplaceApi', async (importOriginal) => {
  // Only the network call is faked. `PAGE_SIZES`/`DEFAULT_PAGE_SIZE` are real constants the
  // component renders from, so stubbing the whole module would silently empty the
  // rows-per-page list and make its assertions meaningless.
  const actual = await importOriginal<typeof import('../../utils/marketplaceApi')>()
  return { ...actual, ...h }
})
// Stubbed like every other page test here: the chrome is not what this file is about, and
// the real one pulls in auth + router state the assertions do not touch.
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))

const entry = (over: Partial<Page['items'][number]> = {}) => ({
  name: 'Baggage Belt Faults',
  description: 'Report baggage belt faults.',
  builderDisplayName: 'Priya Builder',
  url: 'https://pub-abc.example/',
  ...over,
})

const page = (over: Partial<Page> = {}): Page => ({
  items: [entry()],
  page: 1,
  pageSize: 10,
  total: 1,
  totalPages: 1,
  ...over,
})

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/marketplace']}>
      <MarketplacePage />
    </MemoryRouter>,
  )

/** The args of the most recent request — what the page actually asked the server for. */
const lastCall = () => h.listMarketplace.mock.calls.at(-1)?.[0]

/** Opens a <Select> trigger and picks the option with this text. */
async function pickSelect(triggerTestId: string, optionText: string) {
  fireEvent.click(screen.getByTestId(triggerTestId))
  fireEvent.click(await screen.findByRole('option', { name: optionText }))
}

afterEach(cleanup)
beforeEach(() => {
  h.listMarketplace.mockReset()
  // jsdom doesn't implement these; Radix's <Select> calls them on open/scroll (suite-wide
  // convention, see UsersLimitsPanel/BuilderPage test files — vitest.config.js has no
  // setupFiles, so the shim lives per-file).
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn().mockReturnValue(false)
  Element.prototype.releasePointerCapture = vi.fn()
  Element.prototype.setPointerCapture = vi.fn()
})

describe('MarketplacePage', () => {
  it('shows an app built by someone else, naming the builder', async () => {
    h.listMarketplace.mockResolvedValue(page())
    renderPage()

    expect(await screen.findByText('Baggage Belt Faults', {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText(/Built by Priya Builder/)).toBeTruthy()
    expect(screen.getByTestId('marketplace-open').getAttribute('href')).toBe(
      'https://pub-abc.example/',
    )
  })

  it('says nothing is published when the catalog is genuinely empty', async () => {
    h.listMarketplace.mockResolvedValue(page({ items: [], total: 0 }))
    renderPage()

    expect(
      (await screen.findByTestId('marketplace-empty', {}, { timeout: 5000 })).textContent,
    ).toMatch(/nothing has been published/i)
  })

  it('says the SEARCH matched nothing once a query has been applied', async () => {
    h.listMarketplace.mockResolvedValue(page({ items: [], total: 0 }))
    renderPage()
    await screen.findByTestId('marketplace-empty', {}, { timeout: 5000 })

    fireEvent.change(screen.getByTestId('marketplace-search'), { target: { value: 'baggage' } })

    await waitFor(() => expect(lastCall()).toMatchObject({ q: 'baggage' }))
    await waitFor(() =>
      expect(screen.getByTestId('marketplace-empty').textContent).toMatch(/no published app/i),
    )
  })

  it('renders numbered pages and asks for the one clicked', async () => {
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()

    await screen.findByTestId('marketplace-page-2', {}, { timeout: 5000 })
    fireEvent.click(screen.getByTestId('marketplace-page-2'))

    await waitFor(() => expect(lastCall()).toMatchObject({ page: 2 }))
  })

  it('marks the current page for assistive tech, not just visually', async () => {
    // Mutation receipt: drop `aria-current` from `PaginationLink` and this goes red. The
    // underline alone tells a sighted user which page they are on and nobody else.
    //
    // The highlight follows the RENDERED page (`data.page`), not the requested one — so a
    // response saying "this is page 2" highlights 2. An earlier version of this test asserted
    // the opposite, which quietly encoded the mismatch the review asked to fix: the control
    // claiming page 1 while page 2's cards were on screen.
    h.listMarketplace.mockResolvedValue(
      page({ page: 2, pageSize: 10, total: 25, totalPages: 3 }),
    )
    renderPage()

    const current = await screen.findByTestId('marketplace-page-2', {}, { timeout: 5000 })
    expect(current.getAttribute('aria-current')).toBe('page')
    expect(screen.getByTestId('marketplace-page-1').getAttribute('aria-current')).toBeNull()
  })

  it('disables Previous on the first page and Next on the last', async () => {
    // Both halves of the name, actually exercised. An earlier version asserted "Prev
    // disabled" and "Next enabled" while never leaving page 1 — both page-1 facts, so
    // `disabled={page >= totalPages}` was never evaluated at the boundary and a mutant that
    // dropped it survived.
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()

    const prev = await screen.findByTestId('marketplace-prev', {}, { timeout: 5000 })
    expect((prev as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByTestId('marketplace-next') as HTMLButtonElement).disabled).toBe(false)

    // Navigate to the LAST page and assert the other end of the boundary.
    h.listMarketplace.mockResolvedValue(
      page({ page: 3, pageSize: 10, total: 25, totalPages: 3 }),
    )
    fireEvent.click(screen.getByTestId('marketplace-page-3'))

    await waitFor(() =>
      expect((screen.getByTestId('marketplace-next') as HTMLButtonElement).disabled).toBe(true),
    )
    expect((screen.getByTestId('marketplace-prev') as HTMLButtonElement).disabled).toBe(false)
  })

  it('resets to page 1 when the page size changes', async () => {
    // Mutation receipt: remove `setPage(1)` from the page-size handler and this goes red —
    // the request keeps `page: 3` against a result set that has just been renumbered, so the
    // user lands somewhere arbitrary.
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 100, totalPages: 10 }))
    renderPage()
    await screen.findByTestId('marketplace-page-3', {}, { timeout: 5000 })

    fireEvent.click(screen.getByTestId('marketplace-page-3'))
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 3 }))

    await pickSelect('marketplace-page-size', '50')

    await waitFor(() => expect(lastCall()).toMatchObject({ page: 1, limit: 50 }))
  })

  it('resets to page 1 when the sort changes', async () => {
    // Mutation receipt: remove `setPage(1)` from the sort handler and this goes red.
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 100, totalPages: 10 }))
    renderPage()
    await screen.findByTestId('marketplace-page-3', {}, { timeout: 5000 })

    fireEvent.click(screen.getByTestId('marketplace-page-3'))
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 3 }))

    // NB: en dash (U+2013) in "Name (A–Z)", not a hyphen — `findByRole` name matching is
    // exact, so a plain '-' silently fails to match and times out.
    await pickSelect('marketplace-sort', 'Name (A–Z)')

    await waitFor(() => expect(lastCall()).toMatchObject({ page: 1, sort: 'name' }))
  })

  it('hides the pagination control entirely when everything fits on one page', async () => {
    h.listMarketplace.mockResolvedValue(page({ total: 3, totalPages: 1 }))
    renderPage()

    await screen.findByText('Baggage Belt Faults', {}, { timeout: 5000 })
    expect(screen.queryByTestId('marketplace-next')).toBeNull()
    expect(screen.queryByTestId('marketplace-page-size')).toBeNull()
  })

  it('keeps Rows per page reachable when a big catalog still fits on one page', async () => {
    // 30 apps at 50 rows is a single page. Gating the sizer on `totalPages > 1` would hide
    // the only control that could take the reader back to 10 per page, exactly when they
    // wanted it — so it is gated on the catalog size instead.
    h.listMarketplace.mockResolvedValue(page({ pageSize: 50, total: 30, totalPages: 1 }))
    renderPage()

    await screen.findByTestId('marketplace-page-size', {}, { timeout: 5000 })
    expect(screen.queryByTestId('marketplace-next')).toBeNull()
  })

  it('explains that sorting yields to relevance while a search is active', async () => {
    // Otherwise picking A-Z mid-search looks like the control is broken: the order does not
    // change, because the server ranks by relevance whatever `sort` says.
    h.listMarketplace.mockResolvedValue(page())
    renderPage()
    await screen.findByText('Baggage Belt Faults', {}, { timeout: 5000 })

    await pickSelect('marketplace-sort', 'Name (A–Z)')
    fireEvent.change(screen.getByTestId('marketplace-search'), { target: { value: 'baggage' } })

    await waitFor(() => expect(lastCall()).toMatchObject({ q: 'baggage' }))
    await waitFor(() => expect(screen.getByText(/ordered by relevance/i)).toBeTruthy())
  })

  it('renders an app with no description without leaving a broken gap', async () => {
    h.listMarketplace.mockResolvedValue(page({ items: [entry({ description: null })] }))
    renderPage()

    expect(await screen.findByText(/no description yet/i, {}, { timeout: 5000 })).toBeTruthy()
  })

  it('does not claim the catalog is empty when the FIRST load failed', async () => {
    // The reviewer's exact scenario, and it has to be the FIRST load: `loading` is false,
    // `error` is set, and `data` is still the EMPTY sentinel — so `items.length === 0` is
    // genuinely true and the empty-state copy renders right beside the error banner,
    // telling the reader the marketplace is empty when in fact we do not know.
    //
    // Failing a LATER load does not pin this: `data` still holds the previous page's items,
    // so `items.length === 0` is false and the empty state stays hidden for the wrong
    // reason. (Learned the hard way — the first version of this receipt survived its mutant.)
    //
    // Mutation receipt: drop `!error` from the empty-state guard and this goes red.
    h.listMarketplace.mockRejectedValue(new Error('Network is down'))
    renderPage()

    expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByTestId('marketplace-empty')).toBeNull()
  })

  it('shows the error alone on a failed load, and lets the reader retry', async () => {
    // Two defects pinned here, neither of which had ANY coverage before (#147 review): the
    // failed page staying active over the PREVIOUS page's cards, and the retry being a
    // no-op because `setPage(sameValue)` is a React bail-out, so the dispatcher never
    // re-runs. (The empty-state guard is pinned by the test above, which fails the FIRST
    // load — the only point at which `items` is genuinely empty.)
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()
    await screen.findByTestId('marketplace-page-2', {}, { timeout: 5000 })

    h.listMarketplace.mockRejectedValueOnce(new Error('Network is down'))
    fireEvent.click(screen.getByTestId('marketplace-page-2'))

    expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toBeTruthy()

    // `page` rolled back to the last one that rendered, so page 1 is active again — which
    // is what makes re-clicking page 2 a real state change rather than a no-op.
    await waitFor(() =>
      expect(screen.getByTestId('marketplace-page-1').getAttribute('aria-current')).toBe('page'),
    )

    h.listMarketplace.mockResolvedValue(
      page({ page: 2, pageSize: 10, total: 25, totalPages: 3 }),
    )
    fireEvent.click(screen.getByTestId('marketplace-page-2'))

    await waitFor(() => expect(lastCall()).toMatchObject({ page: 2 }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  it('leaves a failed navigation visible instead of silently reverting it', async () => {
    // The bug this replaced: rolling `page` back inside `catch` looked like a fix, but `page`
    // is a dispatcher dependency — so the rollback immediately refetched the previous page,
    // and that success cleared `error`. A transient failure on "Next" reverted the
    // navigation and dismissed its own banner before anyone could read it.
    //
    // Note `mockRejectedValue`, not `...Once`: if a stray refetch happens this stays failed
    // and the assertions below still hold, so the test cannot pass by racing a recovery.
    //
    // Mutation receipt: re-add `setPage(lastGoodPage)` to the catch arm and this goes red —
    // the request count climbs and the alert disappears.
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()
    await screen.findByTestId('marketplace-page-2', {}, { timeout: 5000 })
    const before = h.listMarketplace.mock.calls.length

    h.listMarketplace.mockRejectedValue(new Error('Network is down'))
    fireEvent.click(screen.getByTestId('marketplace-page-2'))
    await screen.findByRole('alert', {}, { timeout: 5000 })

    // Exactly ONE new request: the failed one. No self-triggered refetch behind it.
    await waitFor(() => expect(h.listMarketplace.mock.calls.length).toBe(before + 1))
    // And the banner is still there a tick later, rather than clearing itself.
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByRole('alert')).not.toBeNull()
  })

  it('climbs back out of a page past the end of a shrunken catalog', async () => {
    // A page past the end is a NORMAL response — the server documents it, and it happens
    // whenever the catalog shrinks under a reader who is deep in it. Left alone it was a dead
    // end: `items` empty so nothing to page from, and with the catalog now under one page
    // both controls unmount, stranding `page` at a number nothing on screen can reach.
    //
    // Mutation receipt: delete the past-the-end effect and this goes red — the page never
    // re-requests, and the empty state sits under a non-zero "N published apps".
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()
    await screen.findByTestId('marketplace-page-3', {}, { timeout: 5000 })

    // The catalog shrinks to a single page while the reader is on page 3.
    h.listMarketplace.mockResolvedValue(
      page({ items: [], page: 3, pageSize: 10, total: 5, totalPages: 1 }),
    )
    fireEvent.click(screen.getByTestId('marketplace-page-3'))

    // It re-requests the last page that actually exists rather than stranding the reader.
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 1 }))
  })

  it('recovers when the catalog empties COMPLETELY under a reader on a later page', async () => {
    // The worst case, and the one the recovery effect used to miss: gated on `total > 0`, an
    // emptied catalog (`total: 0`, `totalPages` clamped to 1) on page 2+ meant the effect
    // never fired, the nav unmounted, and the copy promised "taking you back" while nothing
    // took anyone anywhere. `page > totalPages` alone is the correct trigger.
    //
    // Mutation receipt: re-add `total > 0` to the effect's condition and this goes red —
    // `page` stays at 2 and the request for page 1 never happens.
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()
    await screen.findByTestId('marketplace-page-2', {}, { timeout: 5000 })

    // Everything is unpublished while the reader sits on page 2.
    h.listMarketplace
      .mockResolvedValueOnce(page({ items: [], page: 2, pageSize: 10, total: 0, totalPages: 1 }))
      .mockResolvedValue(page({ items: [], page: 1, pageSize: 10, total: 0, totalPages: 1 }))

    fireEvent.click(screen.getByTestId('marketplace-page-2'))
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 2 }))

    // The effect snaps back rather than stranding them behind an unmounted nav.
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 1 }))
    await waitFor(() =>
      expect(screen.getByTestId('marketplace-empty').textContent).toMatch(
        /nothing has been published/i,
      ),
    )
  })

  it('does not blame the page when page 1 comes back empty with a stale total', async () => {
    // The race agc129 named: `total` and the rows are two separate reads under READ
    // COMMITTED, so an unpublish landing between them returns zero items on PAGE 1 with a
    // stale non-zero `total`. Branching the copy on `total !== 0` showed "past the end" —
    // on page 1, which has nowhere to go back to. Branching on `page > totalPages` falls
    // through to the ordinary empty copy, which is the honest thing to say.
    //
    // This is also the ONLY deterministic pin for that branch: when `page > totalPages` is
    // genuinely true the auto-correct effect fires in the same commit, so the overshoot
    // message exists for a single frame and no assertion can catch it reliably.
    //
    // Mutation receipt: swap the ternary back to `total === 0` first and this goes red with
    // "past the end" on page 1.
    h.listMarketplace.mockResolvedValue(
      page({ items: [], page: 1, pageSize: 10, total: 5, totalPages: 1 }),
    )
    renderPage()

    const empty = await screen.findByTestId('marketplace-empty', {}, { timeout: 5000 })
    expect(empty.textContent).toMatch(/nothing has been published/i)
    expect(empty.textContent).not.toMatch(/past the end/i)
  })

  it('recovers instead of stranding the reader when the catalog shrinks under them', async () => {
    // Driven to page 3 rather than MOCKED there. The previous version set `page: 3` in the
    // response payload while the component's own `page` stayed 1 — so it asserted copy the
    // product would never show in that combination, which is the same "seed a state the
    // product reaches differently" shape flagged twice in review (#147 round 3).
    //
    // What is pinned here is RECOVERY, because that is what is durable: the overshoot copy
    // is transient by construction — the auto-correct effect fires in the same commit and
    // snaps `page` back, so the message exists for one frame. Asserting the end state is
    // both honest and non-racy.
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()
    await screen.findByTestId('marketplace-page-3', {}, { timeout: 5000 })

    // Both responses are queued BEFORE the click. Installing the second one afterwards is a
    // race the auto-correct can win — its refetch fires in the same tick as the overshoot
    // response, so it would sometimes read the shrunk mock and land back on the empty state.
    // `Once` then default makes the ordering deterministic regardless of scheduling.
    h.listMarketplace
      .mockResolvedValueOnce(page({ items: [], page: 3, pageSize: 10, total: 5, totalPages: 1 }))
      .mockResolvedValue(page({ pageSize: 10, total: 5, totalPages: 1 }))

    fireEvent.click(screen.getByTestId('marketplace-page-3'))
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 3 }))

    // Auto-correct re-requests the last real page, and the reader lands on actual results
    // rather than a dead end with no control mounted.
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 1 }))
    expect(await screen.findByText('Baggage Belt Faults', {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByTestId('marketplace-empty')).toBeNull()
  })

  it('offers a retry when the FIRST load fails, with no pagination mounted', async () => {
    // The gap round 3 found: `reloadNonce` was only reachable through the pagination nav,
    // and on a failed first load `data` is still the EMPTY sentinel — so `showSizer` and
    // `showPages` are both false, the nav never mounts, and the reader is stranded with a
    // banner and no control at all. Neither existing error test covers this: one only
    // asserts the empty-state copy is suppressed, and the other starts from a SUCCESSFUL
    // load, so its nav is already on screen.
    //
    // Mutation receipt: delete the `marketplace-retry` button (or re-bind it to
    // `showPages`) and this goes red.
    h.listMarketplace.mockRejectedValueOnce(new Error('Network is down'))
    renderPage()

    expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toBeTruthy()
    // Nothing else on the page can dispatch a fetch in this state.
    expect(screen.queryByTestId('marketplace-next')).toBeNull()
    expect(screen.queryByTestId('marketplace-page-size')).toBeNull()

    h.listMarketplace.mockResolvedValue(page())
    fireEvent.click(screen.getByTestId('marketplace-retry'))

    expect(await screen.findByText('Baggage Belt Faults', {}, { timeout: 5000 })).toBeTruthy()
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  it('ignores a stale response that lands after a newer one', async () => {
    // The `requestId` guard, which was previously deletable with the whole suite green
    // (#147 review). The FIRST request resolves LAST here — exactly the interleaving a slow
    // network produces — so an implementation without the guard commits the stale page-2
    // body over the page-3 one the user actually asked for.
    let resolveFirst: (value: Page) => void = () => {}
    const firstInFlight = new Promise<Page>((resolve) => {
      resolveFirst = resolve
    })

    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()
    await screen.findByTestId('marketplace-page-2', {}, { timeout: 5000 })

    const stale = page({
      page: 2,
      pageSize: 10,
      total: 25,
      totalPages: 3,
      items: [entry({ name: 'STALE PAGE TWO', url: 'https://pub-stale.example/' })],
    })
    const fresh = page({
      page: 3,
      pageSize: 10,
      total: 25,
      totalPages: 3,
      items: [entry({ name: 'FRESH PAGE THREE', url: 'https://pub-fresh.example/' })],
    })

    h.listMarketplace.mockReturnValueOnce(firstInFlight) // page 2 — hangs
    fireEvent.click(screen.getByTestId('marketplace-page-2'))
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 2 }))

    h.listMarketplace.mockResolvedValueOnce(fresh) // page 3 — resolves immediately
    fireEvent.click(screen.getByTestId('marketplace-page-3'))
    await screen.findByText('FRESH PAGE THREE', {}, { timeout: 5000 })

    // Only now does the superseded page-2 request come back.
    resolveFirst(stale)

    await waitFor(() => expect(screen.queryByText('STALE PAGE TWO')).toBeNull())
    expect(screen.getByText('FRESH PAGE THREE')).toBeTruthy()
  })
})
