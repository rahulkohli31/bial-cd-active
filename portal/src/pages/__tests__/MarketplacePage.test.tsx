/**
 * MarketplacePage — the two behaviours a reader would most easily get wrong (#145).
 *
 * 1. The empty state says different things for "nothing is published" and "your search
 *    matched nothing", and it must decide that from the query that produced the CURRENT
 *    items (`appliedQuery`), never from the input value — which runs 300ms ahead of the data.
 * 2. "Load more" is hidden while a search is active, because a ranked search response
 *    carries no cursor and the button would have nothing to ask for.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import MarketplacePage from '../MarketplacePage'
import type { MarketplacePage as Page } from '../../utils/marketplaceApi'

const h = vi.hoisted(() => ({ listMarketplace: vi.fn() }))
vi.mock('../../utils/marketplaceApi', () => h)
// Stubbed like every other page test here: the chrome is not what this file is about,
// and the real one pulls in auth + router state the assertions do not touch.
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))

/** The page reads route context through `Navbar`'s siblings, so it needs a Router. */
const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/marketplace']}>
      <MarketplacePage />
    </MemoryRouter>,
  )

const entry = (over: Partial<Page['items'][number]> = {}) => ({
  name: 'Baggage Belt Faults',
  description: 'Report baggage belt faults.',
  builderDisplayName: 'Priya Builder',
  url: 'https://pub-abc.example/',
  ...over,
})

const page = (over: Partial<Page> = {}): Page => ({
  items: [entry()],
  nextCursor: null,
  hasMore: false,
  ...over,
})

afterEach(cleanup)

describe('MarketplacePage', () => {
  it('shows an app built by someone else, naming the builder', async () => {
    h.listMarketplace.mockResolvedValue(page())
    renderPage()

    // `findBy`'s 1s default is marginal here: `useKeysetList` debounces before its first
    // fetch, so the row can land just past it on a loaded machine.
    expect(await screen.findByText('Baggage Belt Faults', {}, { timeout: 5000 })).toBeTruthy()
    // Authorship is shown; the identifiers the API never returns cannot appear here either.
    expect(screen.getByText(/Built by Priya Builder/)).toBeTruthy()
    expect(screen.getByTestId('marketplace-open').getAttribute('href')).toBe(
      'https://pub-abc.example/',
    )
  })

  it('says nothing is published when the catalog is genuinely empty', async () => {
    h.listMarketplace.mockResolvedValue(page({ items: [] }))
    renderPage()

    expect((await screen.findByTestId('marketplace-empty', {}, { timeout: 5000 })).textContent).toMatch(
      /nothing has been published/i,
    )
  })

  it('says the SEARCH matched nothing once a query has been applied', async () => {
    // Mutation receipt: decide `searching` from `q` instead of `appliedQuery` and this still
    // passes here but flashes the wrong copy during the debounce window — which is why the
    // assertion waits for the searched fetch to land before reading the message.
    h.listMarketplace.mockResolvedValue(page({ items: [] }))
    renderPage()
    await screen.findByTestId('marketplace-empty', {}, { timeout: 5000 })

    fireEvent.change(screen.getByTestId('marketplace-search'), { target: { value: 'baggage' } })

    await waitFor(() =>
      expect(h.listMarketplace).toHaveBeenCalledWith(expect.objectContaining({ q: 'baggage' })),
    )
    await waitFor(() =>
      expect(screen.getByTestId('marketplace-empty').textContent).toMatch(/no published app/i),
    )
  })

  it('offers Load more for a paginated catalog', async () => {
    h.listMarketplace.mockResolvedValue(page({ nextCursor: 'c1', hasMore: true }))
    renderPage()

    expect(await screen.findByTestId('marketplace-load-more', {}, { timeout: 5000 })).toBeTruthy()
  })

  it('hides Load more while searching, because a ranked page has no cursor to continue', async () => {
    // The search response deliberately still claims `hasMore: true`. Only the `!searching`
    // guard can hide the button against it, so this pins the guard rather than the data.
    //
    // The assertion waits for the SEARCHED ROW to render before reading the button, which
    // matters more than it looks: `loading` also hides the button, so asserting during the
    // fetch would pass even with the guard removed. An earlier version of this test did
    // exactly that and survived the mutant.
    //
    // Mutation receipt: drop `&& !searching` from the button's condition and this goes red.
    h.listMarketplace.mockResolvedValue(
      page({ items: [entry({ name: 'Catalog App' })], nextCursor: 'c1', hasMore: true }),
    )
    renderPage()
    await screen.findByTestId('marketplace-load-more', {}, { timeout: 5000 })

    h.listMarketplace.mockResolvedValue(
      page({ items: [entry({ name: 'Searched App' })], nextCursor: 'c9', hasMore: true }),
    )
    fireEvent.change(screen.getByTestId('marketplace-search'), { target: { value: 'baggage' } })

    // Settled: the searched page has rendered, so `loading` is false again.
    await screen.findByText('Searched App', {}, { timeout: 5000 })
    expect(screen.queryByTestId('marketplace-load-more')).toBeNull()
  })

  it('renders an app with no description without leaving a broken gap', async () => {
    h.listMarketplace.mockResolvedValue(page({ items: [entry({ description: null })] }))
    renderPage()

    expect(await screen.findByText(/no description yet/i, {}, { timeout: 5000 })).toBeTruthy()
  })
})
