/**
 * DataClassificationModal: six weighted Yes/No toggles, an escalating warning shown only
 * once every question is answered, the notes gate at the >=25 soft threshold, and a Cancel
 * structurally isolated from Deploy (backdrop/Escape/button all resolve to the same
 * `onCancel`, none of them reachable from `onConfirm`).
 *
 * What these do NOT assert, deliberately: that a low score blocks the button. It must not —
 * the running total is informational and the server is the gate, so a test pinning a
 * client-side block would enshrine exactly the bypassable design this avoids. The
 * below-threshold case is covered where it belongs, in DeployControl.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import DataClassificationModal from '../DataClassificationModal'

afterEach(cleanup)

const CATEGORY_KEYS = [
  'credentialsSecrets',
  'healthData',
  'personalInformation',
  'financialData',
  'confidentialBusinessData',
  'publicData',
] as const

function answerAll(value: 'yes' | 'no', except: (typeof CATEGORY_KEYS)[number][] = []): void {
  for (const key of CATEGORY_KEYS) {
    if (except.includes(key)) continue
    fireEvent.click(screen.getByTestId(`dc-question-${key}-${value}`))
  }
}

describe('DataClassificationModal', () => {
  it('starts with Confirm disabled and no warning line (unanswered, not "no")', () => {
    render(<DataClassificationModal onConfirm={vi.fn()} onCancel={vi.fn()} />)
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByTestId('dc-warning')).toBeNull()
  })

  it('requires all six answers before Confirm enables', () => {
    render(<DataClassificationModal onConfirm={vi.fn()} onCancel={vi.fn()} />)
    answerAll('no', ['publicData'])
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByTestId('dc-warning')).toBeNull() // still not all-answered

    fireEvent.click(screen.getByTestId('dc-question-publicData-no'))
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)
    expect(screen.getByTestId('dc-warning').textContent).toMatch(/no sensitive data/i)
  })

  it('distinguishes "answered, all No" from "unanswered" — the warning only ever appears once complete', () => {
    render(<DataClassificationModal onConfirm={vi.fn()} onCancel={vi.fn()} />)
    // Answer, then flip one back to unanswered by never touching it — already covered above.
    // Here: answer five, leave one untouched, confirm no warning renders at all.
    answerAll('no', ['healthData'])
    expect(screen.queryByTestId('dc-warning')).toBeNull()
  })

  it('Credentials/Secrets alone crosses the notes-required threshold (weight 40)', () => {
    render(<DataClassificationModal onConfirm={vi.fn()} onCancel={vi.fn()} />)
    answerAll('no', ['credentialsSecrets'])
    fireEvent.click(screen.getByTestId('dc-question-credentialsSecrets-yes'))

    expect(screen.getByTestId('dc-warning').textContent).toMatch(/higher-sensitivity data/i)
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByTestId('dc-notes'), { target: { value: 'Vaulted, never logged.' } })
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)
  })

  it('Health Data alone also crosses the threshold (weight 25)', () => {
    render(<DataClassificationModal onConfirm={vi.fn()} onCancel={vi.fn()} />)
    answerAll('no', ['healthData'])
    fireEvent.click(screen.getByTestId('dc-question-healthData-yes'))
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(true)
  })

  it('Public Data + Confidential Business Data (15) stays below the threshold — notes optional', () => {
    render(<DataClassificationModal onConfirm={vi.fn()} onCancel={vi.fn()} />)
    answerAll('no', ['publicData', 'confidentialBusinessData'])
    fireEvent.click(screen.getByTestId('dc-question-publicData-yes'))
    fireEvent.click(screen.getByTestId('dc-question-confidentialBusinessData-yes'))

    expect(screen.getByTestId('dc-warning').textContent).toMatch(/some sensitive data/i)
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)
  })

  it('Confirm calls onConfirm with the complete answer set, notes trimmed to null when blank', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined)
    render(<DataClassificationModal onConfirm={onConfirm} onCancel={vi.fn()} />)
    answerAll('no')
    fireEvent.click(screen.getByTestId('dc-confirm'))

    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
    expect(onConfirm).toHaveBeenCalledWith({
      credentialsSecrets: false,
      healthData: false,
      personalInformation: false,
      financialData: false,
      confidentialBusinessData: false,
      publicData: false,
      notes: null,
    })
  })

  it('a rejected Confirm keeps the modal open and shows the error, answers intact', async () => {
    const onConfirm = vi.fn().mockRejectedValue(new Error('Could not submit — try again.'))
    render(<DataClassificationModal onConfirm={onConfirm} onCancel={vi.fn()} />)
    answerAll('no')
    fireEvent.click(screen.getByTestId('dc-confirm'))

    expect((await screen.findByRole('alert')).textContent).toContain('Could not submit')
    expect(screen.getByTestId('data-classification-modal')).toBeTruthy()
    // The answers are still there — every toggle still shows as answered (Confirm re-enabled).
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)
  })

  // --- Cancel is structural ----------------------------------------------------

  it('the Cancel button calls onCancel only — never onConfirm', () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    render(<DataClassificationModal onConfirm={onConfirm} onCancel={onCancel} />)
    answerAll('no')
    fireEvent.click(screen.getByTestId('dc-cancel'))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('a backdrop click calls onCancel only', () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    const { container } = render(<DataClassificationModal onConfirm={onConfirm} onCancel={onCancel} />)
    const backdrop = container.querySelector('.bg-black\\/40')
    expect(backdrop).toBeTruthy()
    fireEvent.click(backdrop as Element)
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('Escape calls onCancel only (and is inert while busy)', async () => {
    const onCancel = vi.fn()
    let release: () => void = () => {}
    const onConfirm = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => { release = resolve }),
    )
    const { container } = render(<DataClassificationModal onConfirm={onConfirm} onCancel={onCancel} />)
    answerAll('no')
    // The Tab/Escape trap is on the focusable CARD (tabIndex={-1}), one level inside the
    // `data-classification-modal` overlay — React's onKeyDown only sees events dispatched
    // on itself or bubbling up from a descendant, so the key must target the card.
    const card = container.querySelector('[tabindex="-1"]') as Element

    fireEvent.click(screen.getByTestId('dc-confirm'))
    await waitFor(() => expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(true))
    fireEvent.keyDown(card, { key: 'Escape' })
    expect(onCancel).not.toHaveBeenCalled() // busy: Escape is inert

    release()
    await waitFor(() => expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.keyDown(card, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).toHaveBeenCalledTimes(1) // never called a second time by Escape
  })
})
