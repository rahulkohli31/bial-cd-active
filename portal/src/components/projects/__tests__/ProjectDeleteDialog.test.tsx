/**
 * ProjectDeleteDialog — the guarantees a destructive confirm owes the user:
 *   1. The confirm button will not arm until the reason is a valid 5-50 words,
 *      and the dialog NAMES the account the deletion will be recorded against
 *      without asking anyone to type it.
 *   2. It names the cascade with a real chat count, and it never flashes
 *      "all 0 chats" while that count is still in flight — the numberless copy
 *      stands in until the count resolves.
 *
 * `listProjectConversations` is mocked at the module boundary so we can hold the
 * count fetch open and observe the loading-vs-resolved copy.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({ listProjectConversations: vi.fn(), getStoredUser: vi.fn() }))
vi.mock('../../../utils/auth', () => ({ getStoredUser: h.getStoredUser }))
vi.mock('../../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  // The real server cap. The dialog compares the count against it, so a mock that omits it
  // would make every comparison silently false.
  CONVERSATION_LIST_CAP: 200,
}))

import ProjectDeleteDialog from '../ProjectDeleteDialog'
import type { Project } from '../../../utils/projectApi'

const project: Project = {
  id: 'p1',
  name: 'VIP Movement',
  description: null,
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '',
  updatedAt: '',
  access: 'owner',
}

beforeEach(() => {
  vi.clearAllMocks()
  h.getStoredUser.mockReturnValue({ email: 'asha@bial.example', display_name: 'Asha Rao' })
})
afterEach(() => cleanup())

describe('ProjectDeleteDialog — confirm gating', () => {
  it('arms confirm on a VALID REASON, not on retyping the name', async () => {
    // A retyped name is not required: retyping proves you can read, not that you meant it, and
    // it taught people to copy-paste past the warning. The reason is the gate instead.
    h.listProjectConversations.mockResolvedValue([{}, {}])
    const onConfirm = vi.fn()
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={onConfirm} />)

    expect(await screen.findByText(/all 2 chats/i)).toBeTruthy()

    const confirmBtn = screen.getByRole('button', { name: /delete project/i })
    const reason = screen.getByLabelText(/why are you deleting/i)

    expect(confirmBtn.hasAttribute('disabled')).toBe(true) // nothing written yet

    fireEvent.change(reason, { target: { value: 'not needed' } }) // 2 words
    expect(confirmBtn.hasAttribute('disabled')).toBe(true)

    fireEvent.change(reason, { target: { value: 'no longer needed by ground ops' } }) // 6
    expect(confirmBtn.hasAttribute('disabled')).toBe(false)

    // Past the upper bound it disarms again — both ends are enforced, not just the lower.
    fireEvent.change(reason, { target: { value: 'word '.repeat(51) } })
    expect(confirmBtn.hasAttribute('disabled')).toBe(true)

    fireEvent.change(reason, { target: { value: 'no longer needed by ground ops' } })
    fireEvent.click(confirmBtn)
    // The reason travels to the caller: the page forwards it to the API, which refuses
    // independently if it is out of bounds.
    expect(onConfirm).toHaveBeenCalledWith('no longer needed by ground ops')
  })

  it('NAMES the account without asking for it, and cannot be edited', async () => {
    // A name this dialog can set is a name that can disagree with the account that acted — the
    // one question the stored name exists to answer — so it is shown, never collected.
    h.listProjectConversations.mockResolvedValue([])
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={vi.fn()} />)

    expect(screen.getByText(/recorded against asha rao/i)).toBeTruthy()
    expect(screen.queryByLabelText(/your name/i)).toBeNull()
    // Liveness: the reason IS still an input, so the absence above means something.
    expect(screen.getByLabelText(/why are you deleting/i)).toBeTruthy()
  })

  it('falls back to the email when Entra gave no display name', async () => {
    h.getStoredUser.mockReturnValue({ email: 'asha@bial.example', display_name: null })
    h.listProjectConversations.mockResolvedValue([])
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={vi.fn()} />)

    expect(screen.getByText(/recorded against asha@bial\.example/i)).toBeTruthy()
  })

  it('still says the deletion is recorded when the profile is not cached', async () => {
    // The cold path. It must not name nobody, and it must not invent a name — the server
    // stamps the row correctly either way, so the copy says the true thing without one.
    h.getStoredUser.mockReturnValue(null)
    h.listProjectConversations.mockResolvedValue([])
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={vi.fn()} />)

    expect(screen.getByText(/recorded against your account/i)).toBeTruthy()
  })

  it('no longer asks for the project name at all', async () => {
    // The inertness half: the old input is gone, not merely bypassed.
    h.listProjectConversations.mockResolvedValue([])
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={vi.fn()} />)

    expect(screen.queryByLabelText(/type the project name/i)).toBeNull()
    // Liveness, so the absence above means something.
    expect(screen.getByLabelText(/why are you deleting/i)).toBeTruthy()
    expect(screen.getByText(/are you sure you want to delete this project/i)).toBeTruthy()
  })

  it('says what happens to the reason, and does not overpromise who reads it', async () => {
    // Claiming "An administrator can see this" would be a promise nobody can keep — nothing reads
    // `deleted_projects`: no route, no schema, no screen. This test is what stops that claim from
    // coming back before the read surface actually lands.
    h.listProjectConversations.mockResolvedValue([])
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={vi.fn()} />)

    expect(screen.getByText(/kept with the deletion record/i)).toBeTruthy()
    expect(screen.queryByText(/an administrator can see this/i)).toBeNull()
  })

  it('arms at EXACTLY the bounds, and disarms one word outside either', async () => {
    // The boundaries themselves: the gating test above uses 2/6/51, so an off-by-one at either
    // end survived it — and the server parametrises 5 and 50 directly, so a client that disagreed
    // here would arm a button the API then refuses.
    h.listProjectConversations.mockResolvedValue([])
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={vi.fn()} />)

    const confirmBtn = screen.getByRole('button', { name: /delete project/i })
    const reason = screen.getByLabelText(/why are you deleting/i)
    const words = (n: number) => Array.from({ length: n }, (_, i) => `w${i}`).join(' ')

    fireEvent.change(reason, { target: { value: words(4) } })
    expect(confirmBtn.hasAttribute('disabled')).toBe(true)

    fireEvent.change(reason, { target: { value: words(5) } }) // the floor itself
    expect(confirmBtn.hasAttribute('disabled')).toBe(false)

    fireEvent.change(reason, { target: { value: words(50) } }) // the ceiling itself
    expect(confirmBtn.hasAttribute('disabled')).toBe(false)

    fireEvent.change(reason, { target: { value: words(51) } })
    expect(confirmBtn.hasAttribute('disabled')).toBe(true)
  })

  it('is a real modal: labelled, and dismissable with Escape', async () => {
    // The hand-rolled `fixed inset-0` announced itself as nothing and could not be closed from
    // the keyboard — in the one dialog that asks for a free-text answer before a destructive action.
    h.listProjectConversations.mockResolvedValue([])
    const onClose = vi.fn()
    render(<ProjectDeleteDialog project={project} onClose={onClose} onConfirm={vi.fn()} />)

    const dialog = screen.getByRole('dialog')
    // NOT `aria-modal`: Radix marks the rest of the document `aria-hidden` instead — the approach
    // with better assistive-tech support. Asserting `aria-modal` here would fail a correct dialog.
    expect(dialog.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog.getAttribute('aria-describedby')).toBeTruthy()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })
})

describe('ProjectDeleteDialog — cascade count', () => {
  it('names the cascade without a number while the count loads, then shows the real count — never "all 0 chats"', async () => {
    let resolveChats: (chats: unknown[]) => void = () => {}
    h.listProjectConversations.mockImplementation(
      () =>
        new Promise<unknown[]>((resolve) => {
          resolveChats = resolve
        }),
    )
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={() => {}} />)

    // While the count is in flight: numberless copy, and crucially NOT "all 0 chats".
    expect(screen.getByText(/all of its chats/i)).toBeTruthy()
    expect(screen.queryByText(/all 0 chats/i)).toBeNull()

    await act(async () => {
      resolveChats([{}, {}, {}])
      await Promise.resolve()
    })

    expect(screen.getByText(/all 3 chats/i)).toBeTruthy()
  })
})

describe('ProjectDeleteDialog — the count must never overstate certainty', () => {
  it('says "200 or more" when the count lands on the server row cap', async () => {
    // GET /v1/conversations caps at 200 rows with no cursor, so exactly-200 means "at least
    // 200". Quoting it as a total would state a falsehood right before an irreversible cascade.
    h.listProjectConversations.mockResolvedValue(Array.from({ length: 200 }, (_, i) => ({ id: `c${i}` })))
    render(<ProjectDeleteDialog project={project} onClose={vi.fn()} onConfirm={vi.fn()} />)
    expect(await screen.findByText(/all 200 or more of its chats/i)).toBeTruthy()
  })

  it('states an exact count below the cap', async () => {
    h.listProjectConversations.mockResolvedValue([{ id: 'c1' }, { id: 'c2' }, { id: 'c3' }])
    render(<ProjectDeleteDialog project={project} onClose={vi.fn()} onConfirm={vi.fn()} />)
    expect(await screen.findByText(/all 3 chats/i)).toBeTruthy()
  })
})

describe('ProjectDeleteDialog — the irreversible warning', () => {
  const WARNING = /the database and files behind the app are destroyed permanently/i

  it('warns about the database on the zero-chat branch, which names nothing else', async () => {
    // The branch that matters most: a project owns its own database from creation, before an app
    // or a single chat, so a project with no chats can still be holding everything ever stored.
    h.listProjectConversations.mockResolvedValue([])
    render(<ProjectDeleteDialog project={project} onClose={vi.fn()} onConfirm={vi.fn()} />)
    expect(await screen.findByText(/This deletes the project and its app\./i)).toBeTruthy()
    expect(screen.getByText(WARNING)).toBeTruthy()
  })

  it('warns on the numberless in-flight branch too', async () => {
    h.listProjectConversations.mockImplementation(() => new Promise<unknown[]>(() => {}))
    render(<ProjectDeleteDialog project={project} onClose={vi.fn()} onConfirm={vi.fn()} />)
    expect(screen.getByText(WARNING)).toBeTruthy()
  })

  it('warns alongside a resolved count', async () => {
    h.listProjectConversations.mockResolvedValue([{ id: 'c1' }, { id: 'c2' }])
    render(<ProjectDeleteDialog project={project} onClose={vi.fn()} onConfirm={vi.fn()} />)
    expect(await screen.findByText(/all 2 chats/i)).toBeTruthy()
    expect(screen.getByText(WARNING)).toBeTruthy()
  })

  it('warns at the cap', async () => {
    h.listProjectConversations.mockResolvedValue(Array.from({ length: 200 }, (_, i) => ({ id: `c${i}` })))
    render(<ProjectDeleteDialog project={project} onClose={vi.fn()} onConfirm={vi.fn()} />)
    expect(await screen.findByText(/all 200 or more of its chats/i)).toBeTruthy()
    expect(screen.getByText(WARNING)).toBeTruthy()
  })
})

describe('★ the reason field says what it wants, and how close you are', () => {
  it('describes the textarea with the rule AND the running count', async () => {
    // The field carried only `aria-label`, so a reader heard the question and neither of the
    // two facts printed directly under the box: the 5-50 word bound, and how many words they
    // have. `DialogContent`'s own `aria-describedby` stays on the cascade sentence — that is
    // what a reader should hear after the title — so this is a second association on a second
    // element, not a move.
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={vi.fn()} />)

    const field = await screen.findByLabelText(/why are you deleting/i)
    const described = (field.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)
    expect(described.length).toBe(2)

    const text = described.map((id) => document.getElementById(id)?.textContent || '').join(' ')
    expect(text).toMatch(/between 5 and 50 words/i)
    expect(text).toMatch(/0\/50 words/)
  })

  it('the described count is the live one, not a static copy', async () => {
    // LIVENESS: without this the association could point at a fossil and still pass.
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={vi.fn()} />)
    const field = await screen.findByLabelText(/why are you deleting/i)
    const countId = (field.getAttribute('aria-describedby') || '').split(/\s+/)[1]

    fireEvent.change(field, { target: { value: 'one two three four five six' } })

    await waitFor(() => expect(document.getElementById(countId!)?.textContent).toMatch(/6\/50 words/))
  })
})
