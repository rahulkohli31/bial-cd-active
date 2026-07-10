/**
 * ProjectDeleteDialog — the two guarantees a destructive confirm owes the user:
 *   1. The confirm button will not arm until the typed text matches the project
 *      name EXACTLY — a trailing space or wrong case is not a match.
 *   2. It names the cascade with a real chat count, and it never flashes
 *      "all 0 chats" while that count is still in flight — the numberless copy
 *      stands in until the count resolves.
 *
 * `listProjectConversations` is mocked at the module boundary so we can hold the
 * count fetch open and observe the loading-vs-resolved copy.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react'

const h = vi.hoisted(() => ({ listProjectConversations: vi.fn() }))
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
  createdAt: '',
  updatedAt: '',
}

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => cleanup())

describe('ProjectDeleteDialog — confirm gating', () => {
  it('arms confirm only when the typed name matches exactly (trailing space / wrong case do not count)', async () => {
    h.listProjectConversations.mockResolvedValue([{}, {}])
    const onConfirm = vi.fn()
    render(<ProjectDeleteDialog project={project} onClose={() => {}} onConfirm={onConfirm} />)

    // Flush the count effect so the resolving promise does not fire an un-acted update.
    expect(await screen.findByText(/all 2 chats/i)).toBeTruthy()

    const confirmBtn = screen.getByRole('button', { name: /delete project/i })
    const input = screen.getByLabelText(/type the project name/i)

    expect(confirmBtn.hasAttribute('disabled')).toBe(true) // nothing typed yet

    fireEvent.change(input, { target: { value: 'VIP Movement ' } }) // trailing space
    expect(confirmBtn.hasAttribute('disabled')).toBe(true)

    fireEvent.change(input, { target: { value: 'vip movement' } }) // wrong case
    expect(confirmBtn.hasAttribute('disabled')).toBe(true)

    fireEvent.change(input, { target: { value: 'VIP Movement' } }) // exact
    expect(confirmBtn.hasAttribute('disabled')).toBe(false)

    fireEvent.click(confirmBtn)
    expect(onConfirm).toHaveBeenCalledTimes(1)
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

    // Resolved: the real count is named.
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
