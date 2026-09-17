import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import FeedbackModal from '../FeedbackModal.jsx'

// Tell React this is an act() environment (testing-library normally sets this).
globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

function renderModal(props = {}, route = '/chat') {
  const onSubmitted = props.onSubmitted ?? vi.fn()
  const onClose = props.onClose ?? vi.fn()
  const submitFn = props.submitFn ?? vi.fn(async () => ({ ok: true }))
  const triggerRef = props.triggerRef
  act(() => {
    root.render(
      <MemoryRouter initialEntries={[route]}>
        <FeedbackModal open onClose={onClose} onSubmitted={onSubmitted} submitFn={submitFn} triggerRef={triggerRef} />
      </MemoryRouter>,
    )
  })
  return { onSubmitted, onClose, submitFn, triggerRef }
}

const byTestId = (id) => container.querySelector(`[data-testid="${id}"]`)
const textarea = () => byTestId('feedback-message')
const submitBtn = () => byTestId('feedback-submit')
const overlay = () => container.querySelector('[role="dialog"] > div.absolute')
// Focusables inside the dialog, in document order (mirrors the modal's own trap query).
const focusables = () => [...container.querySelectorAll('[role="dialog"] textarea, [role="dialog"] button:not([disabled])')]

function pressKey(el, key, shiftKey = false) {
  act(() => {
    el.dispatchEvent(new KeyboardEvent('keydown', { key, shiftKey, bubbles: true }))
  })
}

/** Set a textarea's value the React-compatible way (native setter + input event). */
function typeInto(el, value) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
  setter.call(el, value)
  act(() => {
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

async function click(el) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
}

describe('FeedbackModal', () => {
  it('disables Submit when empty, enables it once text is entered', () => {
    renderModal()
    expect(submitBtn().disabled).toBe(true)
    typeInto(textarea(), 'something is broken')
    expect(submitBtn().disabled).toBe(false)
  })

  it('disables Submit and turns the counter red at/over the byte cap', () => {
    renderModal()
    typeInto(textarea(), 'a'.repeat(4001))
    expect(submitBtn().disabled).toBe(true)
    expect(byTestId('feedback-counter').className).toContain('text-danger')
  })

  it('submits the typed message with the current path, then calls onSubmitted', async () => {
    const { onSubmitted, submitFn } = renderModal({}, '/admin/feedback')
    typeInto(textarea(), '  please fix this  ')
    await click(submitBtn())
    expect(submitFn).toHaveBeenCalledWith('please fix this', '/admin/feedback')
    expect(onSubmitted).toHaveBeenCalledTimes(1)
  })

  it('on a rejected submit shows the error inline and stays open (no onSubmitted)', async () => {
    const submitFn = vi.fn(async () => {
      throw new Error('Server is down, try later.')
    })
    const { onSubmitted } = renderModal({ submitFn })
    typeInto(textarea(), 'a bug')
    await click(submitBtn())
    expect(byTestId('feedback-error').textContent).toContain('Server is down, try later.')
    expect(onSubmitted).not.toHaveBeenCalled()
    expect(textarea()).not.toBeNull() // modal still rendered
  })

  it('focuses the textarea on open', () => {
    renderModal()
    expect(document.activeElement).toBe(textarea())
  })

  it('disables Submit and turns the counter red for multibyte text at the byte cap (parity with the server)', () => {
    renderModal()
    // 2001 'é' = 4002 BYTES but only 2001 chars; a char-count cap would wrongly allow this.
    typeInto(textarea(), 'é'.repeat(2001))
    expect(submitBtn().disabled).toBe(true)
    expect(byTestId('feedback-counter').textContent).toContain('4,002')
    expect(byTestId('feedback-counter').className).toContain('text-danger')
  })

  it('returns focus to the trigger button when cancelled', () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    const { onClose } = renderModal({ triggerRef: { current: trigger } })
    const cancel = [...container.querySelectorAll('button')].find((b) => /cancel/i.test(b.textContent))
    act(() => cancel.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    expect(onClose).toHaveBeenCalled()
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('closes (and restores focus) when the overlay is clicked', () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    const { onClose } = renderModal({ triggerRef: { current: trigger } })
    act(() => overlay().dispatchEvent(new MouseEvent('click', { bubbles: true })))
    expect(onClose).toHaveBeenCalled()
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('traps Tab/Shift+Tab focus within the modal (cycles first<->last focusable)', () => {
    renderModal()
    typeInto(textarea(), 'enable submit so all controls are focusable')
    const items = focusables()
    const first = items[0]
    const last = items[items.length - 1]
    expect(items.length).toBeGreaterThanOrEqual(2)

    act(() => last.focus())
    pressKey(last, 'Tab')
    expect(document.activeElement).toBe(first)

    act(() => first.focus())
    pressKey(first, 'Tab', true)
    expect(document.activeElement).toBe(last)
  })

  it('does not toast success if the modal is dismissed while the submit is in flight', async () => {
    let resolveSubmit
    const submitFn = vi.fn(() => new Promise((r) => { resolveSubmit = r }))
    const onSubmitted = vi.fn()
    const onClose = vi.fn()
    act(() => {
      root.render(
        <MemoryRouter initialEntries={['/chat']}>
          <FeedbackModal open onClose={onClose} onSubmitted={onSubmitted} submitFn={submitFn} />
        </MemoryRouter>,
      )
    })
    typeInto(textarea(), 'a bug')
    await click(submitBtn()) // submit starts; busy=true; submitFn still pending
    expect(submitFn).toHaveBeenCalledTimes(1)

    // Parent dismisses the modal mid-flight (e.g. Escape, which `ProfileCluster` handles).
    act(() => {
      root.render(
        <MemoryRouter initialEntries={['/chat']}>
          <FeedbackModal open={false} onClose={onClose} onSubmitted={onSubmitted} submitFn={submitFn} />
        </MemoryRouter>,
      )
    })
    // The in-flight request now resolves — but the dialog is gone.
    await act(async () => {
      resolveSubmit({ ok: true })
    })
    expect(onSubmitted).not.toHaveBeenCalled() // no spurious "thanks" toast for a dismissed dialog
  })
})
