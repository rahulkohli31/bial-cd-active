/**
 * ProjectDescriptionEditor (U5): the load-bearing behaviour is that every failure
 * leaves the field untouched (never optimistically cleared). Plus the required/word-bound
 * gate (#191 — 15-120 words, no clear-to-null any more), the character backstop, and the
 * disable-during-save lock.
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

const textarea = () => screen.getByRole('textbox', { name: /project description/i }) as HTMLTextAreaElement
const saveBtn = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
const cancelBtn = () => screen.getByRole('button', { name: /cancel/i }) as HTMLButtonElement
const editBtn = () => screen.getByRole('button', { name: /edit/i }) as HTMLButtonElement
const closeXBtn = () => screen.getByRole('button', { name: 'Close' }) as HTMLButtonElement
const dialog = () => screen.queryByRole('dialog')
/** The click-to-dismiss backdrop — the dialog's elder sibling inside the portaled wrapper. */
const backdrop = () => dialog()!.parentElement!.firstElementChild as HTMLElement
/** Open the pop-up editor — every Save/Cancel interaction now happens inside it. */
const openEditor = () => fireEvent.click(editBtn())

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProjectDescriptionEditor — read view and pop-up open/close', () => {
  it('shows the stored description as read-only text with an Edit button, no dialog by default', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="Handles VIP movement." onProjectUpdate={vi.fn()} />)

    expect(screen.getByText('Handles VIP movement.')).toBeTruthy()
    expect(screen.queryByRole('textbox', { name: /project description/i })).toBeNull()
    expect(dialog()).toBeNull()
    expect(editBtn()).toBeTruthy()
  })

  it('shows the placeholder when there is no stored description', () => {
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)
    expect(screen.getByText('No description yet')).toBeTruthy()
  })

  it('Edit opens a pop-up with a big editor pre-filled with the current description', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    openEditor()

    expect(dialog()).toBeTruthy()
    expect(textarea().value).toBe('stored text')
  })

  it('tells the author, at the write surface, that this becomes public catalog copy', () => {
    // `Project.description` was introduced as CHAT GROUNDING, and the marketplace (#145/#147)
    // republishes it verbatim org-wide and makes it full-text searchable. Nothing here said
    // so, and the notice belongs at the WRITE surface because that is the only place it can
    // change what someone types.
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    openEditor()

    const notice = screen.getByText(/becomes its listing in the Marketplace/i)
    expect(notice).toBeTruthy()
    // Both halves of the exposure: who can see it, and that the words are the search index.
    expect(notice.textContent).toMatch(/everyone at BIAL/i)
    expect(notice.textContent).toMatch(/searchable/i)
  })

  it('does not show the public-listing notice until the editor is actually open', () => {
    // The other direction, so the assertion above cannot pass by always rendering. The
    // read-only card is not a write surface, so the notice would be noise there.
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    expect(screen.queryByText(/becomes its listing in the Marketplace/i)).toBeNull()
    // Liveness: the card really did render, so this absence means something.
    expect(editBtn()).toBeTruthy()
  })

  it('Cancel closes the pop-up WITHOUT saving and discards unsaved typing', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    openEditor()
    fireEvent.change(textarea(), { target: { value: 'unsaved edit' } })
    fireEvent.click(cancelBtn())

    expect(h.patchProject).not.toHaveBeenCalled()
    expect(dialog()).toBeNull()
    expect(screen.getByText('stored text')).toBeTruthy()

    openEditor()
    expect(textarea().value).toBe('stored text')
  })

  it('Save persists the text AND closes the pop-up on success', async () => {
    const onProjectUpdate = vi.fn()
    const edited = wordsOf()
    h.patchProject.mockResolvedValue(makeProject({ description: edited }))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={onProjectUpdate} />)

    openEditor()
    fireEvent.change(textarea(), { target: { value: edited } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(dialog()).toBeNull())
    expect(h.patchProject).toHaveBeenCalledWith('p1', { description: edited })
    expect(screen.getByText(edited)).toBeTruthy()
  })

  it('a failed Save leaves the pop-up open with the typed text intact', async () => {
    h.patchProject.mockRejectedValue(new ApiError('boom', 500))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    const edited = wordsOf()
    openEditor()
    fireEvent.change(textarea(), { target: { value: edited } })
    fireEvent.click(saveBtn())

    expect(await screen.findByText('boom')).toBeTruthy()
    expect(dialog()).toBeTruthy()
    expect(textarea().value).toBe(edited)
  })
})

describe('ProjectDescriptionEditor — busy state (Save)', () => {
  // These two used to hold a `generateDescription` promise open to reach "busy" — Generate
  // is gone (#191), so Save is now the only request that can put the surface in that state.
  // Nothing here asserts anything Generate-specific; it never did — the point was always
  // that a busy request disables every control, not what produced the busy state.
  it('disables the textarea while Save is in flight and re-enables it on SUCCESS', async () => {
    const d = deferred<Project>()
    h.patchProject.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: wordsOf() } })

    fireEvent.click(saveBtn())
    await waitFor(() => expect(textarea().disabled).toBe(true))
    expect(saveBtn().disabled).toBe(true)
    expect(cancelBtn().disabled).toBe(true)

    await act(async () => {
      d.resolve(makeProject({ description: wordsOf() }))
      await Promise.resolve()
    })

    await waitFor(() => expect(dialog()).toBeNull())
  })

  it('re-enables the textarea when Save FAILS', async () => {
    const d = deferred<Project>()
    h.patchProject.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: wordsOf() } })

    fireEvent.click(saveBtn())
    await waitFor(() => expect(textarea().disabled).toBe(true))

    await act(async () => {
      d.reject(new ApiError('boom', 500))
      await Promise.resolve()
    })

    await waitFor(() => expect(textarea().disabled).toBe(false))
    expect(await screen.findByText('boom')).toBeTruthy()
  })
})

describe('ProjectDescriptionEditor — character backstop', () => {
  it('saves a description inside the character cap and inside the word bound', async () => {
    // 100 words of 15 characters + 99 separating spaces = 1599 characters, well under the
    // 2000 cap, and 100 words is inside 15-120 — the word rule and the char rule agree here.
    const value = Array.from({ length: 100 }, () => 'x'.repeat(15)).join(' ')
    h.patchProject.mockResolvedValue(makeProject({ description: value }))
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value } })
    expect(saveBtn().disabled).toBe(false)
    fireEvent.click(saveBtn())

    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { description: value }))
  })

  it('blocks an over-2000-character description client-side even though the word count is in bounds', () => {
    // 100 words of 20 characters + 99 spaces = 2099 characters — over the cap, but still only
    // 100 words (inside 15-120), so this is the character rule firing, not the word rule.
    const value = Array.from({ length: 100 }, () => 'x'.repeat(20)).join(' ')
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value } })
    expect(saveBtn().disabled).toBe(true)
    fireEvent.click(saveBtn())

    expect(h.patchProject).not.toHaveBeenCalled()
  })
})

describe('ProjectDescriptionEditor — required + word bound (#191)', () => {
  it('Save is disabled with fewer than the minimum words', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value: wordsOf(MIN_PROJECT_DESCRIPTION_WORDS - 1) } })

    expect(saveBtn().disabled).toBe(true)
    fireEvent.click(saveBtn())
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('Save is enabled at exactly the minimum word count', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value: wordsOf(MIN_PROJECT_DESCRIPTION_WORDS) } })

    expect(saveBtn().disabled).toBe(false)
  })

  it('Save is enabled at exactly the maximum word count', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value: wordsOf(MAX_PROJECT_DESCRIPTION_WORDS) } })

    expect(saveBtn().disabled).toBe(false)
  })

  it('Save is disabled one word past the maximum', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value: wordsOf(MAX_PROJECT_DESCRIPTION_WORDS + 1) } })

    expect(saveBtn().disabled).toBe(true)
  })

  it('a whitespace-only description no longer clears to null — it is simply invalid', () => {
    // #191 R11: description can no longer be cleared. Blank is a write that fails the
    // required/word-bound rule, not a special "clear" request the way it used to be.
    render(
      <ProjectDescriptionEditor
        projectId="p1"
        description="something meaningful"
        onProjectUpdate={vi.fn()}
      />,
    )
    openEditor()

    fireEvent.change(textarea(), { target: { value: '   ' } })

    expect(saveBtn().disabled).toBe(true)
    fireEvent.click(saveBtn())
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('shows the word-bound rule and a live counter that turns red out of bounds', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    expect(screen.getByText(/between 15 and 120 words/i)).toBeTruthy()

    fireEvent.change(textarea(), { target: { value: wordsOf(3) } })
    const counter = screen.getByText('3/120 words')
    expect(counter.className).toMatch(/text-danger/)

    fireEvent.change(textarea(), { target: { value: wordsOf(MIN_PROJECT_DESCRIPTION_WORDS) } })
    const validCounter = screen.getByText(`${MIN_PROJECT_DESCRIPTION_WORDS}/120 words`)
    expect(validCounter.className).not.toMatch(/text-danger/)
  })

  it('a project with no prior description (R14) still opens and closes normally', () => {
    // The grandfather clause: a pre-#191 project with nothing saved is not forced to add
    // one just to open the editor — only to SAVE it does the bound apply.
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)
    openEditor()

    expect(dialog()).toBeTruthy()
    expect(textarea().value).toBe('')
    expect(saveBtn().disabled).toBe(true) // 0 words — cannot save empty, but CAN close

    fireEvent.click(cancelBtn())
    expect(dialog()).toBeNull()
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('the same project can add a description once it clears the bar', async () => {
    const value = wordsOf()
    h.patchProject.mockResolvedValue(makeProject({ description: value }))
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { description: value }))
  })
})

// 405a1d6 (Escape-to-close + focus trap) added NO tests, which is exactly how its trap half
// went out as a no-op: nothing here fired a keydown against the dialog, so "843/843 passing"
// read as confirmation of a fix that wasn't one. These pin the actual contract — Escape and Tab
// containment hold even once a busy request disables every other focusable in the dialog.
describe('ProjectDescriptionEditor — keyboard + focus (405a1d6 regression)', () => {
  it('focuses the textarea as soon as the pop-up opens', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()
    expect(document.activeElement).toBe(textarea())
  })

  it('Escape closes like Cancel — discards unsaved typing and restores focus to Edit', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: 'unsaved edit' } })

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

    expect(dialog()).toBeNull()
    expect(h.patchProject).not.toHaveBeenCalled()
    expect(screen.getByText('stored text')).toBeTruthy()
    expect(document.activeElement).toBe(editBtn())
  })

  it('the backdrop click closes and discards unsaved typing', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: 'unsaved edit' } })

    fireEvent.click(backdrop())

    expect(dialog()).toBeNull()
    expect(h.patchProject).not.toHaveBeenCalled()
    expect(screen.getByText('stored text')).toBeTruthy()
  })

  it('the X button closes and discards unsaved typing', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: 'unsaved edit' } })

    fireEvent.click(closeXBtn())

    expect(dialog()).toBeNull()
    expect(h.patchProject).not.toHaveBeenCalled()
    expect(screen.getByText('stored text')).toBeTruthy()
  })

  it('Tab wraps from the last focusable (Cancel) back to the first (the X button)', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()
    cancelBtn().focus()

    fireEvent.keyDown(cancelBtn(), { key: 'Tab' })

    expect(document.activeElement).toBe(closeXBtn())
  })

  it('Shift+Tab wraps from the first focusable (the X button) to the last (Cancel)', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()
    closeXBtn().focus()

    fireEvent.keyDown(closeXBtn(), { key: 'Tab', shiftKey: true })

    expect(document.activeElement).toBe(cancelBtn())
  })

  it('restores focus to the Edit button after Cancel', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.click(cancelBtn())

    expect(document.activeElement).toBe(editBtn())
  })

  it('restores focus to the Edit button after a SUCCESSFUL Save (Save was the one close path that skipped it)', async () => {
    const onProjectUpdate = vi.fn()
    h.patchProject.mockResolvedValue(makeProject({ description: wordsOf() }))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={onProjectUpdate} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: wordsOf() } })

    fireEvent.click(saveBtn())

    await waitFor(() => expect(dialog()).toBeNull())
    expect(document.activeElement).toBe(editBtn())
  })

  it('a busy request moves focus onto the dialog card itself, so Tab has somewhere to stay contained', async () => {
    // THE POINT: excluding the disabled textarea/buttons from the focusable query is what
    // CAUSES focusables.length to hit 0 during a busy request — mutation check: reverting the
    // tabIndex/focus-on-busy fix makes this assertion fail (document.activeElement falls to
    // <body> instead), confirming this test actually catches the regression 405a1d6 missed.
    // Generate is gone (#191) — Save is now the only request that can hold this busy.
    const d = deferred<Project>()
    h.patchProject.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: wordsOf() } })

    fireEvent.click(saveBtn())
    await waitFor(() => expect(textarea().disabled).toBe(true))

    expect(document.activeElement).toBe(screen.getByRole('dialog'))

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('dialog')) // still inside — never <body>

    await act(async () => {
      d.resolve(makeProject({ description: wordsOf() }))
      await Promise.resolve()
    })
  })

  it('Escape does NOT close while a request is in flight (the busy guard, exercised directly rather than via disabled)', async () => {
    const d = deferred<Project>()
    h.patchProject.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: wordsOf() } })

    fireEvent.click(saveBtn())
    await waitFor(() => expect(textarea().disabled).toBe(true))

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(dialog()).toBeTruthy()

    await act(async () => {
      d.resolve(makeProject({ description: wordsOf() }))
      await Promise.resolve()
    })
  })

  it('the backdrop click does NOT close while a request is in flight', async () => {
    const d = deferred<Project>()
    h.patchProject.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)
    openEditor()
    fireEvent.change(textarea(), { target: { value: wordsOf() } })

    fireEvent.click(saveBtn())
    await waitFor(() => expect(textarea().disabled).toBe(true))

    fireEvent.click(backdrop())
    expect(dialog()).toBeTruthy()

    await act(async () => {
      d.resolve(makeProject({ description: wordsOf() }))
      await Promise.resolve()
    })
  })
})
