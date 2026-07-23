/**
 * ProjectBuilder (U13): the root Build box of the unified chat.
 *
 * What this pins:
 *  - the Ask/Plan/Write toggle renders with PLAN as the default;
 *  - every submit MINTS A FRESH conversation at /chat/{uuid}?projectId=&kind=builder,
 *    carrying { prompt, mode, theme, pendingAttachments } — the canonical-thread resolve
 *    is gone, and so is the per-mode send label (the action is mode-neutral);
 *  - the theme selector still offers its four themes; a sample card fills the prompt;
 *  - a blocked prompt opens the guardrail modal instead of navigating.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useParams } from 'react-router-dom'

import ProjectBuilder from '../ProjectBuilder'

const FIXED_UUID = '22222222-2222-4222-8222-222222222222'

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
  it('renders the Ask/Plan/Write toggle with Plan as the default', () => {
    renderBuilder()

    const ask = screen.getByRole('button', { name: /^Ask$/ })
    const plan = screen.getByRole('button', { name: /^Plan$/ })
    const write = screen.getByRole('button', { name: /^Write$/ })
    expect(ask.getAttribute('aria-pressed')).toBe('false')
    expect(plan.getAttribute('aria-pressed')).toBe('true') // DEFAULT PLAN (the plan's lock)
    expect(write.getAttribute('aria-pressed')).toBe('false')

    expect(screen.getByRole('button', { name: /Bangalore Airport Theme/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Upload File/i })).toBeTruthy()
    const start = screen.getByRole('button', { name: /Start Chat/i })
    expect(start.disabled).toBe(true) // disabled until typed

    expect(screen.getByText('Resource Management')).toBeTruthy()
    expect(screen.getByText('Staff Coordination')).toBeTruthy()
    expect(screen.getByText('Flight Metrics')).toBeTruthy()
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

  it('fills the prompt when a sample-prompt card is clicked', () => {
    renderBuilder()

    fireEvent.click(screen.getByText('Resource Management'))
    const textarea = screen.getByPlaceholderText(/we.ll shape the plan together/i)
    expect(textarea.value).toContain('track gate equipment maintenance logs')
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

    fireEvent.click(screen.getByRole('button', { name: /^Write$/ }))
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
