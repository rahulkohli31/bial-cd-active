/**
 * ProjectCreateModal (#191): description is now REQUIRED and word-bounded (15-120,
 * mirroring the name's existing 8-word cap), relabelled as a question, with a live
 * counter and a keyboard-focusable, click-toggle info control showing a worked example.
 * The character caps on both fields stay as paste backstops the button also enforces
 * belt-and-braces (a test can drive the handler even while the button is disabled).
 *
 * projectApi is mocked at the module boundary; ApiError is the real class where a test
 * needs `instanceof` to narrow, though these tests only ever see a caught Error's message.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import ProjectCreateModal from '../ProjectCreateModal'
import type { Project } from '../../../utils/projectApi'
import { MIN_PROJECT_DESCRIPTION_WORDS, MAX_PROJECT_DESCRIPTION_WORDS, MAX_PROJECT_NAME_WORDS } from '../../../utils/words'

const h = vi.hoisted(() => ({
  createProject: vi.fn(),
  checkDuplicateProjects: vi.fn(),
  reportDuplicateCheckResolution: vi.fn(),
}))

vi.mock('../../../utils/projectApi', () => ({
  createProject: h.createProject,
  checkDuplicateProjects: h.checkDuplicateProjects,
  reportDuplicateCheckResolution: h.reportDuplicateCheckResolution,
}))

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

/** A promise whose settle we drive by hand, to hold the create request "in flight". */
function deferred<T>() {
  let resolve: (value: T) => void = () => {}
  let reject: (reason: unknown) => void = () => {}
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** A description of exactly `n` words — what Save/Create actually requires (#191). */
const wordsOf = (n: number = MIN_PROJECT_DESCRIPTION_WORDS): string =>
  Array.from({ length: n }, (_, i) => `w${i}`).join(' ')

const nameInput = () => screen.getByPlaceholderText(/vip movement tracker/i) as HTMLInputElement
const descriptionInput = () =>
  screen.getByPlaceholderText(/who uses it, and what do they do with it/i) as HTMLTextAreaElement
const createBtn = () => screen.getByRole('button', { name: /create project/i }) as HTMLButtonElement
const cancelBtn = () => screen.getByRole('button', { name: /cancel/i }) as HTMLButtonElement
const infoBtn = () => screen.getByRole('button', { name: /show an example description/i }) as HTMLButtonElement

/** Fills both fields with values that clear every bound, so a test about ONE field's
 *  validation doesn't also have to fight the other's. */
function fillValid(name = 'Gate Pass Log'): void {
  fireEvent.change(nameInput(), { target: { value: name } })
  fireEvent.change(descriptionInput(), { target: { value: wordsOf() } })
}

beforeEach(() => {
  vi.clearAllMocks()
  // The default, day-one case (R31's fall-through path): no matches, so every EXISTING
  // test below — none of which is about the duplicate check — creates exactly as it did
  // before this slice, with no extra screen in the way.
  h.checkDuplicateProjects.mockResolvedValue([])
  // `reportResolution` always calls `.catch` on this — an unconfigured `vi.fn()` returns
  // `undefined`, not a promise, so every test that reaches a resolution report needs this
  // even when it isn't asserting on the call itself.
  h.reportDuplicateCheckResolution.mockResolvedValue(undefined)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProjectCreateModal — relabelled field (#191 R15, R18)', () => {
  it('labels the description as a question, not "Description (optional)"', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.getByText('What should this app do?')).toBeTruthy()
    expect(screen.queryByText('(optional)')).toBeNull()
  })

  it('placeholder no longer advertises the retired Generate feature', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.queryByPlaceholderText(/generate this later/i)).toBeNull()
    expect(descriptionInput()).toBeTruthy()
  })
})

describe('ProjectCreateModal — description required + word bound (#191)', () => {
  it('Create is disabled with no description at all', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.change(nameInput(), { target: { value: 'Gate Pass Log' } })

    expect(createBtn().disabled).toBe(true)
  })

  it('Create is disabled with fewer than the minimum words', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.change(nameInput(), { target: { value: 'Gate Pass Log' } })
    fireEvent.change(descriptionInput(), {
      target: { value: wordsOf(MIN_PROJECT_DESCRIPTION_WORDS - 1) },
    })

    expect(createBtn().disabled).toBe(true)
  })

  it('Create is enabled at exactly the minimum word count', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fillValid()
    expect(createBtn().disabled).toBe(false)
  })

  it('Create is enabled at exactly the maximum word count', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.change(nameInput(), { target: { value: 'Gate Pass Log' } })
    fireEvent.change(descriptionInput(), { target: { value: wordsOf(MAX_PROJECT_DESCRIPTION_WORDS) } })

    expect(createBtn().disabled).toBe(false)
  })

  it('Create is disabled one word past the maximum', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.change(nameInput(), { target: { value: 'Gate Pass Log' } })
    fireEvent.change(descriptionInput(), {
      target: { value: wordsOf(MAX_PROJECT_DESCRIPTION_WORDS + 1) },
    })

    expect(createBtn().disabled).toBe(true)
  })

  it('a whitespace-only description does not satisfy the requirement', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.change(nameInput(), { target: { value: 'Gate Pass Log' } })
    fireEvent.change(descriptionInput(), { target: { value: '     ' } })

    expect(createBtn().disabled).toBe(true)
  })

  it('shows the word-bound rule and a live counter that turns red out of bounds', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.getByText(/between 15 and 120 words/i)).toBeTruthy()

    fireEvent.change(descriptionInput(), { target: { value: wordsOf(3) } })
    const counter = screen.getByText('3/120 words')
    expect(counter.className).toMatch(/text-danger/)

    fireEvent.change(descriptionInput(), { target: { value: wordsOf(MIN_PROJECT_DESCRIPTION_WORDS) } })
    const validCounter = screen.getByText(`${MIN_PROJECT_DESCRIPTION_WORDS}/120 words`)
    expect(validCounter.className).not.toMatch(/text-danger/)
  })

  it('tells the citizen colleagues see this in the Marketplace and it checks for duplicates', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    const notice = screen.getByText(/colleagues see this in the marketplace/i)
    expect(notice.textContent).toMatch(/checks/i)
    expect(notice.textContent).toMatch(/already exists/i)
  })

  it('blocks an over-2000-character description client-side even though the word count is in bounds', () => {
    // 100 words of 20 characters + 99 spaces = 2099 characters — over the paste backstop,
    // but still only 100 words (inside 15-120), so this is the character rule, not the word one.
    const value = Array.from({ length: 100 }, () => 'x'.repeat(20)).join(' ')
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.change(nameInput(), { target: { value: 'Gate Pass Log' } })
    fireEvent.change(descriptionInput(), { target: { value } })

    expect(createBtn().disabled).toBe(true)
  })
})

describe('ProjectCreateModal — the info control (#191 R16)', () => {
  it('is reachable by keyboard and reveals the worked example without a pointer', async () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)

    expect(screen.queryByText(/ground staff log vip movement/i)).toBeNull()
    infoBtn().focus()
    expect(document.activeElement).toBe(infoBtn())

    fireEvent.click(infoBtn())

    expect(await screen.findByText(/ground staff log vip movement/i)).toBeTruthy()
  })
})

describe('ProjectCreateModal — submit', () => {
  it('creates with the trimmed name and the typed description', async () => {
    const onCreated = vi.fn()
    const description = wordsOf()
    h.createProject.mockResolvedValue(makeProject({ name: 'Gate Pass Log', description }))
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={onCreated} />)

    fireEvent.change(nameInput(), { target: { value: '  Gate Pass Log  ' } })
    fireEvent.change(descriptionInput(), { target: { value: description } })
    fireEvent.click(createBtn())

    await waitFor(() =>
      expect(h.createProject).toHaveBeenCalledWith({ name: 'Gate Pass Log', description }),
    )
    expect(onCreated).toHaveBeenCalled()
  })

  it('surfaces the server error message and re-enables the form on failure', async () => {
    h.createProject.mockRejectedValue(new Error('That description is too long. Keep it under 2000 characters.'))
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fillValid()

    fireEvent.click(createBtn())

    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'That description is too long. Keep it under 2000 characters.',
    )
    expect(createBtn().disabled).toBe(false)
  })

  it('disables the form while the request is in flight', async () => {
    const d = deferred<Project>()
    h.createProject.mockReturnValue(d.promise)
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fillValid()

    fireEvent.click(createBtn())
    await waitFor(() => expect(cancelBtn().disabled).toBe(true))

    await act(async () => {
      d.resolve(makeProject())
      await Promise.resolve()
    })
  })

  it('Cancel closes without creating anything', () => {
    const onClose = vi.fn()
    render(<ProjectCreateModal onClose={onClose} onCreated={vi.fn()} />)
    fillValid()

    fireEvent.click(cancelBtn())

    expect(onClose).toHaveBeenCalled()
    expect(h.createProject).not.toHaveBeenCalled()
  })
})

describe('ProjectCreateModal — name word cap (existing #158 behaviour, unaffected)', () => {
  it('Create is disabled one word past the name cap even with a valid description', () => {
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    const tooManyWords = Array.from({ length: MAX_PROJECT_NAME_WORDS + 1 }, (_, i) => `w${i}`).join(' ')
    fireEvent.change(nameInput(), { target: { value: tooManyWords } })
    fireEvent.change(descriptionInput(), { target: { value: wordsOf() } })

    expect(createBtn().disabled).toBe(true)
  })
})

describe('ProjectCreateModal — duplicate check before create (#191 slice 4, R31-R39)', () => {
  const oneMatch = [
    {
      name: 'Existing App',
      description: 'Does the same thing already.',
      builderDisplayName: 'Jane Builder',
      url: 'https://pub-example.azurecontainerapps.io/',
    },
  ]

  it('checks with the trimmed description before creating', async () => {
    h.createProject.mockResolvedValue(makeProject())
    const description = wordsOf()
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.change(nameInput(), { target: { value: 'Gate Pass Log' } })
    fireEvent.change(descriptionInput(), { target: { value: `  ${description}  ` } })

    fireEvent.click(createBtn())

    await waitFor(() => expect(h.checkDuplicateProjects).toHaveBeenCalledWith(description))
  })

  it('creates immediately when the check finds no matches — no extra screen appears', async () => {
    h.createProject.mockResolvedValue(makeProject())
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fillValid()

    fireEvent.click(createBtn())

    await waitFor(() => expect(h.createProject).toHaveBeenCalled())
    expect(screen.queryByText('This might already exist')).toBeNull()
  })

  it('shows the possible-duplicate screen instead of creating when the check finds a match', async () => {
    h.checkDuplicateProjects.mockResolvedValue(oneMatch)
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fillValid()

    fireEvent.click(createBtn())

    expect(await screen.findByText('This might already exist')).toBeTruthy()
    expect(screen.getByText('Existing App')).toBeTruthy()
    expect(screen.getByText('Built by Jane Builder')).toBeTruthy()
    expect(h.createProject).not.toHaveBeenCalled()
  })

  it('a match with no description falls back to "No description yet."', async () => {
    h.checkDuplicateProjects.mockResolvedValue([
      { name: 'Existing App', description: null, builderDisplayName: null, url: 'https://x/a' },
    ])
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fillValid()

    fireEvent.click(createBtn())

    expect(await screen.findByText('No description yet.')).toBeTruthy()
  })

  it('"Go back" returns to the form with the typed name and description intact', async () => {
    h.checkDuplicateProjects.mockResolvedValue(oneMatch)
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fillValid('Gate Pass Log')

    fireEvent.click(createBtn())
    await screen.findByText('This might already exist')
    fireEvent.click(screen.getByRole('button', { name: /go back/i }))

    expect(nameInput().value).toBe('Gate Pass Log')
    expect(descriptionInput().value).toBe(wordsOf())
    expect(h.createProject).not.toHaveBeenCalled()
  })

  it('"Create project anyway" creates the project and reports the resolution', async () => {
    h.checkDuplicateProjects.mockResolvedValue(oneMatch)
    h.createProject.mockResolvedValue(makeProject())
    const onCreated = vi.fn()
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={onCreated} />)
    fillValid()

    fireEvent.click(createBtn())
    await screen.findByText('This might already exist')
    fireEvent.click(screen.getByRole('button', { name: /create project anyway/i }))

    await waitFor(() => expect(h.createProject).toHaveBeenCalled())
    expect(onCreated).toHaveBeenCalled()
    expect(h.reportDuplicateCheckResolution).toHaveBeenCalledWith('created_anyway')
  })

  it('opening a match reports the resolution without blocking the link', async () => {
    h.checkDuplicateProjects.mockResolvedValue(oneMatch)
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={vi.fn()} />)
    fillValid()

    fireEvent.click(createBtn())
    const link = await screen.findByRole('link', { name: /open app/i })
    expect(link).toHaveProperty('href', oneMatch[0].url)

    fireEvent.click(link)

    expect(h.reportDuplicateCheckResolution).toHaveBeenCalledWith('opened_existing')
    // Creating anyway is still on offer beneath the matches after opening one (R36).
    expect(screen.getByRole('button', { name: /create project anyway/i })).toBeTruthy()
  })

  it('the close button still closes the whole modal from the duplicate screen', async () => {
    h.checkDuplicateProjects.mockResolvedValue(oneMatch)
    const onClose = vi.fn()
    render(<ProjectCreateModal onClose={onClose} onCreated={vi.fn()} />)
    fillValid()

    fireEvent.click(createBtn())
    await screen.findByText('This might already exist')
    fireEvent.click(screen.getByRole('button', { name: /^close$/i }))

    expect(onClose).toHaveBeenCalled()
  })

  it('a failed duplicate check still creates the project (R37 — a courtesy, never a gate)', async () => {
    h.checkDuplicateProjects.mockRejectedValue(new Error('network blip'))
    h.createProject.mockResolvedValue(makeProject())
    const onCreated = vi.fn()
    render(<ProjectCreateModal onClose={vi.fn()} onCreated={onCreated} />)
    fillValid()

    fireEvent.click(createBtn())

    await waitFor(() => expect(h.createProject).toHaveBeenCalled())
    expect(onCreated).toHaveBeenCalled()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
