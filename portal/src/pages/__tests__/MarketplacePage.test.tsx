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
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import MarketplacePage from '../MarketplacePage'
import type { MarketplacePage as Page } from '../../utils/marketplaceApi'

const h = vi.hoisted(() => ({ listMarketplace: vi.fn() }))
vi.mock('../../utils/marketplaceApi', () => h)
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
  pageSize: 25,
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

afterEach(cleanup)

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
    h.listMarketplace.mockResolvedValue(
      page({ page: 2, pageSize: 10, total: 25, totalPages: 3 }),
    )
    renderPage()

    const current = await screen.findByTestId('marketplace-page-1', {}, { timeout: 5000 })
    expect(current.getAttribute('aria-current')).toBe('page')
    expect(screen.getByTestId('marketplace-page-2').getAttribute('aria-current')).toBeNull()
  })

  it('disables Previous on the first page and Next on the last', async () => {
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 25, totalPages: 3 }))
    renderPage()

    const prev = await screen.findByTestId('marketplace-prev', {}, { timeout: 5000 })
    expect((prev as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByTestId('marketplace-next') as HTMLButtonElement).disabled).toBe(false)
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

    fireEvent.change(screen.getByTestId('marketplace-page-size'), { target: { value: '50' } })

    await waitFor(() => expect(lastCall()).toMatchObject({ page: 1, limit: 50 }))
  })

  it('resets to page 1 when the sort changes', async () => {
    // Mutation receipt: remove `setPage(1)` from the sort handler and this goes red.
    h.listMarketplace.mockResolvedValue(page({ pageSize: 10, total: 100, totalPages: 10 }))
    renderPage()
    await screen.findByTestId('marketplace-page-3', {}, { timeout: 5000 })

    fireEvent.click(screen.getByTestId('marketplace-page-3'))
    await waitFor(() => expect(lastCall()).toMatchObject({ page: 3 }))

    fireEvent.change(screen.getByTestId('marketplace-sort'), { target: { value: 'name' } })

    await waitFor(() => expect(lastCall()).toMatchObject({ page: 1, sort: 'name' }))
  })

  it('hides the pagination control entirely when everything fits on one page', async () => {
    h.listMarketplace.mockResolvedValue(page({ total: 3, totalPages: 1 }))
    renderPage()

    await screen.findByText('Baggage Belt Faults', {}, { timeout: 5000 })
    expect(screen.queryByTestId('marketplace-next')).toBeNull()
    expect(screen.queryByTestId('marketplace-page-size')).toBeNull()
  })

  it('explains that sorting yields to relevance while a search is active', async () => {
    // Otherwise picking A-Z mid-search looks like the control is broken: the order does not
    // change, because the server ranks by relevance whatever `sort` says.
    h.listMarketplace.mockResolvedValue(page())
    renderPage()
    await screen.findByText('Baggage Belt Faults', {}, { timeout: 5000 })

    fireEvent.change(screen.getByTestId('marketplace-sort'), { target: { value: 'name' } })
    fireEvent.change(screen.getByTestId('marketplace-search'), { target: { value: 'baggage' } })

    await waitFor(() => expect(lastCall()).toMatchObject({ q: 'baggage' }))
    await waitFor(() => expect(screen.getByText(/ordered by relevance/i)).toBeTruthy())
  })

  it('renders an app with no description without leaving a broken gap', async () => {
    h.listMarketplace.mockResolvedValue(page({ items: [entry({ description: null })] }))
    renderPage()

    expect(await screen.findByText(/no description yet/i, {}, { timeout: 5000 })).toBeTruthy()
  })
})
