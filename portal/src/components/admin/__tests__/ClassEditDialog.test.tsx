import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import ClassEditDialog from '../ClassEditDialog'
import { ApiError } from '../../../utils/apiError'
import type { ClassificationClass } from '../../../utils/adminClassificationApi'

afterEach(cleanup)

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

const DESCRIPTION = 'Yes if the app lets people upload files and keeps them.'

function renderDialog(over: Partial<Parameters<typeof ClassEditDialog>[0]> = {}) {
  const onSubmit = over.onSubmit ?? vi.fn(async () => {})
  const onClose = over.onClose ?? vi.fn()
  render(<ClassEditDialog editing={null} classes={LAUNCH} onClose={onClose} onSubmit={onSubmit} {...over} />)
  return { onSubmit, onClose }
}

const title = () => screen.getByLabelText('Title')
const description = () => screen.getByLabelText('Description')
const weight = () => screen.getByLabelText('Weight')
const save = (name = 'Add class') => screen.getByRole('button', { name })
const type = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } })

function fillNewClass(weightValue = '20') {
  type(title(), 'File uploads')
  type(description(), DESCRIPTION)
  type(weight(), weightValue)
}

describe('adding a class', () => {
  it('shows what a Yes is worth and how the other classes shift', () => {
    renderDialog()
    fillNewClass()

    expect(screen.getByText('A Yes here is worth 17 of 100')).toBeTruthy()
    expect(
      screen.getByText('Total weight becomes 120, so the other five classes drop from 20 to 17 each.'),
    ).toBeTruthy()
    const legend = screen.getByTestId('share-legend')
    expect(legend.textContent).toContain('Credentials & keys 17%')
    expect(legend.textContent).toContain('File uploads 17%')
    expect(screen.getAllByTestId('share-segment')).toHaveLength(6)
  })

  it('leaves inactive classes out of the total and the bar', () => {
    const aiOff = LAUNCH.map((c) => (c.key === 'ai_usage' ? { ...c, active: false } : c))
    renderDialog({ classes: aiOff })
    fillNewClass()

    expect(
      screen.getByText('Total weight becomes 100, so the other four classes drop from 25 to 20 each.'),
    ).toBeTruthy()
    expect(screen.getAllByTestId('share-segment')).toHaveLength(5)
    expect(screen.getByTestId('share-legend').textContent).not.toContain('AI usage')
  })

  it('sends the trimmed fields as a scored, active class', async () => {
    const { onSubmit } = renderDialog()
    type(title(), '  File uploads ')
    type(description(), DESCRIPTION)
    type(weight(), '20')

    fireEvent.click(save())

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({
        title: 'File uploads',
        description: DESCRIPTION,
        kind: 'scored',
        weight: 20,
        active: true,
      }),
    )
  })

  it('tells the agent’s instruction apart from anything owners see', () => {
    renderDialog()
    const helper = screen.getByText('The deployment classification agent follows this when it checks an app.')
    expect(helper.textContent).not.toContain('Owners')
  })

  it('keeps Save disabled until title and description are filled', () => {
    renderDialog()
    type(weight(), '20')
    expect(save()).toHaveProperty('disabled', true)

    type(title(), 'File uploads')
    expect(save()).toHaveProperty('disabled', true)

    type(description(), DESCRIPTION)
    expect(save()).toHaveProperty('disabled', false)

    type(title(), '   ')
    expect(save()).toHaveProperty('disabled', true)
  })

  it('refuses a description over 1,000 characters with its own error', () => {
    renderDialog()
    fillNewClass()
    type(description(), 'x'.repeat(1001))

    expect(screen.getByText('Keep the description to 1,000 characters or fewer.')).toBeTruthy()
    expect(save()).toHaveProperty('disabled', true)
  })

  it('refuses a title over 60 characters', () => {
    renderDialog()
    fillNewClass()
    type(title(), 'x'.repeat(61))

    expect(screen.getByText('Keep the title to 60 characters or fewer.')).toBeTruthy()
    expect(save()).toHaveProperty('disabled', true)
  })

  it.each(['', '4.5', '101', '-1', 'ten'])('refuses the weight %j', (value) => {
    renderDialog()
    fillNewClass(value)

    expect(screen.getByText('Enter a whole number from 0 to 100.')).toBeTruthy()
    expect(save()).toHaveProperty('disabled', true)
  })

  it('shows a duplicate title from the server on the title field and stays open', async () => {
    const onSubmit = vi.fn(async () => {
      throw new ApiError('A class called “PII” already exists.', 409, 'duplicate_title')
    })
    const { onClose } = renderDialog({ onSubmit })
    fillNewClass()
    type(title(), 'PII')

    fireEvent.click(save())

    const error = await screen.findByText('A class called “PII” already exists.')
    expect(title().getAttribute('aria-invalid')).toBe('true')
    expect(title().getAttribute('aria-describedby')).toBe(error.id)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('shows any other failure in the dialog', async () => {
    const onSubmit = vi.fn(async () => {
      throw new ApiError('Super-admin privileges required.', 403)
    })
    renderDialog({ onSubmit })
    fillNewClass()

    fireEvent.click(save())

    expect((await screen.findByRole('alert')).textContent).toContain('Super-admin privileges required.')
  })
})

describe('editing a class', () => {
  const aiUsage = LAUNCH[4]

  it('opens on the class as saved', () => {
    renderDialog({ editing: aiUsage })

    expect(screen.getByRole('heading', { name: 'Edit class' })).toBeTruthy()
    expect(title()).toHaveProperty('value', 'AI usage')
    expect(weight()).toHaveProperty('value', '20')
    expect(screen.getByText('Total weight stays 100, so the other classes keep their shares.')).toBeTruthy()
  })

  it('hides the weight for a hard block and sends no weight', async () => {
    const { onSubmit } = renderDialog({ editing: aiUsage })

    fireEvent.click(screen.getByRole('radio', { name: 'Hard block' }))

    expect(screen.queryByLabelText('Weight')).toBeNull()
    expect(screen.queryByText(/A Yes here is worth/)).toBeNull()
    expect(screen.getByRole('radio', { name: 'Hard block' }).getAttribute('aria-checked')).toBe('true')

    fireEvent.click(save('Save'))
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({
        title: 'AI usage',
        description: aiUsage.description,
        kind: 'hard_block',
        weight: null,
        active: true,
      }),
    )
  })

  it('shows the other classes rising when this one is turned off', () => {
    renderDialog({ editing: aiUsage })

    fireEvent.click(screen.getByRole('switch', { name: 'Active' }))

    expect(screen.getByText('A Yes here is worth 0 of 100')).toBeTruthy()
    expect(screen.getByText('Total weight becomes 80, so the other four classes rise from 20 to 25 each.')).toBeTruthy()
  })

  it('points at the bar when the other classes move by different amounts', () => {
    const mixed = [cls('a', 'Alpha', { weight: 30 }), cls('b', 'Beta', { weight: 20 })]
    renderDialog({ classes: mixed })
    fillNewClass('50')

    expect(screen.getByText('A Yes here is worth 50 of 100')).toBeTruthy()
    expect(screen.getByText('Total weight becomes 100. The bar shows each class’s new share.')).toBeTruthy()
    expect(screen.getByTestId('share-legend').textContent).toContain('Alpha 30%')
  })

  it('says so when every weight is 0', () => {
    renderDialog({ classes: [cls('a', 'Alpha', { weight: 0 })] })
    fillNewClass('0')

    expect(screen.getByText('A Yes here is worth 0 of 100')).toBeTruthy()
    expect(screen.getByText('Total weight stays 0, so no class adds to the score.')).toBeTruthy()
    expect(screen.queryByTestId('share-legend')).toBeNull()
  })
})
