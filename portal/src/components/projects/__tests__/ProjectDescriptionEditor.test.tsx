/**
 * ProjectDescriptionEditor (U5): the two load-bearing behaviours are
 *   1. "Generate saves first" — a dirty field is PATCHed BEFORE generate, so the
 *      model revises what the user sees rather than a stale stored copy (R19); and
 *   2. every failure leaves the field untouched (never optimistically cleared).
 * Plus the length gate, the whitespace→null clear, and the disable-during-billing lock.
 *
 * projectApi is mocked at the module boundary so we control patch/generate timing
 * and assert exact call order; ApiError is the real class so `instanceof` narrows.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import ProjectDescriptionEditor from '../ProjectDescriptionEditor'
import { ApiError } from '../../../utils/apiError'
import type { Project } from '../../../utils/projectApi'

const h = vi.hoisted(() => ({
  patchProject: vi.fn(),
  generateDescription: vi.fn(),
}))

vi.mock('../../../utils/projectApi', () => ({
  patchProject: h.patchProject,
  generateDescription: h.generateDescription,
}))

const makeProject = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'VIP Movement',
  description: null,
  appId: null,
  appStatus: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
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
const generateBtn = () => screen.getByRole('button', { name: /generate/i }) as HTMLButtonElement
const saveBtn = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
const cancelBtn = () => screen.getByRole('button', { name: /cancel/i }) as HTMLButtonElement
const editBtn = () => screen.getByRole('button', { name: /edit/i }) as HTMLButtonElement
const dialog = () => screen.queryByRole('dialog')
/** Open the pop-up editor — every Save/Generate/Cancel interaction now happens inside it. */
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

  it('Cancel closes the pop-up WITHOUT saving and discards unsaved typing', () => {
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    openEditor()
    fireEvent.change(textarea(), { target: { value: 'unsaved edit' } })
    fireEvent.click(cancelBtn())

    expect(h.patchProject).not.toHaveBeenCalled()
    expect(dialog()).toBeNull()
    expect(screen.getByText('stored text')).toBeTruthy()

    // Reopening shows the original, not the discarded edit.
    openEditor()
    expect(textarea().value).toBe('stored text')
  })

  it('Save persists the text AND closes the pop-up on success', async () => {
    const onProjectUpdate = vi.fn()
    h.patchProject.mockResolvedValue(makeProject({ description: 'edited text' }))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={onProjectUpdate} />)

    openEditor()
    fireEvent.change(textarea(), { target: { value: 'edited text' } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(dialog()).toBeNull())
    expect(h.patchProject).toHaveBeenCalledWith('p1', { description: 'edited text' })
    expect(screen.getByText('edited text')).toBeTruthy()
  })

  it('a failed Save leaves the pop-up open with the typed text intact', async () => {
    h.patchProject.mockRejectedValue(new ApiError('boom', 500))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    openEditor()
    fireEvent.change(textarea(), { target: { value: 'edited text' } })
    fireEvent.click(saveBtn())

    expect(await screen.findByText('boom')).toBeTruthy()
    expect(dialog()).toBeTruthy()
    expect(textarea().value).toBe('edited text')
  })
})

describe('ProjectDescriptionEditor — generate', () => {
  it('replaces the textarea with the returned description (clean field, no save-first)', async () => {
    h.generateDescription.mockResolvedValue(makeProject({ description: 'Generated summary of the app.' }))
    render(<ProjectDescriptionEditor projectId="p1" description="old stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.click(generateBtn())

    await waitFor(() => expect(textarea().value).toBe('Generated summary of the app.'))
    expect(h.patchProject).not.toHaveBeenCalled() // field was not dirty → no pre-save
  })

  it('with a DIRTY field, PATCHes the typed text FIRST and only then generates', async () => {
    h.patchProject.mockResolvedValue(makeProject({ description: 'unsaved user text' }))
    h.generateDescription.mockResolvedValue(makeProject({ description: 'server generated text' }))
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value: 'unsaved user text' } })
    fireEvent.click(generateBtn())

    await waitFor(() => expect(h.generateDescription).toHaveBeenCalledTimes(1))
    // The PATCH carried the user's UNSAVED text...
    expect(h.patchProject).toHaveBeenCalledWith('p1', { description: 'unsaved user text' })
    // ...and it happened BEFORE generate — order, not merely "both happened".
    expect(h.patchProject.mock.invocationCallOrder[0]).toBeLessThan(
      h.generateDescription.mock.invocationCallOrder[0],
    )
    await waitFor(() => expect(textarea().value).toBe('server generated text'))
  })

  it('409 → "build the app first"; textarea unchanged; Generate re-enabled', async () => {
    h.generateDescription.mockRejectedValue(new ApiError('No app code yet.', 409))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.click(generateBtn())

    expect(await screen.findByText(/build the app first/i)).toBeTruthy()
    expect(textarea().value).toBe('stored text')
    expect(generateBtn().disabled).toBe(false)
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('429 → surfaces the daily-limit message the ApiError carries; textarea unchanged', async () => {
    h.generateDescription.mockRejectedValue(
      new ApiError('You have reached your daily token limit.', 429, 'daily_token_limit_exceeded'),
    )
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.click(generateBtn())

    expect(await screen.findByText(/daily token limit/i)).toBeTruthy()
    expect(textarea().value).toBe('stored text')
  })

  it('503 → "unavailable"; textarea unchanged', async () => {
    h.generateDescription.mockRejectedValue(new ApiError('nope', 503))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.click(generateBtn())

    expect(await screen.findByText(/unavailable/i)).toBeTruthy()
    expect(textarea().value).toBe('stored text')
  })

  it('500 → "Generation failed. Try again."; textarea unchanged', async () => {
    h.generateDescription.mockRejectedValue(new ApiError('boom', 500))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.click(generateBtn())

    expect(await screen.findByText(/generation failed\. try again/i)).toBeTruthy()
    expect(textarea().value).toBe('stored text')
  })

  it('disables the textarea while Generate is in flight and re-enables it on SUCCESS', async () => {
    const d = deferred<Project>()
    h.generateDescription.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.click(generateBtn())
    await waitFor(() => expect(textarea().disabled).toBe(true))
    expect(generateBtn().disabled).toBe(true)
    expect(saveBtn().disabled).toBe(true)
    expect(cancelBtn().disabled).toBe(true)
    expect(screen.getByRole('status')).toBeTruthy() // "a model call is running"

    await act(async () => {
      d.resolve(makeProject({ description: 'done' }))
      await Promise.resolve()
    })

    await waitFor(() => expect(textarea().disabled).toBe(false))
  })

  it('re-enables the textarea when Generate FAILS', async () => {
    const d = deferred<Project>()
    h.generateDescription.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.click(generateBtn())
    await waitFor(() => expect(textarea().disabled).toBe(true))

    await act(async () => {
      d.reject(new ApiError('boom', 500))
      await Promise.resolve()
    })

    await waitFor(() => expect(textarea().disabled).toBe(false))
    expect(await screen.findByText(/generation failed/i)).toBeTruthy()
  })
})

describe('ProjectDescriptionEditor — save + length gate', () => {
  it('saves a description of exactly 2000 characters', async () => {
    const long = 'x'.repeat(2000)
    h.patchProject.mockResolvedValue(makeProject({ description: long }))
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value: long } })
    expect(saveBtn().disabled).toBe(false)
    fireEvent.click(saveBtn())

    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { description: long }))
  })

  it('blocks a 2001-character description client-side — no request fires', () => {
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value: 'x'.repeat(2001) } })
    expect(saveBtn().disabled).toBe(true)
    fireEvent.click(saveBtn())

    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('clearing to whitespace saves description:null, closes the pop-up, and the read view shows the placeholder', async () => {
    h.patchProject.mockResolvedValue(makeProject({ description: null }))
    render(<ProjectDescriptionEditor projectId="p1" description="something meaningful" onProjectUpdate={vi.fn()} />)
    openEditor()

    fireEvent.change(textarea(), { target: { value: '   ' } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { description: null }))
    await waitFor(() => expect(dialog()).toBeNull())
    expect(screen.getByText('No description yet')).toBeTruthy()
  })
})

describe('ProjectDescriptionEditor — a dirty save survives a failed generate', () => {
  it('lifts the SAVED project to the parent even when generation then fails', async () => {
    // The patch already landed on the server. If generation fails afterwards, the parent must
    // not go on believing the description is the one it held before the user typed.
    const onProjectUpdate = vi.fn()
    h.patchProject.mockResolvedValue(makeProject({ description: 'typed by hand' }))
    h.generateDescription.mockRejectedValue(new ApiError('boom', 500))

    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={onProjectUpdate} />)
    openEditor()
    fireEvent.change(screen.getByLabelText(/project description/i), { target: { value: 'typed by hand' } })
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))

    expect(await screen.findByText(/Generation failed\. Try again\./i)).toBeTruthy()
    expect(onProjectUpdate).toHaveBeenCalledWith(expect.objectContaining({ description: 'typed by hand' }))
    expect((screen.getByLabelText(/project description/i) as HTMLTextAreaElement).value).toBe('typed by hand')
  })
})
