/**
 * DiscardChangesDialog — the guarantees a destructive confirm owes the user:
 *   1. It never invents a date: with a valid `savedAt` it names the exact saved instant, and
 *      with `null` or an unparseable string it falls back to the dateless sentence.
 *   2. Confirm cannot double-fire while its promise is pending, and Cancel/Escape cannot
 *      close the dialog mid-request.
 *   3. Focus opens on Cancel, the safe default for a destructive action.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import DiscardChangesDialog from '../DiscardChangesDialog'
import { formatStamp } from '../../../utils/publishPresentation'

afterEach(() => cleanup())

const SAVED_AT = '2026-08-25T14:20:00.000Z'

describe('DiscardChangesDialog — wording', () => {
  it('states the title and, with a valid saved instant, the exact formatted date', () => {
    render(<DiscardChangesDialog savedAt={SAVED_AT} onClose={vi.fn()} onConfirm={vi.fn()} />)

    expect(screen.getByText('Discard unsaved changes?')).toBeTruthy()
    expect(
      screen.getByText(
        `Your app goes back to the version you saved on ${formatStamp(SAVED_AT)}. Everything changed since then is removed.`,
      ),
    ).toBeTruthy()
  })

  it('falls back to the dateless sentence when savedAt is null', () => {
    render(<DiscardChangesDialog savedAt={null} onClose={vi.fn()} onConfirm={vi.fn()} />)

    expect(
      screen.getByText('Your app goes back to the version you last saved. Everything changed since then is removed.'),
    ).toBeTruthy()
    // Never a guess at a date it does not have.
    expect(screen.queryByText(/saved on/i)).toBeNull()
  })

  it('falls back to the dateless sentence when savedAt does not parse as a date', () => {
    render(<DiscardChangesDialog savedAt="not-a-date" onClose={vi.fn()} onConfirm={vi.fn()} />)

    expect(
      screen.getByText('Your app goes back to the version you last saved. Everything changed since then is removed.'),
    ).toBeTruthy()
    expect(screen.queryByText(/saved on/i)).toBeNull()
  })
})

describe('DiscardChangesDialog — buttons', () => {
  it('Cancel calls onClose and never onConfirm', () => {
    const onClose = vi.fn()
    const onConfirm = vi.fn()
    render(<DiscardChangesDialog savedAt={null} onClose={onClose} onConfirm={onConfirm} />)

    fireEvent.click(screen.getByTestId('discard-dialog-cancel'))

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('presses confirm only once, even when clicked twice while its promise is pending', async () => {
    let resolveConfirm: () => void = () => {}
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveConfirm = resolve
        }),
    )
    render(<DiscardChangesDialog savedAt={null} onClose={vi.fn()} onConfirm={onConfirm} />)

    const confirmBtn = screen.getByTestId('discard-dialog-confirm')
    fireEvent.click(confirmBtn)
    fireEvent.click(confirmBtn)
    fireEvent.click(confirmBtn)

    expect(onConfirm).toHaveBeenCalledTimes(1)

    resolveConfirm()
    await waitFor(() => expect(confirmBtn.hasAttribute('disabled')).toBe(false))
  })

  it('disables Cancel while confirm is in flight', async () => {
    const onConfirm = vi.fn(() => new Promise<void>(() => {}))
    render(<DiscardChangesDialog savedAt={null} onClose={vi.fn()} onConfirm={onConfirm} />)

    fireEvent.click(screen.getByTestId('discard-dialog-confirm'))

    await waitFor(() => expect(screen.getByTestId('discard-dialog-cancel').hasAttribute('disabled')).toBe(true))
  })
})

describe('DiscardChangesDialog — modal behaviour', () => {
  it('does not close on Escape while confirm is pending', async () => {
    const onClose = vi.fn()
    const onConfirm = vi.fn(() => new Promise<void>(() => {}))
    render(<DiscardChangesDialog savedAt={null} onClose={onClose} onConfirm={onConfirm} />)

    fireEvent.click(screen.getByTestId('discard-dialog-confirm'))
    await waitFor(() => expect(screen.getByTestId('discard-dialog-cancel').hasAttribute('disabled')).toBe(true))

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes on Escape when idle', () => {
    const onClose = vi.fn()
    render(<DiscardChangesDialog savedAt={null} onClose={onClose} onConfirm={vi.fn()} />)

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('opens with focus on Cancel', async () => {
    render(<DiscardChangesDialog savedAt={null} onClose={vi.fn()} onConfirm={vi.fn()} />)

    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('discard-dialog-cancel')))
  })
})
