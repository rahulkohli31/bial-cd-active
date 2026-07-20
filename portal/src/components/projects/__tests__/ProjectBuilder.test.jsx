/**
 * ProjectBuilder (U1): the Sandbox composer, lifted into a project.
 *
 * What this pins:
 *  - the full composer renders (Build/Plan toggle, prompt, theme selector defaulting to
 *    the BIAL theme, Upload File, Generate App disabled-until-typed, 3 sample cards);
 *  - the theme selector opens to exactly the four themes and updates its trigger;
 *  - a sample card fills the prompt;
 *  - a BUILD send resolves the project's ONE canonical thread and lands there (003-U1) —
 *    it does NOT mint a builder chat per send, which is what used to leave a project with a
 *    pile of one-shot build chats and no continuity;
 *  - a PLAN send still mints a fresh chat at /chat/{uuid}?projectId=&kind=planning, carrying
 *    the router-state shape ChatPage reads — and NO ProjectPicker on either path;
 *  - a blocked prompt opens the guardrail modal instead of navigating.
 *
 * A ChatProbe on the /chat route reports the address AND the router state the handoff
 * carried, so each handoff is asserted end to end.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useParams } from 'react-router-dom'

const h = vi.hoisted(() => ({ resolveBuilderThread: vi.fn() }))
vi.mock('../../../utils/builderThreadApi', () => ({ resolveBuilderThread: h.resolveBuilderThread }))

import ProjectBuilder from '../ProjectBuilder'
import { ApiError } from '../../../utils/apiError'

const FIXED_UUID = '22222222-2222-4222-8222-222222222222'
const THREAD_ID = '33333333-3333-4333-8333-333333333333'

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
  h.resolveBuilderThread.mockResolvedValue({ id: THREAD_ID, projectId: 'p1', kind: 'builder' })
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

  it('Generate App resolves the canonical thread and lands there with {prompt,theme,pendingAttachments} — no ProjectPicker', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    renderBuilder()

    const textarea = screen.getByPlaceholderText(/Describe the app you want to build/i)
    fireEvent.change(textarea, { target: { value: 'a gate tracker' } })
    fireEvent.click(screen.getByRole('button', { name: /Generate App/i }))

    await waitFor(() => expect(screen.getByTestId('chat-path').textContent).toBe(THREAD_ID))
    expect(h.resolveBuilderThread).toHaveBeenCalledWith('p1')
    // The prompt rides as a DRAFT, not an auto-send: the interview runs before any build.
    const state = JSON.parse(screen.getByTestId('chat-state').textContent)
    expect(state.prompt).toBe('a gate tracker')
    expect(state.theme).toBe('bial')
    expect(state.pendingAttachments).toEqual([])

    // The picker gate is gone: no "which project" dialog ever rendered.
    expect(screen.queryByText(/lives in a project/i)).toBeNull()
  })

  it('mints NO builder chat id, and needs no transient ?projectId=&kind= query', async () => {
    // The regression guard. Under newest-wins canonicalization, a per-send mint would make each
    // fresh chat "the" project thread and orphan the previous transcript — silently. The
    // canonical thread's row already exists, so the server knows its project: no query needed.
    const mint = vi.spyOn(crypto, 'randomUUID').mockReturnValue(FIXED_UUID)
    renderBuilder()

    fireEvent.change(screen.getByPlaceholderText(/Describe the app you want to build/i), {
      target: { value: 'a gate tracker' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Generate App/i }))

    await waitFor(() => expect(screen.getByTestId('chat-path').textContent).toBe(THREAD_ID))
    expect(screen.getByTestId('chat-path').textContent).not.toContain(FIXED_UUID)
    expect(screen.getByTestId('chat-path').textContent).not.toContain('kind=builder')
    expect(mint).not.toHaveBeenCalled()
  })

  it('gates a double-click on Generate to a single resolve', async () => {
    let release
    h.resolveBuilderThread.mockReturnValue(new Promise((r) => { release = r }))
    renderBuilder()

    fireEvent.change(screen.getByPlaceholderText(/Describe the app you want to build/i), {
      target: { value: 'a gate tracker' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Generate App/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Opening/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Opening/i }))

    expect(h.resolveBuilderThread).toHaveBeenCalledTimes(1)
    release({ id: THREAD_ID, projectId: 'p1', kind: 'builder' })
  })

  it('keeps the draft and explains when the thread cannot be opened', async () => {
    h.resolveBuilderThread.mockRejectedValue(new Error('network'))
    renderBuilder()

    const textarea = screen.getByPlaceholderText(/Describe the app you want to build/i)
    fireEvent.change(textarea, { target: { value: 'a gate tracker' } })
    fireEvent.click(screen.getByRole('button', { name: /Generate App/i }))

    expect(await screen.findByText(/could not open this project’s build chat/i)).toBeTruthy()
    // A failed hop must not cost the user their typing — draft intact, Generate usable again.
    expect(textarea.value).toBe('a gate tracker')
    expect(screen.getByRole('button', { name: /Generate App/i }).disabled).toBe(false)
    expect(screen.queryByTestId('chat-path')).toBeNull()
  })

  it('names a deleted project rather than blaming the network', async () => {
    h.resolveBuilderThread.mockRejectedValue(new ApiError('Project not found.', 404))
    renderBuilder()

    fireEvent.change(screen.getByPlaceholderText(/Describe the app you want to build/i), {
      target: { value: 'a gate tracker' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Generate App/i }))

    expect(await screen.findByText(/no longer available/i)).toBeTruthy()
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
    // Planning is ideation: several parallel planning chats per project are legitimate and none
    // is canonical, so the per-send mint stays and no thread is resolved.
    expect(h.resolveBuilderThread).not.toHaveBeenCalled()
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
