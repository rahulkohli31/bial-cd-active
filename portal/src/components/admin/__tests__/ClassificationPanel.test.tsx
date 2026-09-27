import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, within, act } from '@testing-library/react'
import type { ClassificationClass, ClassificationConfig } from '../../../utils/adminClassificationApi'
import { ApiError } from '../../../utils/apiError'

const h = vi.hoisted(() => ({
  fetchClassificationConfig: vi.fn(),
  updateClassificationPolicy: vi.fn(),
  addClassificationClass: vi.fn(),
  editClassificationClass: vi.fn(),
}))
vi.mock('../../../utils/adminClassificationApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/adminClassificationApi')>()),
  ...h,
}))

import ClassificationPanel from '../ClassificationPanel'

const cls = (key: string, title: string, over: Partial<ClassificationClass> = {}): ClassificationClass => ({
  key,
  title,
  description: `Yes if the app has ${title}.`,
  kind: 'scored',
  weight: 20,
  active: true,
  updatedAt: '2026-09-26T09:00:00Z',
  updatedByName: 'admin',
  ...over,
})

const LAUNCH: ClassificationClass[] = [
  cls('pii', 'PII', { kind: 'hard_block', weight: null }),
  cls('financial_data', 'Financial data', { kind: 'hard_block', weight: null }),
  cls('credentials', 'Credentials & keys'),
  cls('confidential', 'Confidential business data'),
  cls('ai_usage', 'AI usage'),
  cls('integrations', 'Integrations'),
  cls('public_data', 'Public data'),
]

const config = (
  classes: ClassificationClass[] = LAUNCH,
  policy: Partial<ClassificationConfig['policy']> = {},
): ClassificationConfig => ({ policy: { threshold: 100, ownersCanChangeAnswers: true, ...policy }, classes })

const withClass = (key: string, over: Partial<ClassificationClass>, classes = LAUNCH) =>
  classes.map((c) => (c.key === key ? { ...c, ...over } : c))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

afterEach(cleanup)
beforeEach(() => {
  for (const fn of Object.values(h)) fn.mockReset()
  h.fetchClassificationConfig.mockResolvedValue(config())
})

const onToast = vi.fn()
async function renderPanel() {
  onToast.mockReset()
  render(<ClassificationPanel onToast={onToast} />)
  await screen.findByText('Public data')
}

const row = (key: string) => screen.getByTestId(`row-${key}`)
const threshold = () => screen.getByLabelText('Publish without review up to a score of')
const ownersSwitch = () => screen.getByRole('switch', { name: "Owners can change the agent's answers" })

describe('the policy', () => {
  it('shows the threshold and the owners’ switch as the server holds them', async () => {
    await renderPanel()

    expect(threshold()).toHaveProperty('value', '100')
    expect(screen.getByText('Higher scores go to admin review. Hard blocks always do.')).toBeTruthy()
    expect(ownersSwitch().getAttribute('aria-checked')).toBe('true')
    expect(screen.getByTestId('owners-state').textContent).toBe("On · the owner's answers set the score")
  })

  it('saves a new threshold on blur', async () => {
    h.updateClassificationPolicy.mockResolvedValue(config(LAUNCH, { threshold: 50 }))
    await renderPanel()

    fireEvent.change(threshold(), { target: { value: '50' } })
    fireEvent.blur(threshold())

    await waitFor(() => expect(h.updateClassificationPolicy).toHaveBeenCalledWith({ threshold: 50 }))
    expect(h.updateClassificationPolicy).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(onToast).toHaveBeenCalledWith('Apps now publish without review up to a score of 50.'))
  })

  it('saves a new threshold on Enter, once', async () => {
    h.updateClassificationPolicy.mockResolvedValue(config(LAUNCH, { threshold: 50 }))
    await renderPanel()

    threshold().focus()
    fireEvent.change(threshold(), { target: { value: '50' } })
    fireEvent.keyDown(threshold(), { key: 'Enter' })

    await waitFor(() => expect(h.updateClassificationPolicy).toHaveBeenCalledWith({ threshold: 50 }))
    fireEvent.blur(threshold())
    await waitFor(() => expect(threshold()).toHaveProperty('value', '50'))
    expect(h.updateClassificationPolicy).toHaveBeenCalledTimes(1)
  })

  it.each(['101', '-1', '4.5', ''])('refuses the threshold %j inline and sends nothing', async (value) => {
    await renderPanel()

    fireEvent.change(threshold(), { target: { value } })
    fireEvent.blur(threshold())

    expect(screen.getByText('Enter a whole number from 0 to 100.')).toBeTruthy()
    expect(threshold().getAttribute('aria-invalid')).toBe('true')
    expect(h.updateClassificationPolicy).not.toHaveBeenCalled()
  })

  it('changes the owners’ state line only after the server confirms', async () => {
    const answer = deferred<ClassificationConfig>()
    h.updateClassificationPolicy.mockReturnValue(answer.promise)
    await renderPanel()

    fireEvent.click(ownersSwitch())

    await waitFor(() => expect(h.updateClassificationPolicy).toHaveBeenCalledWith({ ownersCanChangeAnswers: false }))
    expect(screen.getByTestId('owners-state').textContent).toBe("On · the owner's answers set the score")

    await act(async () => answer.resolve(config(LAUNCH, { ownersCanChangeAnswers: false })))

    expect(screen.getByTestId('owners-state').textContent).toBe("Off · the agent's answers set the score")
    expect(ownersSwitch().getAttribute('aria-checked')).toBe('false')
  })
})

describe('the classes table', () => {
  it('lists hard blocks first, then scored classes by weight, as the server orders them', async () => {
    h.fetchClassificationConfig.mockResolvedValue(
      config([
        cls('pii', 'PII', { kind: 'hard_block', weight: null }),
        cls('ai_usage', 'AI usage', { weight: 40 }),
        cls('public_data', 'Public data', { weight: 10 }),
      ]),
    )
    await renderPanel()

    const keys = screen.getAllByTestId(/^row-/).map((r) => r.dataset.testid)
    expect(keys).toEqual(['row-pii', 'row-ai_usage', 'row-public_data'])
  })

  it('shows each class’s kind, weight, share and who changed it last', async () => {
    await renderPanel()

    expect(within(row('pii')).getByText('Hard block')).toBeTruthy()
    expect(within(row('pii')).getAllByText('—')).toHaveLength(2)
    expect(within(row('ai_usage')).getByText('Scored')).toBeTruthy()
    expect(within(row('ai_usage')).getByText('20')).toBeTruthy()
    expect(within(row('ai_usage')).getByText('20%')).toBeTruthy()
    const fill = within(row('ai_usage')).getByRole('progressbar').firstElementChild as HTMLElement
    expect(fill.style.transform).toBe('translateX(-80%)')
    expect(within(row('ai_usage')).getByText('26 Sep · admin')).toBeTruthy()
  })

  it('totals the weight in the footer', async () => {
    await renderPanel()

    expect(
      screen.getByText('7 classes · total weight 100 · changes apply to apps sent for publishing after you save'),
    ).toBeTruthy()
  })

  it('shows the description behind the class’s help icon', async () => {
    await renderPanel()

    fireEvent.focus(screen.getByRole('button', { name: 'What counts as AI usage' }))

    expect((await screen.findByRole('tooltip')).textContent).toBe('Yes if the app has AI usage.')
  })

  it('narrows the rows to a search', async () => {
    await renderPanel()

    fireEvent.change(screen.getByLabelText('Search classes'), { target: { value: 'data' } })

    expect(screen.getAllByTestId(/^row-/).map((r) => r.dataset.testid)).toEqual([
      'row-financial_data',
      'row-confidential',
      'row-public_data',
    ])
  })

  it('turns a class off and shows the other shares the server now holds', async () => {
    h.editClassificationClass.mockResolvedValue(config(withClass('ai_usage', { active: false })))
    await renderPanel()

    fireEvent.click(within(row('ai_usage')).getByRole('switch', { name: 'Active: AI usage' }))

    await waitFor(() => expect(within(row('credentials')).getByText('25%')).toBeTruthy())
    expect(h.editClassificationClass).toHaveBeenCalledWith('ai_usage', { active: false })
    expect(within(row('ai_usage')).getAllByText('—')).toHaveLength(1)
    expect(onToast).toHaveBeenCalledWith('The agent no longer checks “AI usage”.')
  })

  it('turns a hard block off with one call and no confirmation', async () => {
    h.editClassificationClass.mockResolvedValue(config(withClass('pii', { active: false })))
    await renderPanel()

    fireEvent.click(within(row('pii')).getByRole('switch', { name: 'Active: PII' }))

    await waitFor(() =>
      expect(within(row('pii')).getByRole('switch', { name: 'Active: PII' }).getAttribute('aria-checked')).toBe('false'),
    )
    expect(h.editClassificationClass).toHaveBeenCalledTimes(1)
    expect(h.editClassificationClass).toHaveBeenCalledWith('pii', { active: false })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows no shares when every scored class is off', async () => {
    const allOff = LAUNCH.map((c) => (c.kind === 'scored' ? { ...c, active: false } : c))
    h.fetchClassificationConfig.mockResolvedValue(config(allOff))
    await renderPanel()

    expect(within(row('ai_usage')).getByText('20')).toBeTruthy()
    expect(within(row('ai_usage')).getByText('—')).toBeTruthy()
    expect(screen.queryByText(/^\d+%$/)).toBeNull()
    expect(screen.getByText(/total weight 0 ·/)).toBeTruthy()
  })

  it('reports a failed toggle as a problem and keeps the server’s state', async () => {
    h.editClassificationClass.mockRejectedValue(new ApiError('Super-admin privileges required.', 403))
    await renderPanel()

    fireEvent.click(within(row('ai_usage')).getByRole('switch', { name: 'Active: AI usage' }))

    await waitFor(() => expect(onToast).toHaveBeenCalledWith('Super-admin privileges required.', 'problem'))
    expect(within(row('ai_usage')).getByRole('switch', { name: 'Active: AI usage' }).getAttribute('aria-checked')).toBe(
      'true',
    )
  })
})

describe('adding and editing classes', () => {
  it('adds a class, and the table shows the shares the server returns', async () => {
    const fileUploads = cls('file_uploads', 'File uploads')
    h.addClassificationClass.mockResolvedValue(config([...LAUNCH, fileUploads]))
    await renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Add class' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Title'), { target: { value: 'File uploads' } })
    fireEvent.change(within(dialog).getByLabelText('Description'), { target: { value: 'Yes if it keeps uploads.' } })
    fireEvent.change(within(dialog).getByLabelText('Weight'), { target: { value: '20' } })
    expect(within(dialog).getByText('A Yes here is worth 17 of 100')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add class' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(h.addClassificationClass).toHaveBeenCalledWith({
      title: 'File uploads',
      description: 'Yes if it keeps uploads.',
      kind: 'scored',
      weight: 20,
      active: true,
    })
    for (const key of ['credentials', 'ai_usage', 'file_uploads']) {
      expect(within(row(key)).getByText('17%')).toBeTruthy()
    }
    expect(onToast).toHaveBeenCalledWith('Added “File uploads”.')
  })

  it('makes a class a hard block, and its row loses its weight and share', async () => {
    h.editClassificationClass.mockResolvedValue(config(withClass('ai_usage', { kind: 'hard_block', weight: null })))
    await renderPanel()

    fireEvent.click(within(row('ai_usage')).getByRole('button', { name: 'Edit AI usage' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Hard block' }))
    expect(within(dialog).queryByLabelText('Weight')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(h.editClassificationClass).toHaveBeenCalledWith('ai_usage', {
      title: 'AI usage',
      description: 'Yes if the app has AI usage.',
      kind: 'hard_block',
      weight: null,
      active: true,
    })
    expect(within(row('ai_usage')).getByText('Hard block')).toBeTruthy()
    expect(within(row('ai_usage')).getAllByText('—')).toHaveLength(2)
  })

  it('shows a duplicate title on the title field and keeps the dialog open', async () => {
    h.addClassificationClass.mockRejectedValue(
      new ApiError('A class called “PII” already exists.', 409, 'duplicate_title'),
    )
    await renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Add class' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Title'), { target: { value: 'PII' } })
    fireEvent.change(within(dialog).getByLabelText('Description'), { target: { value: 'Yes if it keeps IDs.' } })
    fireEvent.change(within(dialog).getByLabelText('Weight'), { target: { value: '20' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add class' }))

    expect(await within(dialog).findByText('A class called “PII” already exists.')).toBeTruthy()
    expect(within(dialog).getByLabelText('Title').getAttribute('aria-invalid')).toBe('true')
    expect(onToast).not.toHaveBeenCalled()
  })
})

describe('loading', () => {
  it('offers a retry when the configuration cannot be read', async () => {
    h.fetchClassificationConfig.mockRejectedValueOnce(new ApiError('Could not load the classification settings (500).', 500))
    render(<ClassificationPanel onToast={onToast} />)

    expect(await screen.findByText('Could not load the classification settings (500).')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByText('Public data')).toBeTruthy()
  })
})
