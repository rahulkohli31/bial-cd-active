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

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProjectDescriptionEditor — generate', () => {
  it('replaces the textarea with the returned description (clean field, no save-first)', async () => {
    h.generateDescription.mockResolvedValue(makeProject({ description: 'Generated summary of the app.' }))
    render(<ProjectDescriptionEditor projectId="p1" description="old stored text" onProjectUpdate={vi.fn()} />)

    fireEvent.click(generateBtn())

    await waitFor(() => expect(textarea().value).toBe('Generated summary of the app.'))
    expect(h.patchProject).not.toHaveBeenCalled() // field was not dirty → no pre-save
  })

  it('with a DIRTY field, PATCHes the typed text FIRST and only then generates', async () => {
    h.patchProject.mockResolvedValue(makeProject({ description: 'unsaved user text' }))
    h.generateDescription.mockResolvedValue(makeProject({ description: 'server generated text' }))
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)

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

    fireEvent.click(generateBtn())

    expect(await screen.findByText(/daily token limit/i)).toBeTruthy()
    expect(textarea().value).toBe('stored text')
  })

  it('503 → "unavailable"; textarea unchanged', async () => {
    h.generateDescription.mockRejectedValue(new ApiError('nope', 503))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    fireEvent.click(generateBtn())

    expect(await screen.findByText(/unavailable/i)).toBeTruthy()
    expect(textarea().value).toBe('stored text')
  })

  it('500 → "Generation failed. Try again."; textarea unchanged', async () => {
    h.generateDescription.mockRejectedValue(new ApiError('boom', 500))
    render(<ProjectDescriptionEditor projectId="p1" description="stored text" onProjectUpdate={vi.fn()} />)

    fireEvent.click(generateBtn())

    expect(await screen.findByText(/generation failed\. try again/i)).toBeTruthy()
    expect(textarea().value).toBe('stored text')
  })

  it('disables the textarea while Generate is in flight and re-enables it on SUCCESS', async () => {
    const d = deferred<Project>()
    h.generateDescription.mockReturnValue(d.promise)
    render(<ProjectDescriptionEditor projectId="p1" description="stored" onProjectUpdate={vi.fn()} />)

    fireEvent.click(generateBtn())
    await waitFor(() => expect(textarea().disabled).toBe(true))
    expect(generateBtn().disabled).toBe(true)
    expect(saveBtn().disabled).toBe(true)
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

    fireEvent.change(textarea(), { target: { value: long } })
    expect(saveBtn().disabled).toBe(false)
    fireEvent.click(saveBtn())

    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { description: long }))
  })

  it('blocks a 2001-character description client-side — no request fires', () => {
    render(<ProjectDescriptionEditor projectId="p1" description={null} onProjectUpdate={vi.fn()} />)

    fireEvent.change(textarea(), { target: { value: 'x'.repeat(2001) } })
    expect(saveBtn().disabled).toBe(true)
    fireEvent.click(saveBtn())

    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('clearing to whitespace saves description:null and then shows the placeholder', async () => {
    h.patchProject.mockResolvedValue(makeProject({ description: null }))
    render(<ProjectDescriptionEditor projectId="p1" description="something meaningful" onProjectUpdate={vi.fn()} />)

    fireEvent.change(textarea(), { target: { value: '   ' } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { description: null }))
    await waitFor(() => {
      const ta = textarea()
      expect(ta.value).toBe('')
      expect(ta.placeholder).toBe('No description yet')
    })
  })
})
