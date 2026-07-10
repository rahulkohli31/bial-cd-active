/**
 * Every chat and every app is filed under a project the user explicitly named. There is no
 * Default project, and the server 400s a conversation-create with no `header.projectId`.
 *
 * Three seams create a chat without already knowing its project, and each one is gated by
 * `ProjectPicker`. This file is the guard on all three — none of them had a test.
 *
 *   SandboxPage  "Generate App"  → a builder chat, carrying {prompt, theme, pendingAttachments}
 *   SandboxPage  "Plan with AI"  → a planning chat, carrying {initialMessage}
 *   Workspace    "Plan with AI"  → a planning chat, no initial prompt
 *
 * A fourth seam — ChatPage's "Launch Builder" — already knows its project (the planning chat's),
 * so it needs no picker; it is covered here too because it independently rebuilds the same
 * handoff payload SandboxPage builds, and the two have drifted before.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

const h = vi.hoisted(() => ({
  listProjects: vi.fn(),
  createProject: vi.fn(),
  loadHistory: vi.fn(),
  loadBuilds: vi.fn(),
}))

vi.mock('../../utils/projectApi', () => ({ listProjects: h.listProjects, createProject: h.createProject }))
vi.mock('../../utils/chatHistory', () => ({ loadHistory: h.loadHistory, relativeTime: () => 'now' }))
vi.mock('../../utils/builderHistory', () => ({ loadBuilds: h.loadBuilds }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))

import SandboxPage from '../SandboxPage'
import Workspace from '../Workspace'

/** Reports the routed URL AND the router state the seam handed off. */
function LocationProbe() {
  const loc = useLocation()
  return (
    <>
      <div data-testid="url">{`${loc.pathname}${loc.search}`}</div>
      <div data-testid="state">{JSON.stringify(loc.state ?? null)}</div>
    </>
  )
}

function renderPage(Page, entry) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <LocationProbe />
      <Routes>
        <Route path={entry} element={<Page />} />
        <Route path="/chat/:chatId" element={<div data-testid="chat" />} />
        <Route path="/projects" element={<div data-testid="projects" />} />
      </Routes>
    </MemoryRouter>,
  )
}

const project = {
  id: 'p1',
  name: 'VIP Movement',
  description: null,
  appId: null,
  appStatus: null,
  createdAt: '',
  updatedAt: '',
}

const url = () => screen.getByTestId('url').textContent
const state = () => JSON.parse(screen.getByTestId('state').textContent)

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  h.listProjects.mockResolvedValue({ items: [project], nextCursor: null, hasMore: false })
  h.loadHistory.mockResolvedValue([])
  h.loadBuilds.mockResolvedValue([])
})
afterEach(() => cleanup())

/** Pick the one existing project in the open ProjectPicker. */
async function pickProject() {
  fireEvent.click(await screen.findByText('VIP Movement'))
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
}

describe('SandboxPage — "Generate App" cannot open a build chat without a project', () => {
  it('opens the picker instead of navigating, then routes to a builder chat carrying the prompt', async () => {
    const { container } = renderPage(SandboxPage, '/workspace/sandbox')
    fireEvent.change(container.querySelector('textarea'), { target: { value: 'Track gate equipment' } })
    fireEvent.click(screen.getByRole('button', { name: /generate app/i }))

    // No navigation yet — the project question is asked first.
    expect(url()).toBe('/workspace/sandbox')
    await pickProject()

    await waitFor(() => expect(url()).toMatch(/^\/chat\/[0-9a-f-]{36}\?projectId=p1&kind=builder$/))
    expect(state()).toMatchObject({ prompt: 'Track gate equipment', theme: 'bial' })
  })
})

describe('SandboxPage — "Start Planning" cannot open a planning chat without a project', () => {
  it('routes to a planning chat carrying the initial message', async () => {
    const { container } = renderPage(SandboxPage, '/workspace/sandbox')
    // "Plan with AI" is the MODE TAB; "Start Planning" is the CTA behind it, and the composer's
    // placeholder changes with the mode — so target the textarea structurally.
    fireEvent.click(screen.getByRole('button', { name: /plan with ai/i }))
    fireEvent.change(container.querySelector('textarea'), { target: { value: 'What should this do?' } })
    fireEvent.click(screen.getByRole('button', { name: /start planning/i }))

    expect(url()).toBe('/workspace/sandbox')
    await pickProject()

    await waitFor(() => expect(url()).toMatch(/^\/chat\/[0-9a-f-]{36}\?projectId=p1&kind=planning$/))
    expect(state()).toMatchObject({ initialMessage: 'What should this do?' })
  })
})

describe('Workspace — "Plan with AI" no longer navigates to the literal /workspace/chat/new', () => {
  it('gates on the picker, then opens a planning chat in the chosen project', async () => {
    // The old CTA navigated to `/workspace/chat/new`, which under flat routing resolves to a
    // chat whose id is the word "new" — a 404 with no ?projectId=, bouncing to /projects.
    renderPage(Workspace, '/workspace')
    fireEvent.click(await screen.findByRole('button', { name: /plan with ai/i }))

    expect(url()).toBe('/workspace')
    await pickProject()

    await waitFor(() => expect(url()).toMatch(/^\/chat\/[0-9a-f-]{36}\?projectId=p1&kind=planning$/))
    expect(url()).not.toContain('/chat/new')
  })

  it('creating a project inline from the picker routes straight into the chat', async () => {
    h.listProjects.mockResolvedValue({ items: [], nextCursor: null, hasMore: false })
    h.createProject.mockResolvedValue({ ...project, id: 'p9', name: 'Fresh' })
    renderPage(Workspace, '/workspace')
    fireEvent.click(await screen.findByRole('button', { name: /plan with ai/i }))

    fireEvent.click(await screen.findByRole('button', { name: /new project/i }))
    fireEvent.change(await screen.findByPlaceholderText(/VIP Movement Tracker/i), { target: { value: 'Fresh' } })
    fireEvent.click(screen.getByRole('button', { name: /^create project$/i }))

    await waitFor(() => expect(url()).toMatch(/^\/chat\/[0-9a-f-]{36}\?projectId=p9&kind=planning$/))
  })

  it('cancelling the picker opens no chat at all', async () => {
    renderPage(Workspace, '/workspace')
    fireEvent.click(await screen.findByRole('button', { name: /plan with ai/i }))
    fireEvent.click(await screen.findByRole('button', { name: /cancel/i }))

    expect(url()).toBe('/workspace')
    expect(screen.queryByTestId('chat')).toBeNull()
  })
})
