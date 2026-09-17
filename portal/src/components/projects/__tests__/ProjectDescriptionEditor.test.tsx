/**
 * ProjectDescriptionEditor: the field itself is the write surface — always editable, with
 * Save and Cancel appearing only once there is something to save. The load-bearing behaviour
 * is that every failure leaves the typed text exactly as the user left it, never cleared and
 * never reverted. Plus the required/word-bound gate (#191 — 15-120 words, no clear-to-null),
 * the character backstop, the disable-during-save lock, and the parent re-sync that must not
 * clobber in-progress typing.
 *
 * projectApi is mocked at the module boundary so we control patch timing; ApiError
 * is the real class so `instanceof` narrows.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import ProjectDescriptionEditor from '../ProjectDescriptionEditor'
import { ApiError } from '../../../utils/apiError'
import type { Project } from '../../../utils/projectApi'
import { MIN_PROJECT_DESCRIPTION_WORDS, MAX_PROJECT_DESCRIPTION_WORDS } from '../../../utils/words'

const h = vi.hoisted(() => ({
  patchProject: vi.fn(),
}))

vi.mock('../../../utils/projectApi', () => ({
  patchProject: h.patchProject,
}))

/** A description of exactly `n` words — the shape Save actually requires (#191). Defaults
 *  to the minimum, since most tests below only need SOME value that clears the bar. */
const wordsOf = (n: number = MIN_PROJECT_DESCRIPTION_WORDS): string =>
  Array.from({ length: n }, (_, i) => `w${i}`).join(' ')

const makeProject = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'VIP Movement',
  description: null,
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
  ...over,
})

/** A promise whose settle we drive by hand, to hold an operation "in flight". */
function deferred<T>() {
  let resolve: (value: T) => void = () => {}
  let reject: (reason: unknown) => void = () => {}
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const textarea = () => screen.getByRole('textbox', { name: /what should this app do/i }) as HTMLTextAreaElement
const saveBtn = () => screen.getByTestId('project-description-save') as HTMLButtonElement
const cancelBtn = () => screen.getByTestId('project-description-revert') as HTMLButtonElement
const type = (value: string) => fireEvent.change(textarea(), { target: { value } })

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProjectDescriptionEditor — the field is the write surface', () => {
  it('puts the stored description straight into an editable field, with nothing to open first', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="Handles VIP movement." onProjectUpdate={vi.fn()} />)

    expect(textarea().value).toBe('Handles VIP movement.')
    expect(textarea().disabled).toBe(false)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
  })

  it('a project with no prior description (R14) still opens, on an empty field with the create form’s prompt', () => {
    // The grandfather clause: a pre-#191 project with nothing saved is not forced to add one
    // just to reach the field — only to SAVE it does the bound apply.
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)

    expect(textarea().value).toBe('')
    expect(textarea().placeholder).toBe('Who uses it, and what do they do with it?')
    expect(screen.getByText('0/120 words')).toBeTruthy()
  })

  it('★ draws no heading of its own — the form that holds it names it', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)

    expect(screen.queryByRole('heading')).toBeNull()
    // LIVENESS beside the absence: the editor really rendered, and its field holds the value.
    expect(textarea().value).toBe('stored')
  })
})

describe('ProjectDescriptionEditor — the controls appear because there is something to do', () => {
  it('offers neither Save nor Cancel while the text still matches what is stored', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    expect(screen.queryByTestId('project-description-save')).toBeNull()
    expect(screen.queryByTestId('project-description-revert')).toBeNull()
    // LIVENESS: the field is there and editable, so the absence above is a decision, not a crash.
    expect(textarea().value).toBe('stored text')
  })

  it('reveals both controls as soon as the text differs', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf())

    expect(saveBtn().textContent).toBe('Save')
    expect(cancelBtn().textContent).toBe('Cancel')
  })

  it('withdraws them again when the text is typed back to the stored value', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf())
    type('stored text')

    expect(screen.queryByTestId('project-description-save')).toBeNull()
    expect(screen.queryByTestId('project-description-revert')).toBeNull()
    expect(textarea().value).toBe('stored text')
  })

  it('Cancel restores the stored text without saving', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type('unsaved edit')
    fireEvent.click(cancelBtn())

    expect(textarea().value).toBe('stored text')
    expect(h.patchProject).not.toHaveBeenCalled()
    expect(screen.queryByTestId('project-description-save')).toBeNull()
  })
})

describe('ProjectDescriptionEditor — saving', () => {
  it('sends the trimmed text and then adopts the server’s canonical copy', async () => {
    const onProjectUpdate = vi.fn()
    const typed = wordsOf()
    const canonical = wordsOf(20)
    const updated = makeProject({ description: canonical })
    h.patchProject.mockResolvedValue(updated)
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={onProjectUpdate} />)

    type(`  ${typed}  `)
    fireEvent.click(saveBtn())

    await waitFor(() => expect(textarea().value).toBe(canonical))
    expect(h.patchProject).toHaveBeenCalledWith('p1', { description: typed })
    expect(onProjectUpdate).toHaveBeenCalledWith(updated)
  })

  it('locks the field and both controls while the save is in flight, and releases them on success', async () => {
    const d = deferred<Project>()
    h.patchProject.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    type(wordsOf())

    fireEvent.click(saveBtn())

    await waitFor(() => expect(textarea().disabled).toBe(true))
    expect(saveBtn().textContent).toBe('Saving…')
    expect(saveBtn().disabled).toBe(true)
    expect(cancelBtn().disabled).toBe(true)

    await act(async () => {
      d.resolve(makeProject({ description: wordsOf() }))
      await Promise.resolve()
    })

    await waitFor(() => expect(textarea().disabled).toBe(false))
    expect(saveBtn().textContent).toBe('Save')
  })
})

describe('ProjectDescriptionEditor — a failed save leaves the field exactly as typed', () => {
  it('keeps the typed text and shows what the API refused', async () => {
    h.patchProject.mockRejectedValue(new ApiError('Description must be at least 15 words.', 422))
    const typed = wordsOf()
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(typed)
    fireEvent.click(saveBtn())

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('Description must be at least 15 words.')
    expect(textarea().value).toBe(typed)
    expect(textarea().disabled).toBe(false)
  })

  it('says something a citizen can act on when the failure carries no API message', async () => {
    h.patchProject.mockRejectedValue(new Error('socket hang up'))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf())
    fireEvent.click(saveBtn())

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('Could not save. Try again.')
  })

  it('clears the error the moment the text changes — it described a write nobody is attempting any more', async () => {
    h.patchProject.mockRejectedValue(new ApiError('boom', 500))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf())
    fireEvent.click(saveBtn())
    expect(await screen.findByRole('alert')).toBeTruthy()

    type(wordsOf(20))

    expect(screen.queryByRole('alert')).toBeNull()
    expect(textarea().value).toBe(wordsOf(20))
  })

  it('Cancel clears the error as well as the text', async () => {
    h.patchProject.mockRejectedValue(new ApiError('boom', 500))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf())
    fireEvent.click(saveBtn())
    expect(await screen.findByRole('alert')).toBeTruthy()

    fireEvent.click(cancelBtn())

    expect(screen.queryByRole('alert')).toBeNull()
    expect(textarea().value).toBe('stored text')
  })
})

describe('ProjectDescriptionEditor — character backstop', () => {
  it('saves a description inside the character cap and inside the word bound', async () => {
    // 100 words of 15 characters + 99 separating spaces = 1599 characters, well under the
    // 2000 cap, and 100 words is inside 15-120 — the word rule and the char rule agree here.
    const value = Array.from({ length: 100 }, () => 'x'.repeat(15)).join(' ')
    h.patchProject.mockResolvedValue(makeProject({ description: value }))
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)

    type(value)
    expect(saveBtn().disabled).toBe(false)
    fireEvent.click(saveBtn())

    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { description: value }))
  })

  it('blocks an over-2000-character description client-side even though the word count is in bounds', () => {
    // 100 words of 20 characters + 99 spaces = 2099 characters — over the cap, but still only
    // 100 words (inside 15-120), so this is the character rule firing, not the word rule.
    const value = Array.from({ length: 100 }, () => 'x'.repeat(20)).join(' ')
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)

    type(value)

    expect(saveBtn().disabled).toBe(true)
    fireEvent.click(saveBtn())
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('stops typing at the cap, so only a paste can reach the blocked state above', () => {
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)

    expect(textarea().maxLength).toBe(2000)
  })
})

describe('ProjectDescriptionEditor — required + word bound (#191)', () => {
  it('Save is disabled with fewer than the minimum words', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf(MIN_PROJECT_DESCRIPTION_WORDS - 1))

    expect(saveBtn().disabled).toBe(true)
    fireEvent.click(saveBtn())
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('Save is enabled at exactly the minimum word count', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf(MIN_PROJECT_DESCRIPTION_WORDS))

    expect(saveBtn().disabled).toBe(false)
  })

  it('Save is enabled at exactly the maximum word count', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf(MAX_PROJECT_DESCRIPTION_WORDS))

    expect(saveBtn().disabled).toBe(false)
  })

  it('Save is disabled one word past the maximum', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf(MAX_PROJECT_DESCRIPTION_WORDS + 1))

    expect(saveBtn().disabled).toBe(true)
  })

  it('a whitespace-only description no longer clears to null — it is simply invalid', () => {
    // #191 R11: description can no longer be cleared. Blank is a write that fails the
    // required/word-bound rule, not a special "clear" request the way it used to be.
    render(<ProjectDescriptionEditor projectId="p1" description="something meaningful" onProjectUpdate={vi.fn()} />)

    type('   ')

    expect(saveBtn().disabled).toBe(true)
    fireEvent.click(saveBtn())
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('counts words live and turns the counter red out of bounds', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    type(wordsOf(3))
    expect(screen.getByText('3/120 words').className).toMatch(/text-danger/)

    type(wordsOf(MIN_PROJECT_DESCRIPTION_WORDS))
    expect(screen.getByText(`${MIN_PROJECT_DESCRIPTION_WORDS}/120 words`).className).not.toMatch(/text-danger/)
  })
})

describe('ProjectDescriptionEditor — the parent owns the stored value', () => {
  it('adopts a genuinely new description handed down from above', () => {
    const { rerender } = render(
      <ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />,
    )

    rerender(<ProjectDescriptionEditor projectId="p1" description="a newer stored text" onProjectUpdate={vi.fn()} />)

    expect(textarea().value).toBe('a newer stored text')
  })

  it('an unrelated re-render never clobbers in-progress typing', () => {
    // The parent re-renders for its own reasons — a sibling's state, a fresh callback identity
    // — and mid-sentence is the worst possible moment to lose what was typed. Two independent
    // guards hold it, the `[description]` dependency and the last-synced ref, so this goes red
    // only when the effect is rewritten to sync unconditionally, not when either one is dropped.
    const { rerender } = render(
      <ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />,
    )

    type('half a sentence so f')
    rerender(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    expect(textarea().value).toBe('half a sentence so f')
  })
})
