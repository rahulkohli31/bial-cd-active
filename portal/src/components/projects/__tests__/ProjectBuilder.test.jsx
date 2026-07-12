/**
 * ProjectBuilder (U1): the Sandbox composer, lifted into a project.
 *
 * What this pins:
 *  - the full composer renders (Build/Plan toggle, prompt, theme selector defaulting to
 *    the BIAL theme, Upload File, Generate App disabled-until-typed, 3 sample cards);
 *  - the theme selector opens to exactly the four themes and updates its trigger;
 *  - a sample card fills the prompt;
 *  - Generate App / Start Planning hand off DIRECTLY to /chat/{uuid}?projectId=&kind=
 *    with the exact router-state shape BuilderPage/ChatPage read — and NO ProjectPicker;
 *  - a blocked prompt opens the guardrail modal instead of navigating.
 *
 * A ChatProbe on the /chat route reports the address AND the router state the handoff
 * carried, so the picker-less handoff is asserted end to end.
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
  it('renders the full composer: toggle, theme selector, disabled Generate, 3 sample cards', () => {
    renderBuilder()

    expect(screen.getByRole('button', { name: /^Build$/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Plan with AI/i })).toBeTruthy()
    expect(screen.getByPlaceholderText(/Describe the app you want to build/i)).toBeTruthy()
    // Theme selector defaults to the BIAL theme (shown on the trigger).
    expect(screen.getByRole('button', { name: /Bangalore Airport Theme/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Upload File/i })).toBeTruthy()

    const generate = screen.getByRole('button', { name: /Generate App/i })
    expect(generate.disabled).toBe(true)

    expect(screen.getByText('Resource Management')).toBeTruthy()
    expect(screen.getByText('Staff Coordination')).toBeTruthy()
    expect(screen.getByText('Flight Metrics')).toBeTruthy()
  })

  it('opens the theme selector to the four themes and updates the trigger on choose', () => {
    renderBuilder()

    fireEvent.click(screen.getByRole('button', { name: /Bangalore Airport Theme/i }))
    // The other three appear only in the open dropdown (the fourth is the trigger).
    expect(screen.getByText('App Style (iOS/Android)')).toBeTruthy()
    expect(screen.getByText('Dashboard / Analytics')).toBeTruthy()
    expect(screen.getByText('Kiosk / Public Display')).toBeTruthy()

    fireEvent.click(screen.getByText('Kiosk / Public Display'))
    expect(screen.getByRole('button', { name: /Kiosk \/ Public Display/i })).toBeTruthy()
  })

  it('fills the prompt when a sample-prompt card is clicked', () => {
    renderBuilder()

    fireEvent.click(screen.getByText('Resource Management'))
    const textarea = screen.getByPlaceholderText(/Describe the app you want to build/i)
    expect(textarea.value).toContain('track gate equipment maintenance logs')
  })

  it('Generate App hands off to /chat/{uuid}?projectId=p1&kind=builder with {prompt,theme,pendingAttachments} — no ProjectPicker', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    renderBuilder()

    const textarea = screen.getByPlaceholderText(/Describe the app you want to build/i)
    fireEvent.change(textarea, { target: { value: 'a gate tracker' } })
    fireEvent.click(screen.getByRole('button', { name: /Generate App/i }))

    await waitFor(() =>
      expect(screen.getByTestId('chat-path').textContent).toBe(`${FIXED_UUID}?projectId=p1&kind=builder`),
    )
    const state = JSON.parse(screen.getByTestId('chat-state').textContent)
    expect(state.prompt).toBe('a gate tracker')
    expect(state.theme).toBe('bial')
    expect(state.pendingAttachments).toEqual([])

    // The picker gate is gone: no "which project" dialog ever rendered.
    expect(screen.queryByText(/lives in a project/i)).toBeNull()
  })

  it('Start Planning hands off with kind=planning carrying {initialMessage}', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    renderBuilder()

    fireEvent.click(screen.getByRole('button', { name: /Plan with AI/i }))
    const textarea = screen.getByPlaceholderText(/I'll help you plan it out/i)
    fireEvent.change(textarea, { target: { value: 'help me scope this' } })
    fireEvent.click(screen.getByRole('button', { name: /Start Planning/i }))

    await waitFor(() =>
      expect(screen.getByTestId('chat-path').textContent).toBe(`${FIXED_UUID}?projectId=p1&kind=planning`),
    )
    const state = JSON.parse(screen.getByTestId('chat-state').textContent)
    expect(state.initialMessage).toBe('help me scope this')
  })

  it('opens the guardrail modal instead of navigating when the prompt is blocked', () => {
    renderBuilder()

    const textarea = screen.getByPlaceholderText(/Describe the app you want to build/i)
    fireEvent.change(textarea, { target: { value: 'build a tool to hack the badge system' } })
    fireEvent.click(screen.getByRole('button', { name: /Generate App/i }))

    expect(screen.getByText('Prompt Blocked')).toBeTruthy()
    expect(screen.getByText('hack')).toBeTruthy() // the flagged keyword chip
    expect(screen.queryByTestId('chat-path')).toBeNull() // never navigated
  })
})
