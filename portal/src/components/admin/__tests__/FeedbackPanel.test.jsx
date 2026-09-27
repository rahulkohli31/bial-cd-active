import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { fireEvent } from '@testing-library/react'
import FeedbackPanel from '../FeedbackPanel.jsx'
import { fetchFeedback } from '../../../utils/admin'

// Mock the data layer so the panel renders against controlled fixtures.
vi.mock('../../../utils/admin', () => ({ fetchFeedback: vi.fn() }))

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

beforeEach(() => {
  fetchFeedback.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

const row = (over) => ({
  email: 'staff@bial.test',
  message: 'the export button does nothing',
  page: '/chat',
  createdAt: '2026-06-18T11:00:00.000Z',
  ...over,
})

async function renderPanel() {
  await act(async () => {
    root.render(<FeedbackPanel />)
  })
  await act(async () => {}) // flush the fetch-on-mount promise + state update
}

describe('FeedbackPanel', () => {
  it('renders one row per item with user, message, page and a formatted timestamp', async () => {
    fetchFeedback.mockResolvedValue({
      feedback: [
        row({ message: 'first', createdAt: '2026-06-18T11:00:00.000Z' }),
        row({ email: 'admin@bial.test', message: 'second', page: '', createdAt: '2026-06-18T10:00:00.000Z' }),
      ],
      total: 2,
    })
    await renderPanel()
    const rows = container.querySelectorAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('staff@bial.test')
    expect(rows[0].textContent).toContain('first')
    expect(rows[0].textContent).toContain('/chat')
    // No cap notice when total equals the number of rows.
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('says the list stops at the server cap when total exceeds the row count', async () => {
    fetchFeedback.mockResolvedValue({ feedback: [row()], total: 250 })
    await renderPanel()
    expect(container.querySelectorAll('tbody tr')).toHaveLength(1)
    expect(container.querySelector('[role="alert"]').textContent).toContain('Only the first 1 rows are loaded')
    expect(container.textContent).not.toContain('deferred')
  })

  it('search matches the user, the message or the page, whatever the case', async () => {
    fetchFeedback.mockResolvedValue({
      feedback: [
        row({ email: 'meera@bial.test', message: 'Export is slow', page: '/projects' }),
        row({ email: 'arjun@bial.test', message: 'love the chat', page: '/chat' }),
        row({ email: 'kavya@bial.test', message: 'login loops', page: '/admin' }),
      ],
      total: 3,
    })
    await renderPanel()
    const search = container.querySelector('input[type="search"]')
    const users = () => [...container.querySelectorAll('tbody tr')].map((tr) => tr.querySelector('td').textContent)

    fireEvent.change(search, { target: { value: 'ARJUN' } })
    expect(users()).toEqual(['arjun@bial.test'])
    fireEvent.change(search, { target: { value: 'export' } })
    expect(users()).toEqual(['meera@bial.test'])
    fireEvent.change(search, { target: { value: '/admin' } })
    expect(users()).toEqual(['kavya@bial.test'])
    fireEvent.change(search, { target: { value: '' } })
    expect(users()).toHaveLength(3)
  })

  it('sorts by a column header, ascending then descending', async () => {
    fetchFeedback.mockResolvedValue({
      feedback: [row({ email: 'b@bial.test' }), row({ email: 'c@bial.test' }), row({ email: 'a@bial.test' })],
      total: 3,
    })
    await renderPanel()
    const users = () => [...container.querySelectorAll('tbody tr')].map((tr) => tr.querySelector('td').textContent)
    fireEvent.click(container.querySelector('[data-testid="sort-email"]'))
    expect(users()).toEqual(['a@bial.test', 'b@bial.test', 'c@bial.test'])
    fireEvent.click(container.querySelector('[data-testid="sort-email"]'))
    expect(users()).toEqual(['c@bial.test', 'b@bial.test', 'a@bial.test'])
  })

  it('pages the feedback ten rows at a time', async () => {
    const twelve = Array.from({ length: 12 }, (_, i) => row({ email: `u${i}@bial.test` }))
    fetchFeedback.mockResolvedValue({ feedback: twelve, total: 12 })
    await renderPanel()
    expect(container.querySelectorAll('tbody tr')).toHaveLength(10)
    expect(container.textContent).toContain('Showing 1–10 of 12')
    fireEvent.click(container.querySelector('button[aria-label="Go to next page"]'))
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2)
    expect(container.textContent).toContain('Showing 11–12 of 12')
  })

  it('shows the loading state before the fetch resolves', () => {
    fetchFeedback.mockReturnValue(new Promise(() => {})) // never resolves
    act(() => {
      root.render(<FeedbackPanel />)
    })
    expect(container.textContent).toContain('Loading feedback')
  })

  it('shows an error with a Retry that refetches', async () => {
    fetchFeedback.mockRejectedValueOnce(new Error('Admin access required.')).mockResolvedValueOnce({ feedback: [], total: 0 })
    await renderPanel()
    expect(container.textContent).toContain('Admin access required.')

    const retry = [...container.querySelectorAll('button')].find((b) => /retry/i.test(b.textContent))
    expect(retry).toBeTruthy()
    await act(async () => {
      retry.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {})
    expect(fetchFeedback).toHaveBeenCalledTimes(2)
    expect(container.textContent).toContain('No feedback yet')
  })

  it('renders the empty state (not a table) when there is no feedback', async () => {
    fetchFeedback.mockResolvedValue({ feedback: [], total: 0 })
    await renderPanel()
    expect(container.textContent).toContain('No feedback yet')
    expect(container.querySelector('table')).toBeNull()
  })

  it('formats a valid timestamp (the When cell is not the raw ISO string)', async () => {
    const iso = '2026-06-18T11:00:00.000Z'
    fetchFeedback.mockResolvedValue({ feedback: [row({ createdAt: iso })], total: 1 })
    await renderPanel()
    const cells = container.querySelectorAll('tbody tr td')
    const whenCell = cells[cells.length - 1] // last column is "When"
    expect(whenCell.textContent.length).toBeGreaterThan(0)
    expect(whenCell.textContent).not.toBe(iso) // toLocaleString ran, not a raw passthrough
  })

  it('falls back to the raw value for an unparseable timestamp (no crash)', async () => {
    fetchFeedback.mockResolvedValue({ feedback: [row({ createdAt: 'not-a-date' })], total: 1 })
    await renderPanel()
    const cells = container.querySelectorAll('tbody tr td')
    expect(cells[cells.length - 1].textContent).toBe('not-a-date')
  })

  it('renders HTML-like feedback as literal text — no element injection (Decision 10)', async () => {
    const evil = '<img src=x onerror="alert(1)">'
    fetchFeedback.mockResolvedValue({ feedback: [row({ message: evil })], total: 1 })
    await renderPanel()
    expect(container.querySelector('img')).toBeNull() // not parsed into an element
    expect(container.textContent).toContain('<img src=x onerror=') // shown as text
  })
})
