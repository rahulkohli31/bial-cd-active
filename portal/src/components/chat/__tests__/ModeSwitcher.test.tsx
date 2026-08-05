/**
 * ModeSwitcher (F5/U6) — the ONE shared in-composer Ask/Plan/Write switch.
 *
 * What this pins:
 *  - the trigger reflects the current mode; opening shows all three with a one-line
 *    description each and a ✓ on the active mode;
 *  - a CHAT wiring routes onSelect through the server switch and reflects the
 *    server-CONFIRMED value — never optimistically, and never while disabled;
 *  - a PROJECT wiring updates local draft state with no server call;
 *  - ⌥P is a document-level shortcut that opens the menu AND swallows the keystroke
 *    (macOS Option+P would otherwise type `π`), and focus returns to the composer;
 *  - the shortcut is discoverable (labeled + tooltipped).
 */
import { useRef, useState } from 'react'
import { beforeAll, afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

import { ModeSwitcher } from '../ModeSwitcher'
import * as turnStreamApi from '../../../utils/turnStreamApi'
import { type ConversationMode } from '../../../utils/turnStreamApi'

// jsdom lacks these; Radix's menu calls them on open / focus roving.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

interface HarnessProps {
  initial?: ConversationMode
  disabled?: boolean
  wiring?: 'project' | 'chat'
  withComposer?: boolean
}

function Harness({ initial = 'plan', disabled = false, wiring = 'project', withComposer = false }: HarnessProps) {
  const [mode, setMode] = useState<ConversationMode>(initial)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const onSelect = (next: ConversationMode) => {
    if (wiring === 'chat') {
      void turnStreamApi.switchMode('conv-1', next).then((confirmed) => setMode(confirmed))
    } else {
      setMode(next)
    }
  }
  return (
    <div>
      {withComposer && <textarea data-testid="composer" ref={composerRef} />}
      <ModeSwitcher
        value={mode}
        onSelect={onSelect}
        disabled={disabled}
        composerRef={withComposer ? composerRef : undefined}
      />
      <div data-testid="mode">{mode}</div>
    </div>
  )
}

/** Open the menu the way a citizen would from the composer: the ⌥P shortcut. */
function pressOptionP() {
  return fireEvent.keyDown(document, { code: 'KeyP', altKey: true })
}

describe('ModeSwitcher', () => {
  it('reflects the current mode in the trigger', () => {
    render(<ModeSwitcher value="write" onSelect={vi.fn()} />)
    expect(screen.getByRole('button', { name: /Mode: Write/i })).toBeTruthy()
  })

  it('opens to Ask/Plan/Write, each with a description and a ✓ on the active mode', () => {
    render(<ModeSwitcher value="plan" onSelect={vi.fn()} />)
    pressOptionP()

    const items = screen.getAllByRole('menuitemradio')
    expect(items).toHaveLength(3)

    // Each mode carries a one-line plain-language description.
    expect(screen.getByText(/Ask about your app/i)).toBeTruthy()
    expect(screen.getByText(/Plan it together/i)).toBeTruthy()
    expect(screen.getByText(/Build the changes/i)).toBeTruthy()

    // The ✓ is on Plan (the active mode) and only there.
    const plan = screen.getByRole('menuitemradio', { name: /Plan/i })
    const ask = screen.getByRole('menuitemradio', { name: /Ask/i })
    const write = screen.getByRole('menuitemradio', { name: /Write/i })
    expect(plan.getAttribute('aria-checked')).toBe('true')
    expect(ask.getAttribute('aria-checked')).toBe('false')
    expect(write.getAttribute('aria-checked')).toBe('false')
    expect(plan.querySelector('.lucide-check')).not.toBeNull()
    expect(ask.querySelector('.lucide-check')).toBeNull()
    expect(write.querySelector('.lucide-check')).toBeNull()
  })

  it('CHAT wiring: selecting fires the server switch and reflects the confirmed value', async () => {
    const spy = vi.spyOn(turnStreamApi, 'switchMode').mockResolvedValue('write')
    render(<Harness wiring="chat" initial="ask" />)

    pressOptionP()
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Write/i }))

    expect(spy).toHaveBeenCalledWith('conv-1', 'write')
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('write'))
    expect(screen.getByRole('button', { name: /Mode: Write/i })).toBeTruthy()
  })

  it('CHAT wiring: disabled fires no request and the menu never opens', () => {
    const spy = vi.spyOn(turnStreamApi, 'switchMode').mockResolvedValue('plan')
    render(<Harness wiring="chat" initial="plan" disabled />)

    pressOptionP()
    expect(screen.queryByRole('menuitemradio')).toBeNull() // shortcut is inert while disabled
    const trigger = screen.getByRole('button', { name: /Mode: Plan/i }) as HTMLButtonElement
    expect(trigger.disabled).toBe(true)
    expect(spy).not.toHaveBeenCalled()
  })

  it('CHAT wiring: the trigger is CONTROLLED — it does not move until the server confirms', async () => {
    let resolveSwitch: (mode: ConversationMode) => void = () => {}
    vi.spyOn(turnStreamApi, 'switchMode').mockImplementation(
      () => new Promise<ConversationMode>((resolve) => { resolveSwitch = resolve })
    )
    render(<Harness wiring="chat" initial="ask" />)

    pressOptionP()
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Plan/i }))

    // Requested, but NOT reflected yet — no optimistic move.
    expect(screen.getByTestId('mode').textContent).toBe('ask')
    expect(screen.getByRole('button', { name: /Mode: Ask/i })).toBeTruthy()

    resolveSwitch('plan')
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('plan'))
  })

  it('PROJECT wiring: selecting updates local draft with no server call', () => {
    const spy = vi.spyOn(turnStreamApi, 'switchMode')
    render(<Harness wiring="project" initial="plan" />)

    pressOptionP()
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Write/i }))

    expect(screen.getByTestId('mode').textContent).toBe('write')
    expect(screen.getByRole('button', { name: /Mode: Write/i })).toBeTruthy()
    expect(spy).not.toHaveBeenCalled()
  })

  it('⌥P opens the menu, types NO character, and returns focus to the composer after selection', async () => {
    render(<Harness wiring="project" initial="plan" withComposer />)
    const composer = screen.getByTestId('composer') as HTMLTextAreaElement
    composer.focus()
    expect(document.activeElement).toBe(composer)

    // ⌥P from the focused composer: preventDefault swallows the `π`, and the menu opens.
    const notCanceled = pressOptionP()
    expect(notCanceled).toBe(false) // preventDefault was called (no π inserted)
    expect(composer.value).toBe('')
    expect(screen.getAllByRole('menuitemradio')).toHaveLength(3)

    fireEvent.click(screen.getByRole('menuitemradio', { name: /Write/i }))
    await waitFor(() => expect(document.activeElement).toBe(composer)) // keeps typing
  })

  it('surfaces the ⌥P shortcut for discoverability (label + tooltip)', () => {
    render(<ModeSwitcher value="plan" onSelect={vi.fn()} />)
    const trigger = screen.getByRole('button', { name: /Mode: Plan/i })
    expect(trigger.getAttribute('title')).toContain('⌥P')
    expect(trigger.getAttribute('aria-keyshortcuts')).toBe('Alt+P')

    pressOptionP()
    expect(screen.getByText('⌥P')).toBeTruthy() // the kbd hint in the menu
  })
})
