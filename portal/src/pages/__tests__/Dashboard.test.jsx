/**
 * Dashboard (U4): one front door.
 *
 * The App Builder card is gone — the dashboard offers a single Projects card, with the
 * greeting and the Pilot (POC) disclaimer preserved. Navbar is stubbed; auth is mocked so
 * the greeting has a name.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import Dashboard from '../Dashboard'

vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/auth', () => ({ getStoredUser: () => ({ display_name: 'Anant', name: 'Anant' }) }))

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{loc.pathname}</div>
}

function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <Routes>
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => vi.clearAllMocks())
afterEach(() => cleanup())

describe('Dashboard — single front door', () => {
  it('keeps the greeting + Pilot (POC) notice and offers exactly one Projects card', () => {
    renderDashboard()
    expect(screen.getByRole('heading', { name: /Hello, Anant/i })).toBeTruthy()
    expect(screen.getByText('Pilot (POC)')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Projects' })).toBeTruthy()
  })

  it('has no App Builder door anywhere', () => {
    renderDashboard()
    expect(screen.queryByText(/App Builder/i)).toBeNull()
  })

  it('opens /projects from the Projects card', async () => {
    renderDashboard()
    fireEvent.click(screen.getByRole('heading', { name: 'Projects' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/projects'))
  })
})
