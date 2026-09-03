/**
 * ONE COMPOSER (R40–R45, R55, R57–R60, R64, R72).
 *
 * ══ THE PROPERTY THIS FILE EXISTS FOR ══
 *
 * NOTHING HERE IS EVER `disabled`. Not the textarea, not attach, not Send — in any state,
 * including mid-turn, over the cap, and with an offer pending. `disabled` on the currently-focused
 * element blurs it to `document.body`, which is the mechanism behind "it blurs mid-sentence and
 * focus never comes back"; this codebase has recorded that twice and it is not a style preference.
 *
 * So the subtree sweep below is not one assertion among many — it is the mechanical form of "the
 * library's Send is not used here". `createActionButton` renders
 * `<button disabled={props.disabled || !callback}>` and `useComposerSend` returns no callback
 * while `isRunning && !capabilities.queue`, and `queue` is never registered, so the library's Send
 * ships a hard `disabled` for the whole of every turn. It is swept for in EVERY state rather than
 * checked once, because the states are where it would come back.
 *
 * ══ THE DRAFT IS HELD, NOT RESTORED (R58/R59) ══
 *
 * Nothing is cleared optimistically. A failed send therefore has nothing to put back and no race
 * to guard — which is how issue #154's defect class stops existing rather than being patched.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import { ComposerHarness } from './_composerHarness'
import Composer, { type ComposerProps } from '../Composer'
import { OFFER_GATE_NOTE } from '../OfferStrip'
import { readDraft, writeDraft } from '../../../utils/composerDraft'

afterEach(() => {
  cleanup()
  sessionStorage.clear()
})

function draw(over: Partial<ComposerProps> = {}) {
  const props: ComposerProps = {
    conversationId: 'chat-1',
    onSubmit: vi.fn().mockResolvedValue(undefined),
    isRunning: false,
    onUrgent: vi.fn(),
    ...over,
  }
  return { props, ...render(<ComposerHarness><Composer {...props} /></ComposerHarness>) }
}

const box = () => screen.getByTestId('composer-input') as HTMLTextAreaElement
const send = () => screen.getByTestId('composer-send')
const gateNote = () => screen.queryByTestId('composer-gate-note')
const type = (text: string) => fireEvent.change(box(), { target: { value: text } })

/** The sweep. Every state this composer can be in has to pass it. */
const noRealDisabled = (container: HTMLElement) =>
  expect(container.querySelector('[disabled]')).toBeNull()

describe('a turn in flight: typing stays, sending waits (AE30)', () => {
  it('takes typed input, marks Send unavailable, and says why in one short line', () => {
    const { container } = draw({ isRunning: true })

    type('while it thinks')
    expect(box().value).toBe('while it thinks')

    expect(send().getAttribute('aria-disabled')).toBe('true')
    expect(gateNote()?.textContent).toMatch(/send unlocks when it is done/i)
    // ONE line, not a stack of them.
    expect(screen.getAllByTestId('composer-gate-note')).toHaveLength(1)
    noRealDisabled(container)
  })

  it('the reason rides in Send’s accessible name, not only in a line of text elsewhere', () => {
    // A screen-reader user standing on the control gets the reason from the control.
    draw({ isRunning: true })
    expect(screen.getByRole('button', { name: /Send message — .*send unlocks/i })).toBeTruthy()
  })

  it('Enter sends nothing while a turn runs, and the ENFORCEMENT is in the handler', () => {
    // The attribute is affordance. Pressing Enter goes straight to the handler, so the handler is
    // what has to refuse — a guard that lived only in the button would be bypassed by the key.
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    draw({ isRunning: true, onSubmit })
    type('should not go')
    fireEvent.keyDown(box(), { key: 'Enter' })
    expect(onSubmit).not.toHaveBeenCalled()
    // …and clicking the visually-dimmed Send is refused by the same handler.
    fireEvent.click(send())
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('Shift+Enter is a newline, never a send', () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    draw({ onSubmit })
    type('first line')
    fireEvent.keyDown(box(), { key: 'Enter', shiftKey: true })
    expect(onSubmit).not.toHaveBeenCalled()
  })
})

describe('nothing is ever `disabled` — swept in every state', () => {
  it.each([
    ['idle', {} as Partial<ComposerProps>],
    ['a turn running', { isRunning: true }],
    ['a surface gate closed', { gate: { blocked: true, reason: 'Building your app.' } }],
    ['an offer pending', { offer: { toolCallId: 'c1', conversationId: 'chat-1', spent: false, onBuild: vi.fn(), onKeepPlanning: vi.fn() } }],
    ['a stop control mounted', { isRunning: true, stop: { running: true, resolveTarget: () => null, onStopTurn: vi.fn(), onStopSession: vi.fn() } }],
  ])('%s', (_name, over) => {
    const { container } = draw(over)
    noRealDisabled(container)
    // LIVENESS: the sweep is over a composer that actually rendered its controls.
    expect(box()).toBeTruthy()
    expect(send()).toBeTruthy()
  })

  it('and over the cap, which is the state most likely to tempt a real disable', () => {
    const { container } = draw()
    type('x'.repeat(10_001))
    expect(send().getAttribute('aria-disabled')).toBe('true')
    noRealDisabled(container)
    expect(box().value.length).toBe(10_001) // and NOTHING was cut
  })

  it('the textarea carries no `maxLength` — issue #156 forbids it by name', () => {
    draw()
    expect(box().hasAttribute('maxLength')).toBe(false)
  })
})

describe('focus never drops', () => {
  it('Send keeps focus across a turn starting', () => {
    // The regression guard for the reported "it blurs mid-sentence" defect. jsdom does not
    // implement blur-on-disable, so this cannot catch a reintroduced `disabled` on its own — the
    // subtree sweep above is that half. What this pins is the other one: nothing here GRABS or
    // drops focus at the turn's edges.
    const { rerender, props } = draw()
    send().focus()
    expect(document.activeElement).toBe(send())

    rerender(<ComposerHarness><Composer {...props} isRunning /></ComposerHarness>)
    expect(document.activeElement).toBe(send())
  })

  it('the textarea keeps focus too', () => {
    const { rerender, props } = draw()
    box().focus()
    rerender(<ComposerHarness><Composer {...props} isRunning /></ComposerHarness>)
    expect(document.activeElement).toBe(box())
  })
})

describe('growth is bounded, then it scrolls', () => {
  it('grows with the text and stops at a ceiling', () => {
    // `react-textarea-autosize` measures with `scrollHeight`, which jsdom reports as 0 — so the
    // pixel behaviour cannot be observed here. What CAN be pinned is that the ceiling is declared
    // and finite, and that reaching it drops nothing.
    //
    // THE CEILING MOVED WITH THE BOX (plan 002, U5). It was a `maxRows` prop on our own textarea;
    // it is a `max-h` on the library's, because the library's input owns its own autosize. One
    // line is the board's resting height — the box grows from a single line rather than opening
    // two rows tall.
    draw()
    expect(box().getAttribute('rows')).toBe('1')
    expect(box().className).toMatch(/max-h-\[\d+px\]/)
    type('one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\neleven\ntwelve\nthirteen')
    expect(box().value.split('\n')).toHaveLength(13)
    // The box still exists and still holds every line — growth capped is not content dropped.
    expect(box().value).toContain('thirteen')
  })
})

describe('the draft is held until the server confirms (R58/R59)', () => {
  it('clears text and staged files ONLY on a resolved send', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    draw({ onSubmit })
    type('send me')
    fireEvent.keyDown(box(), { key: 'Enter' })

    await waitFor(() => expect(box().value).toBe(''))
    expect(readDraft('chat-1')).toBe('')
  })

  it('keeps EVERYTHING when the send rejects, and says so once', async () => {
    // The defect this replaces: `ChatPage` did a blind `setText(rawText)` on failure, and because
    // the input was fully controlled the browser's undo stack could not recover what it replaced.
    // Holding the text means there is nothing to restore and no race to lose.
    const onUrgent = vi.fn()
    draw({ onSubmit: vi.fn().mockRejectedValue(new Error('refused')), onUrgent })
    type('do not lose me')
    fireEvent.keyDown(box(), { key: 'Enter' })

    await waitFor(() => expect(onUrgent).toHaveBeenCalled())
    expect(onUrgent.mock.calls[0][0]).toMatch(/still here/i)
    expect(box().value).toBe('do not lose me')
    expect(readDraft('chat-1')).toBe('do not lose me')
  })

  it('★ stores what the box KEPT when the citizen rewrote it mid-send, rather than clearing it', async () => {
    // THE DIVERGENCE THIS IS WRITTEN AGAINST. The box deliberately leaves a rewritten message
    // alone, but the draft was cleared on every accepted send — so the composer showed words that
    // were no longer stored anywhere, and because the mirror only fires when the text CHANGES
    // nothing ever wrote them back. A reload, or a step to a sibling chat, destroyed them.
    let release = () => {}
    const gate = new Promise<void>((r) => { release = r })
    const onSubmit = vi.fn().mockReturnValue(gate)
    draw({ onSubmit })

    type('make the header blue')
    fireEvent.keyDown(box(), { key: 'Enter' })
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(send().getAttribute('aria-disabled')).toBe('true') // the send is out

    // While the request is out they select all and start again.
    type('actually make it red')
    release()

    // WAIT FOR THE SEND TO SETTLE, not for the draft to hold a value it holds already: the store
    // is written the moment they type, so an assertion polled from the press would pass before
    // the clearing this test is about had even had its chance to run.
    await waitFor(() => expect(send().getAttribute('aria-disabled')).toBe('false'))
    expect(readDraft('chat-1')).toBe('actually make it red')
    // …and the box and the store agree, which is the property.
    expect(box().value).toBe('actually make it red')
  })

  it('stamps the conversation at PRESS time, so a mid-send switch cannot misfile it (R60)', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    draw({ onSubmit })
    type('for chat one')
    fireEvent.keyDown(box(), { key: 'Enter' })
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].conversationId).toBe('chat-1')
  })
})

describe('the draft follows its own chat', () => {
  it('survives a round trip to a sibling, which was empty throughout', () => {
    const { rerender, props } = draw()
    type('a draft for one')

    rerender(<ComposerHarness><Composer {...props} conversationId="chat-2" /></ComposerHarness>)
    expect(box().value).toBe('') // the sibling never saw it

    rerender(<ComposerHarness><Composer {...props} conversationId="chat-1" /></ComposerHarness>)
    expect(box().value).toBe('a draft for one')
  })

  it('survives a reload, and reads through the one store rather than a second one', () => {
    // The store is `utils/composerDraft.ts` and this is its ONE writer (R-7). Seeding the store
    // directly and mounting fresh is the reload: if the composer kept its own second copy, the
    // seeded value would not appear.
    writeDraft('chat-9', 'typed before the reload')
    draw({ conversationId: 'chat-9' })
    expect(box().value).toBe('typed before the reload')
  })
})

describe('R55 — the relocated stop', () => {
  it('renders when a turn is running, with a stable accessible name', () => {
    draw({
      isRunning: true,
      stop: { running: true, resolveTarget: () => ({ conversationId: 'chat-1', turnId: 't1' }), onStopTurn: vi.fn().mockResolvedValue(undefined), onStopSession: vi.fn() },
    })
    expect(screen.getByTestId('stop-turn')).toBeTruthy()
  })

  it('presses through to the turn-stop path with the active conversation and turn', async () => {
    const onStopTurn = vi.fn().mockResolvedValue(undefined)
    draw({
      isRunning: true,
      stop: { running: true, resolveTarget: () => ({ conversationId: 'chat-1', turnId: 't7' }), onStopTurn, onStopSession: vi.fn() },
    })
    fireEvent.click(screen.getByTestId('stop-turn'))
    await waitFor(() => expect(onStopTurn).toHaveBeenCalledWith('chat-1', 't7'))
  })

  it('is ABSENT when nothing is running — paired with a liveness assertion', () => {
    draw()
    expect(screen.queryByTestId('stop-turn')).toBeNull()
    // …and the composer is there and typeable, so the absence is an absence rather than a
    // component that rendered nothing.
    type('still typeable')
    expect(box().value).toBe('still typeable')
  })
})

describe('attachments', () => {
  it('the drop target is the WHOLE composer, not just the row', () => {
    // A drop landing on the chips or the gate note would otherwise fall through to the browser's
    // default handler — which navigates the tab away and discards the draft AND the staged files.
    //
    // THE DROPZONE IS THE LIBRARY'S NOW (plan 002, U5) and it wraps the whole box, which is the
    // same property said about a different element. It sets the same `data-dragging` attribute
    // the hand-rolled one did, so what changed is the handle, not the behaviour.
    const { container } = draw()
    const zone = screen.getByTestId('composer-dropzone')
    expect(zone.contains(screen.getByTestId('composer'))).toBe(true)
    expect(zone.getAttribute('data-dragging')).toBeNull()

    fireEvent.dragEnter(zone, { dataTransfer: { types: ['Files'] } })
    fireEvent.dragOver(zone, { dataTransfer: { types: ['Files'] } })
    expect(zone.getAttribute('data-dragging')).toBe('true')

    fireEvent.dragLeave(zone, { dataTransfer: { types: ['Files'] } })
    expect(zone.getAttribute('data-dragging')).toBeNull()
    noRealDisabled(container)
  })

  it('★ claims a drop on the NOTE UNDER the box, which is where a mis-aimed one lands', async () => {
    // THE STRIP UNDER THE BOX IS PART OF THE COMPOSER TO WHOEVER IS AIMING AT IT — the gate note,
    // the context warning, the standing caption and the counter — and on a running turn there is
    // always text there. Left outside the drop target, a drop a few pixels low fell through to the
    // browser's default handler, which navigates the tab to the file and takes every staged
    // attachment with it.
    draw({ isRunning: true })
    const note = screen.getByTestId('composer-gate-note')

    const notCancelled = fireEvent.drop(note, {
      dataTransfer: {
        types: ['Files'],
        files: [new File(['id,name\n1,Priya'], 'payroll.csv', { type: 'text/csv' })],
      },
    })

    // `dispatchEvent` returns false exactly when something called `preventDefault` — which is the
    // difference between the composer taking the file and the browser leaving the page.
    expect(notCancelled).toBe(false)
    // …and the file was actually staged, so the drop was claimed rather than merely swallowed.
    await waitFor(() => expect(screen.getByTestId('composer-chips').textContent).toContain('payroll.csv'))
  })

  it('attach never goes unavailable — staging a file is composing, not sending', () => {
    draw({ isRunning: true })
    const attach = screen.getByLabelText('Attach a file')
    expect(attach.hasAttribute('disabled')).toBe(false)
    expect(attach.getAttribute('aria-disabled')).toBeNull()
  })

  it('a staged file does NOT follow the reader into the next chat', async () => {
    // The draft is stored per conversation and re-hydrates on a switch; the staged files had no
    // such handling and simply stayed. This composer is not remounted per chat — flat routing
    // keeps one instance — so a file picked in one conversation was still staged in the next,
    // counted against that chat's attachment and text budgets, and would have been sent into a
    // conversation the citizen never attached it to.
    //
    // They are DROPPED rather than restored on the way back: unlike the draft they live only in
    // memory as decoded bytes, so there is nothing to hydrate them from.
    // STAGED BY DROP rather than through a hidden file input. The library's add-attachment control
    // opens the OS picker, which jsdom cannot drive — and its dropzone reaches exactly the same
    // `addAttachment`, so this exercises the real path rather than a seam invented for the test.
    const { rerender, props } = draw()
    const file = new File(['id,name\n1,Priya'], 'payroll.csv', { type: 'text/csv' })
    fireEvent.drop(screen.getByTestId('composer-dropzone'), {
      dataTransfer: { types: ['Files'], files: [file] },
    })

    await waitFor(() => expect(screen.getByTestId('composer-chips').textContent).toContain('payroll.csv'))

    rerender(<ComposerHarness><Composer {...props} conversationId="chat-2" /></ComposerHarness>)
    expect(screen.queryByTestId('composer-chips')).toBeNull()
    // LIVENESS — the composer is still mounted and usable in the sibling, so the chip's absence is
    // the switch clearing it rather than a component that failed to render.
    expect(box()).toBeTruthy()
  })
})

describe('one composer, both kinds (R72)', () => {
  it('behaves identically whichever kind mounted it — the placeholder is the only difference', () => {
    // The placeholder is a HINT, not a mode: nothing downstream reads it, and every behaviour
    // above is a property of this component rather than of the surface that mounted it.
    const { rerender, props } = draw({ placeholder: 'Describe what you need…' })
    type('same in both')
    const first = box().value

    rerender(<ComposerHarness><Composer {...props} placeholder="What are we planning?" /></ComposerHarness>)
    expect(box().value).toBe(first)
    expect(box().getAttribute('placeholder')).toBe('What are we planning?')
    expect(send().getAttribute('aria-disabled')).toBe('false')
  })
})

/**
 * WHICH REASON WINS WHEN MORE THAN ONE IS TRUE.
 *
 * `unavailableReason` is a four-arm cascade, and every other test in this file drives exactly one
 * arm — so the ORDER, which is the only thing a cascade encodes, was never actually asserted.
 * Reordering it would have broken nothing.
 *
 * The order is by immediacy, and the offer is deliberately LAST: the first three describe
 * something happening right now — the citizen's own text is too long, a reply is arriving, their
 * app is being built — while a pending offer describes a question still waiting. The offer also
 * stays pending for the whole round trip its own Build press starts, so putting it first told a
 * citizen to "choose one of the two above" while the build they had just chosen was starting.
 */
describe('the send-unavailable cascade, with more than one arm true', () => {
  const OVER_CAP = 'x'.repeat(20000)
  const offer = { toolCallId: 'tc-1', conversationId: 'chat-1', spent: false, onBuild: vi.fn(), onKeepPlanning: vi.fn() }
  const gate = {
    blocked: true,
    reason: 'Building your app — keep typing if you like; send unlocks when it is done.',
  }

  it('the citizen’s own text beats everything else', () => {
    draw({ isRunning: true, gate, offer })
    type(OVER_CAP)
    expect(gateNote()?.textContent).toMatch(/too long|shorten|character/i)
  })

  it('a reply arriving beats a build gate and a waiting offer', () => {
    draw({ isRunning: true, gate, offer })
    type('short enough')
    expect(gateNote()?.textContent).toMatch(/send unlocks when it is done/i)
  })

  it('a build running beats a waiting offer', () => {
    draw({ isRunning: false, gate, offer })
    type('short enough')
    expect(gateNote()?.textContent).toMatch(/Building your app/i)
  })

  it('the offer speaks only when nothing more immediate is true', () => {
    draw({ isRunning: false, offer })
    type('short enough')
    expect(gateNote()?.textContent).toBe(OFFER_GATE_NOTE)
  })

  it('says exactly ONE of them, however many are true', () => {
    const { container } = draw({ isRunning: true, gate, offer })
    type(OVER_CAP)
    expect(screen.getAllByTestId('composer-gate-note')).toHaveLength(1)
    noRealDisabled(container)
  })
})
