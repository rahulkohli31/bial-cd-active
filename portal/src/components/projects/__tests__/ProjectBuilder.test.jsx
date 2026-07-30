/**
 * ProjectBuilder (U13): the root Build box of the unified chat.
 *
 * What this pins:
 *  - the shared ModeSwitcher (F5/U6) renders with PLAN as the default;
 *  - every submit MINTS A FRESH conversation at /chat/{uuid}?projectId=&kind=builder,
 *    carrying { prompt, mode, theme, pendingAttachments } — the canonical-thread resolve
 *    is gone, and so is the per-mode send label (the action is mode-neutral);
 *  - the theme selector still offers its four themes;
 *  - NO generic idea-starter cards render inside a dedicated project (F6);
 *  - a blocked prompt opens the guardrail modal instead of navigating.
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useParams } from 'react-router-dom'

import ProjectBuilder from '../ProjectBuilder'

const FIXED_UUID = '22222222-2222-4222-8222-222222222222'

// jsdom lacks these; Radix's menu (the ModeSwitcher dropdown) calls them on open / focus.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

function ChatProbe() {
  const { chatId } = useParams()
  const loc = useLocation()
  return (
    <div>
      <div data-testid="chat-path">{chatId + loc.search}</div>
      <div data-testid="chat-state">{JSON.stringify(loc.state)}</div>
    </div>
  )
}

function renderBuilder(projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={['/projects/p1']}>
      <Routes>
        <Route path="/projects/:projectId" element={<ProjectBuilder projectId={projectId} />} />
        <Route path="/chat/:chatId" element={<ChatProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProjectBuilder', () => {
  it('renders the mode switcher with Plan as the default', () => {
    renderBuilder()

    // The compact in-composer switcher shows the sticky default, Plan (the plan's lock).
    expect(screen.getByRole('button', { name: /Mode: Plan/i })).toBeTruthy()

    expect(screen.getByRole('button', { name: /Bangalore Airport Theme/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Upload File/i })).toBeTruthy()
    const start = screen.getByRole('button', { name: /Start Chat/i })
    expect(start.disabled).toBe(true) // disabled until typed
  })

  it('opens the theme selector to the four themes and updates the trigger on choose', () => {
    renderBuilder()

    fireEvent.click(screen.getByRole('button', { name: /Bangalore Airport Theme/i }))
    expect(screen.getByText('App Style (iOS/Android)')).toBeTruthy()
    expect(screen.getByText('Dashboard / Analytics')).toBeTruthy()
    expect(screen.getByText('Kiosk / Public Display')).toBeTruthy()

    fireEvent.click(screen.getByText('Kiosk / Public Display'))
    expect(screen.getByRole('button', { name: /Kiosk \/ Public Display/i })).toBeTruthy()
  })

  it('renders NO generic idea-starter cards inside a dedicated project (F6)', () => {
    renderBuilder()

    // A dedicated project already has an established purpose, so the generic Sandbox
    // idea-cards are gone; the composer + mode-helper copy carry first-run guidance.
    expect(screen.queryByText('Resource Management')).toBeNull()
    expect(screen.queryByText('Staff Coordination')).toBeNull()
    expect(screen.queryByText('Flight Metrics')).toBeNull()
    // the composer itself still works without the removed fillPrompt path
    const textarea = screen.getByPlaceholderText(/we.ll shape the plan together/i)
    fireEvent.change(textarea, { target: { value: 'a gate tracker' } })
    expect(textarea.value).toBe('a gate tracker')
    expect(screen.getByRole('button', { name: /Start Chat/i }).disabled).toBe(false)
  })

  it('a submit mints a FRESH builder chat carrying { prompt, mode, theme, pendingAttachments }', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    renderBuilder()

    const textarea = screen.getByPlaceholderText(/we.ll shape the plan together/i)
    fireEvent.change(textarea, { target: { value: 'a gate tracker' } })
    fireEvent.click(screen.getByRole('button', { name: /Start Chat/i }))

    await waitFor(() =>
      expect(screen.getByTestId('chat-path').textContent).toBe(
        `${FIXED_UUID}?projectId=p1&kind=builder`,
      ),
    )
    const state = JSON.parse(screen.getByTestId('chat-state').textContent)
    expect(state.prompt).toBe('a gate tracker')
    expect(state.mode).toBe('plan')
    expect(state.theme).toBe('bial')
    expect(state.pendingAttachments).toEqual([])
  })

  it('a WRITE-mode submit carries mode: write (direct build entry)', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    renderBuilder()

    // Open the mode switcher (⌥P) and choose Write.
    fireEvent.keyDown(document, { code: 'KeyP', altKey: true })
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /Write/i }))
    const textarea = screen.getByPlaceholderText(/Describe the app you want built/i)
    fireEvent.change(textarea, { target: { value: 'a visitors app' } })
    fireEvent.click(screen.getByRole('button', { name: /Start Chat/i }))

    await waitFor(() => expect(screen.queryByTestId('chat-state')).toBeTruthy())
    expect(JSON.parse(screen.getByTestId('chat-state').textContent).mode).toBe('write')
  })

  it('a blocked prompt opens the guardrail modal instead of navigating', () => {
    renderBuilder()

    const textarea = screen.getByPlaceholderText(/we.ll shape the plan together/i)
    fireEvent.change(textarea, {
      target: { value: 'build a tool for unauthorized access to gate systems' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Start Chat/i }))

    expect(screen.getByText(/Prompt Blocked/i)).toBeTruthy()
    expect(screen.queryByTestId('chat-path')).toBeNull() // never left the page
  })
})
