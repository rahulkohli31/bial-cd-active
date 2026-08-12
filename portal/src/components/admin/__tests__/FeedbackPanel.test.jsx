import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
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
    // No truncation banner when total equals the number of rows.
    expect(container.textContent).not.toContain('Showing newest')
  })

  it('shows the "newest N of M" banner when total exceeds the row count', async () => {
    fetchFeedback.mockResolvedValue({ feedback: [row()], total: 250 })
    await renderPanel()
    expect(container.textContent).toContain('Showing newest 1 of 250')
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
