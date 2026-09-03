/**
 * THE RAIL'S COMPOSER (Plan F, U1) — the mint-and-navigate protocol and R15's kind picker.
 *
 * Two things are under test and they fail differently.
 *
 * THE PROTOCOL is inherited from a component this plan deletes, and the deletion is exactly how it
 * gets lost: the id's version, the query/state split, and the `freshlyMinted` flag are all invisible
 * in a render and only wrong later — a v4 id becomes a badly-ordered primary key, a kind carried in
 * router state dies on reload, and a missing flag costs four guaranteed-404 requests per new chat.
 * Nothing about the screen looks different in any of those cases.
 *
 * THE PICKER is new, and it is what makes half the product reachable: a chat's kind is fixed at
 * creation, the retired composer hardcoded the build kind, and its own docstring called the control
 * for the other kind "a picker nobody has designed yet". Without it this rail can only mint Build
 * chats.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import RailComposer from '../RailComposer'

// The words a citizen reads come from the bootstrap catalogue, not from this file — so a suite that
// does not stand one up gets the honest "Chat" fallback on every option and every label assertion
// fails for a reason that has nothing to do with this component.
vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

/** Where a navigation actually landed, plus the router state it carried. */
function LocationProbe() {
  const loc = useLocation()
  return (
    <div>
      <span data-testid="path">{loc.pathname + loc.search}</span>
      <span data-testid="state">{JSON.stringify(loc.state)}</span>
    </div>
  )
}

function renderComposer(projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={['/projects/p1']}>
      <Routes>
        <Route path="/projects/:projectId" element={<RailComposer projectId={projectId} />} />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

const composer = () => screen.getByPlaceholderText(/Describe the change you need/i)
const path = () => screen.getByTestId('path').textContent ?? ''
const routerState = () => JSON.parse(screen.getByTestId('state').textContent || 'null') as unknown

const send = (text = 'a visitor log') => {
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.click(screen.getByTestId('composer-send'))
}

beforeEach(() => {
  // Radix needs these in jsdom; the toggle group's items are focusable roving-tabindex controls.
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => cleanup())

describe('the mint-and-navigate protocol, carried through the deletion', () => {
  it('mints a UUIDv7, not a v4 — this id becomes a primary key', () => {
    // ADR-0006 wants a sortable primary key. The retired composer's own comment records what
    // happens without a shared mint: two sites each kept a private `crypto.randomUUID()` and both
    // went on producing v4 long after the store's mint moved on. Nothing about the screen looks
    // different when this is wrong.
    renderComposer()
    send()

    const id = path().split('?')[0].replace('/chat/', '')
    // The version nibble: the 15th hex digit of a UUID is its version.
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })

  it('carries the project and the kind as QUERY, and the draft as router STATE', () => {
    // The split is deliberate: router state dies on reload and never travels in a shared link, so a
    // bookmarked `/chat/{id}` must still be able to take its kind from somewhere. The draft is the
    // opposite — this navigation's payload, with no business surviving a reload or sitting in a URL.
    renderComposer()
    send('a visitor log')

    expect(path()).toMatch(/\?projectId=p1&kind=build$/)
    expect(routerState()).toMatchObject({ prompt: 'a visitor log', freshlyMinted: true })
  })

  it('sets `freshlyMinted`, so the route skips a GET that can only 404', () => {
    // The row does not exist until the first message, so the chat route's hydration fetch is a
    // guaranteed 404 — doubled by two hydration fetches and doubled again by StrictMode in dev.
    renderComposer()
    send()

    expect(routerState()).toMatchObject({ freshlyMinted: true })
  })

  it('encodes a project id that would otherwise break the query', () => {
    renderComposer('p 1&kind=plan')
    send()

    expect(path()).toContain('projectId=p%201%26kind%3Dplan')
    expect(path()).toMatch(/&kind=build$/)
  })

  it('navigates nowhere on an empty or whitespace-only draft', () => {
    renderComposer()
    fireEvent.change(composer(), { target: { value: '   ' } })
    fireEvent.click(screen.getByTestId('composer-send'))

    expect(screen.queryByTestId('path')).toBeNull()
  })

  it('blocks a guard-railed prompt before any navigation, and says why', () => {
    renderComposer()
    // A prompt the shared guardrails reject — `spy on` is one of the harmful-content keywords.
    // If it ever stops being rejected this goes red rather than silently proving nothing.
    fireEvent.change(composer(), { target: { value: 'an app to spy on the ground crew' } })
    fireEvent.click(screen.getByTestId('composer-send'))

    expect(screen.queryByTestId('path')).toBeNull()
    expect(screen.getByRole('dialog', { name: /prompt blocked/i })).toBeTruthy()
  })
})

describe("R15's picker — the control that makes the other half of the product reachable", () => {
  it('offers both kinds, with the words from the shared catalogue', () => {
    renderComposer()

    expect(screen.getByRole('radio', { name: 'Build' })).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Plan' })).toBeTruthy()
  })

  it('★ mints the kind that was PICKED, not the one that was hardcoded', () => {
    // The whole point. The retired composer wrote `kind=build` into the address unconditionally,
    // so a Plan chat could not be created from a project at all.
    renderComposer()
    fireEvent.click(screen.getByRole('radio', { name: 'Plan' }))
    send()

    expect(path()).toMatch(/&kind=plan$/)
  })

  it('defaults to Build, which is what this control did before it had a choice', () => {
    // Defaulting to Plan would silently change what the existing control does for every citizen who
    // never touches the picker.
    renderComposer()
    send()

    expect(path()).toMatch(/&kind=build$/)
  })

  it('reads its one line of explanation from the catalogue, never from this file', () => {
    // R73: one source for what a kind IS. A second wording here would drift the first time the
    // server's changed, and nothing would notice.
    renderComposer()
    expect(screen.getByTestId('kind-description').textContent).toBe('Change the live app.')

    fireEvent.click(screen.getByRole('radio', { name: 'Plan' }))
    expect(screen.getByTestId('kind-description').textContent).toBe('Shape a plan first.')
  })

  it('★ cannot reach a third, empty state by re-pressing the active option', () => {
    // Radix hands back `''` when a single-select item is pressed while already on. A chat is always
    // one kind or the other, so an empty value is not a state this control may reach — and the mint
    // below it would put `kind=` in the address with nothing after it.
    renderComposer()
    const build = screen.getByRole('radio', { name: 'Build' })
    fireEvent.click(build)
    fireEvent.click(build)
    send()

    expect(path()).toMatch(/&kind=build$/)
  })
})
