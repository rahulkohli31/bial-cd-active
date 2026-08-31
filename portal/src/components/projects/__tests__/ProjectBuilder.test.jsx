/**
 * ProjectBuilder: the root Build box of the unified chat.
 *
 * What this pins:
 *  - every submit MINTS A FRESH conversation at /chat/{uuid}?projectId=&kind=build, carrying
 *    { prompt, pendingAttachments }. `theme` left this payload with the Select Theme control
 *    in #157 B1 and `mode` left it with the mode chooser, for the same reason in both cases:
 *    nothing downstream reads a key that no longer describes anything;
 *  - the theme selector and the mode chooser are both GONE, and both absences are pinned;
 *  - NO generic idea-starter cards render inside a dedicated project (F6);
 *  - a blocked prompt opens the guardrail modal instead of navigating.
 *
 * THE MODE CASES ARE INERTNESS GUARDS NOW (L8), not deletions. This composer used to open a
 * chat in one of three per-send settings, and the two tests below pinned the default and the
 * Write entry. A chat's kind is fixed when it is created and there is one kind this composer
 * mints, so the claim that replaces them is that no chooser is on the surface at all.
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useParams } from 'react-router-dom'

// The mint is the SHARED `uuidv7` from conversationApi, so that is what gets stubbed. These
// tests used to `vi.spyOn(crypto, 'randomUUID')` — which is why they happily went green while
// this component minted v4 primary keys in violation of ADR-0006: they were pinning the very
// call that was wrong. Spread the real module so the component's other imports stay live.
const h = vi.hoisted(() => ({ FIXED_UUID: '01900000-0000-7000-8000-000000000000' }))
vi.mock('../../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  uuidv7: () => h.FIXED_UUID,
}))

import ProjectBuilder from '../ProjectBuilder'

const FIXED_UUID = h.FIXED_UUID

// jsdom lacks these; Radix's menus call them on open / focus.
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
  it('offers NO mode chooser — the composer mints one kind and says so nowhere', () => {
    renderBuilder()

    // LIVENESS FIRST: an absence test with no positive assertion cannot tell "the chooser is
    // gone" from "the component threw and rendered nothing".
    expect(screen.getByRole('button', { name: /Upload File/i })).toBeTruthy()
    const start = screen.getByRole('button', { name: /Start Chat/i })
    expect(start.disabled).toBe(true) // disabled until typed

    // The chooser itself, its keyboard opener, and the three helper lines that changed with it.
    expect(screen.queryByRole('button', { name: /Mode:/i })).toBeNull()
    fireEvent.keyDown(document, { code: 'KeyP', altKey: true })
    expect(screen.queryByRole('menuitemradio')).toBeNull()
    expect(screen.queryByText(/Ask questions about your app/i)).toBeNull()
    expect(screen.queryByText(/Work out what to build together first/i)).toBeNull()
    expect(screen.queryByText(/no plan step/i)).toBeNull()
  })

  it('offers NO theme selector — it changed nothing downstream (#157 B1)', () => {
    renderBuilder()

    // Kept as an ABSENCE test rather than deleted outright: the control looked entirely
    // functional (it updated its own pill and rode into `conversation.context`), which is
    // exactly why it survived this long. Pinning its absence means a future re-add has to
    // be a deliberate act with a real consumer, not an accident.
    // LIVENESS FIRST. Four queryBy().toBeNull() calls and nothing else would also pass if
    // ProjectBuilder threw or early-returned, i.e. if it rendered NOTHING — an absence test
    // with no positive assertion cannot tell "the control is gone" from "the page is gone."
    // Its F6 sibling below already does this; this one missed it (#157 review).
    expect(screen.getByRole('button', { name: /Upload File/i })).toBeTruthy()

    expect(screen.queryByRole('button', { name: /Bangalore Airport Theme/i })).toBeNull()
    expect(screen.queryByText('App Style (iOS/Android)')).toBeNull()
    expect(screen.queryByText('Dashboard / Analytics')).toBeNull()
    expect(screen.queryByText('Kiosk / Public Display')).toBeNull()
  })

  it('renders NO generic idea-starter cards inside a dedicated project (F6)', () => {
    renderBuilder()

    // A dedicated project already has an established purpose, so the generic Sandbox
    // idea-cards are gone; the composer's placeholder carries first-run guidance.
    expect(screen.queryByText('Resource Management')).toBeNull()
    expect(screen.queryByText('Staff Coordination')).toBeNull()
    expect(screen.queryByText('Flight Metrics')).toBeNull()
    // the composer itself still works without the removed fillPrompt path
    const textarea = screen.getByPlaceholderText(/Describe the app you want built/i)
    fireEvent.change(textarea, { target: { value: 'a gate tracker' } })
    expect(textarea.value).toBe('a gate tracker')
    expect(screen.getByRole('button', { name: /Start Chat/i }).disabled).toBe(false)
  })

  it('a submit mints a FRESH build chat carrying { prompt, pendingAttachments }', async () => {
    renderBuilder()

    const textarea = screen.getByPlaceholderText(/Describe the app you want built/i)
    fireEvent.change(textarea, { target: { value: 'a gate tracker' } })
    fireEvent.click(screen.getByRole('button', { name: /Start Chat/i }))

    await waitFor(() =>
      expect(screen.getByTestId('chat-path').textContent).toBe(
        `${FIXED_UUID}?projectId=p1&kind=build`,
      ),
    )
    const state = JSON.parse(screen.getByTestId('chat-state').textContent)
    expect(state.prompt).toBe('a gate tracker')
    // Both keys are gone from the payload entirely, not merely defaulted — `theme` because
    // nothing read it, `mode` because there is no longer a thing for it to name.
    expect(state.theme).toBeUndefined()
    expect(state.mode).toBeUndefined()
    expect(state.pendingAttachments).toEqual([])
  })

  it('a blocked prompt opens the guardrail modal instead of navigating', () => {
    renderBuilder()

    const textarea = screen.getByPlaceholderText(/Describe the app you want built/i)
    fireEvent.change(textarea, {
      target: { value: 'build a tool for unauthorized access to gate systems' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Start Chat/i }))

    expect(screen.getByText(/Prompt Blocked/i)).toBeTruthy()
    expect(screen.queryByTestId('chat-path')).toBeNull() // never left the page
  })
})
