/**
 * ACTIVITY GROUPS — drawn by the parts, sealed by adjacency (R30–R35c).
 *
 * The two cases worth reading first are AE15 and the two-group case: they are what prove the
 * grouping is structural rather than conditional. A turn with no tool parts renders no element
 * because there is nothing to render, not because a renderer decided to hide a bar; and text
 * between two runs of tool calls produces two groups because the primitive coalesces ADJACENT
 * parts. Neither has any code behind it, which is the point.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'

import ChatThread from '../ChatThread'
import ChatRuntimeProvider from '../runtime/ChatRuntimeProvider'
import { groupLabel } from '../ActivityGroup'
import type { ChatMessage, MessagePart } from '../../../utils/messageTypes'
import type { StepItem } from '../../../utils/turnStreamApi'

afterEach(cleanup)

const stepPart = (seq: number, label: string, state: StepItem['state'] = 'ok'): MessagePart => ({
  type: 'step',
  step: { type: 'step', seq, tool: 'bash', label, state, hidden: false },
})

const textPart = (text: string): MessagePart => ({ type: 'text', text })

/** The tree under test, so a scenario can RE-RENDER it with different parts — which is the only
 *  way to observe a group SEALING, and therefore the only way to test the peek closing itself. */
function tree(parts: MessagePart[], opts: { isRunning?: boolean; interrupted?: boolean } = {}) {
  const message: ChatMessage = { id: 'a1', role: 'assistant', parts, seq: 1 }
  return (
    <ChatRuntimeProvider
      messages={[message]}
      isRunning={opts.isRunning ?? false}
      onNew={vi.fn().mockResolvedValue(undefined)}
      onCancel={vi.fn().mockResolvedValue(undefined)}
    >
      <ChatThread interruptedMessageIds={opts.interrupted ? new Set(['a1']) : undefined} />
    </ChatRuntimeProvider>
  )
}

function mount(parts: MessagePart[], opts: { isRunning?: boolean; interrupted?: boolean } = {}) {
  return render(tree(parts, opts))
}

const groups = () => screen.queryAllByTestId('activity-group')
const trigger = (i = 0) => within(groups()[i]!).getByTestId('activity-group-trigger')

describe('a group exists only when a tool actually ran', () => {
  it('AE15: a turn with a text part and NO tool calls renders zero group elements', () => {
    mount([textPart('Right now only you can. Feedback is saved against the person who sent it.')])

    expect(groups()).toHaveLength(0)
    expect(screen.queryByTestId('activity-group-trigger')).toBeNull()
    // The liveness half, so the two absences above cannot false-green on a crashed render.
    expect(screen.getByTestId('assistant-message').textContent).toContain('Right now only you can')
  })

  it('AE15: the same turn shows no group WHILE RUNNING either', () => {
    mount([textPart('Right now only you can.')], { isRunning: true })

    expect(groups()).toHaveLength(0)
    expect(screen.getByTestId('assistant-message').textContent).toContain('Right now only you can')
  })
})

describe('sealing is adjacency, and there is no seal logic', () => {
  it('AE13: twelve adjacent tool calls followed by text make ONE group above the paragraph', () => {
    const parts = [
      ...Array.from({ length: 12 }, (_, i) => stepPart(i + 1, `Step ${i + 1}`)),
      textPart('The picker is in and wired to the list.'),
    ]
    mount(parts)

    expect(groups()).toHaveLength(1)
    expect(trigger().textContent).toContain('12 steps')
  })

  it('text, three calls, text, two calls → TWO groups, counts 3 and 2', () => {
    mount([
      textPart('Adding the status picker now.'),
      stepPart(1, 'Reading your restaurants screen'),
      stepPart(2, 'Checking what a status can be'),
      stepPart(3, 'Making sure everything fits together'),
      textPart('The picker is in.'),
      stepPart(4, 'Wrapping up the build'),
      stepPart(5, 'Build verified'),
    ])

    expect(groups()).toHaveLength(2)
    expect(trigger(0).textContent).toContain('3 steps')
    expect(trigger(1).textContent).toContain('2 steps')
  })

  it('a turn where the agent never speaks and calls nine tools makes ONE group', () => {
    mount(Array.from({ length: 9 }, (_, i) => stepPart(i + 1, `Step ${i + 1}`)))

    expect(groups()).toHaveLength(1)
    expect(trigger().textContent).toContain('9 steps')
  })
})

describe('a sealed group collapses to a count, and opens where it sits', () => {
  it('AE13: is collapsed at rest, and pressing it lists the rows in place', () => {
    // The client's decision, 2026-09-01: sealed means collapsed, including the last group of a
    // turn. Vercel's AI Elements auto-open completed tools; we deliberately do not.
    mount([
      stepPart(1, 'Reading your visitor screen'),
      stepPart(2, 'Adding the Out column'),
      textPart('Done.'),
    ])

    expect(screen.queryByTestId('activity-group-rows')).toBeNull()
    expect(trigger().getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(trigger())

    const rows = screen.getByTestId('activity-group-rows')
    expect(within(rows).getByText('Reading your visitor screen')).toBeTruthy()
    expect(within(rows).getByText('Adding the Out column')).toBeTruthy()
    // No new scroll container and no side panel — it opens where it sits.
    expect(rows.querySelectorAll('.overflow-y-auto, .overflow-y-scroll')).toHaveLength(0)
  })

  it('★ is a bordered chip on the board\'s own ground, not bare text in the transcript', () => {
    // AN EARLIER PASS READ `BuildChat` AS DRAWING NO CHROME AT ALL and said so at length in the
    // component. `ActivityAnatomy` is the artboard that specifies this component, and it draws
    // `border:1px solid #E2E8F0; background:#FCFDFD; border-radius:10px`. Without it the group was
    // a line of text with nothing to say it was a receipt rather than a sentence.
    mount([stepPart(1, 'Reading your visitor screen'), textPart('Done.')])

    const container = screen.getByTestId('activity-group-container')
    expect(container.className).toMatch(/border-bial-border/)
    expect(container.className).toMatch(/rounded-\[10px\]/)
    expect(trigger().className).toMatch(/bg-canvas-group/)
    // It hugs its contents rather than spanning the transcript.
    expect(container.className).toMatch(/w-fit/)
  })

  it('★ gives a problem group its own tint, and only once it is terminal', () => {
    // `ActivityAnatomy` panel 4: "nothing is hidden when something went wrong". The container, its
    // header and its label all change; the fail-open and the tint follow the SAME predicate, so a
    // group cannot be red and shut, or open and neutral.
    mount([
      stepPart(1, 'Working on your app', 'ok'),
      stepPart(2, 'Working on your app', 'failed'),
      textPart('Not green yet — continuing.'),
    ])
    const container = screen.getByTestId('activity-group-container')
    expect(container.getAttribute('data-problem')).toBe('true')
    expect(container.className).toMatch(/border-problem-edge/)
    expect(trigger().className).toMatch(/bg-problem-ground/)
    expect(trigger().getAttribute('aria-expanded')).toBe('true')

    cleanup()
    // MID-TURN IT IS NEITHER TINTED NOR OPEN — expanding then would move what the reader is
    // reading, and a failure that is still being recovered from is not yet a problem to report.
    mount([stepPart(1, 'Working on your app', 'failed'), stepPart(2, 'Working on your app', 'pending')])
    expect(screen.getByTestId('activity-group-container').getAttribute('data-problem')).toBeNull()
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
  })

  it('★ draws an icon per KIND of step, and keeps the state glyph for one that failed', () => {
    // `ActivityAnatomy`: one icon per kind, not one tick for everything. The kind is derived from
    // the label because the wire carries no kind — see `stepIconFor` for why that is an honest
    // limit rather than a shortcut. A FAILED step keeps its cross: what went wrong outranks what
    // was being attempted, and shape rather than colour carries it.
    mount([
      stepPart(1, 'Reading your visitor screen', 'ok'),
      stepPart(2, 'Adding the Out column', 'ok'),
      stepPart(3, 'Working on your app', 'failed'),
      textPart('Done.'),
    ])
    const tiles = screen.getByTestId('activity-glyphs').children
    // Three tiles, and the first two carry DIFFERENT icons — one tick for everything is exactly
    // what this replaces. Compared by their rendered markup, since both are inline SVGs.
    expect(tiles).toHaveLength(3)
    expect(tiles[0]!.innerHTML).not.toBe(tiles[1]!.innerHTML)
    expect(tiles[2]!.textContent).toMatch(/failed/i)
  })
})

describe('R34 — a group that hit a problem opens by itself, but never mid-turn', () => {
  it('AE14: a sealed group containing a failed step is already expanded', () => {
    mount([
      stepPart(1, 'Working on your app', 'ok'),
      stepPart(2, 'Working on your app', 'failed'),
      textPart('Not green yet — continuing.'),
    ])

    expect(trigger().getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByTestId('activity-group-rows')).toBeTruthy()
    expect(trigger().textContent).toContain('one problem')
  })

  it('AE14 (the negative half): the same group WHILE RUNNING stays collapsed', () => {
    // Expanding mid-turn moves what the reader is reading, so the fail-open waits for terminal.
    mount([
      stepPart(1, 'Working on your app', 'failed'),
      stepPart(2, 'Working on your app', 'pending'),
    ])

    expect(trigger().getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByTestId('activity-group-rows')).toBeNull()
  })

  it('the reader can close a fail-opened group and it stays closed', () => {
    mount([stepPart(1, 'Working on your app', 'failed'), textPart('Continuing.')])

    expect(trigger().getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(trigger())
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
  })
})

describe('R31 — a live group names what is happening NOW and grows in place', () => {
  it('★ stays COLLAPSED while it runs, with the current step on one quiet line beneath it', () => {
    // THE OWNER'S RULING OF 2026-09-02, which amends `ActivityAnatomy` panel 2 in place. The board
    // draws a live group OPEN with the current step named inside it; the owner does not want the
    // working detail on screen. So the row is a count with icons accumulating in it, and the
    // sentence moves to a line underneath. Mutation receipt: return `facts.currentLabel` from
    // `groupLabel`'s running arm again and the first two assertions go red together.
    mount([
      stepPart(1, 'Reading your restaurants screen', 'ok'),
      stepPart(2, 'Checking what a status can be', 'ok'),
      stepPart(3, 'Making sure everything fits together', 'pending'),
    ])

    expect(trigger().getAttribute('aria-expanded')).toBe('false')
    expect(trigger().textContent).toContain('3 steps')
    expect(trigger().textContent).not.toContain('Making sure everything fits together')
    expect(screen.getByTestId('activity-group-now').textContent).toBe('Making sure everything fits together')
    // Icons accumulate in the row itself — one per call, oldest on the left.
    expect(screen.getByTestId('activity-glyphs').childElementCount).toBe(3)
  })

  it('★ a sealed group has no quiet line — its steps are in the receipt, one press away', () => {
    mount([stepPart(1, 'Reading your restaurants screen', 'ok'), textPart('Done.')])
    expect(screen.queryByTestId('activity-group-now')).toBeNull()
    // LIVENESS: the group rendered, it simply has nothing happening right now.
    expect(trigger().textContent).toContain('1 step')
  })

  it('★ a glance inside a RUNNING group closes itself when the group seals', () => {
    // "A glance inside is temporary, not a new resting state" (owner ruling, 2026-09-02). Cleared
    // to "the reader has not decided" rather than to closed, so a group that also FAILED still
    // opens itself afterwards.
    const view = mount([stepPart(1, 'Reading your restaurants screen', 'pending')])
    fireEvent.click(trigger())
    expect(trigger().getAttribute('aria-expanded')).toBe('true')

    view.rerender(tree([stepPart(1, 'Reading your restaurants screen', 'ok')]))

    expect(trigger().getAttribute('aria-expanded')).toBe('false')
  })

  it('AE16: a Plan-kind message with four read steps produces the same shape as a Build one', () => {
    const parts = Array.from({ length: 4 }, (_, i) => stepPart(i + 1, `Reading file ${i + 1}`))
    const a = mount([...parts, textPart('Here is what I found.')])
    const html = a.container.innerHTML
    cleanup()

    const b = mount([
      ...parts.map((p) => ({ ...p })),
      textPart('Here is what I found.'),
    ])
    expect(b.container.innerHTML).toBe(html)
  })
})

describe('R35b — a step with no label still says something', () => {
  it('renders the unrecognised-tool phrase, never an empty row and never the tool name', () => {
    mount([stepPart(1, ''), textPart('Done.')])

    fireEvent.click(trigger())
    const rows = screen.getByTestId('activity-group-rows')
    expect(rows.textContent).toContain('Working on your app')
    expect(rows.textContent).not.toContain('bash')
  })
})

describe('R35c — an interrupted turn does not read like a finished one', () => {
  it('says so in the sealed label, and the two labels differ', () => {
    const finished = mount([stepPart(1, 'Working on your app'), textPart('Done.')])
    const finishedLabel = trigger().textContent
    cleanup()

    mount([stepPart(1, 'Working on your app'), textPart('Done.')], { interrupted: true })
    const stoppedLabel = trigger().textContent

    expect(stoppedLabel).toContain('stopped before it finished')
    expect(stoppedLabel).not.toBe(finishedLabel)
    expect(finished).toBeTruthy()
  })
})

describe('groupLabel — the wording, unit-tested away from the DOM', () => {
  const facts = (over: Partial<Parameters<typeof groupLabel>[0]> = {}) => ({
    count: 9,
    failures: 0,
    running: false,
    currentLabel: 'Working on your app',
    ...over,
  })

  it('is a count when sealed and clean', () => {
    expect(groupLabel(facts(), false)).toBe('9 steps')
  })

  it('is singular for one step', () => {
    expect(groupLabel(facts({ count: 1 }), false)).toBe('1 step')
  })

  it('names one problem, and counts more than one', () => {
    expect(groupLabel(facts({ count: 4, failures: 1 }), false)).toBe('4 steps · one problem')
    expect(groupLabel(facts({ count: 4, failures: 3 }), false)).toBe('4 steps · 3 problems')
  })

  it('is a COUNT while running too, whatever the failures — the current step is not the row\'s', () => {
    // A COUNT OF PROBLEMS WHILE THE RUN IS STILL GOING describes something that may yet be
    // recovered from, which is why the suffixes are sealed-only. Mutation receipt: return
    // `facts.currentLabel` from the running arm again and this goes red.
    expect(groupLabel(facts({ running: true, failures: 2 }), false)).toBe('9 steps')
    expect(groupLabel(facts({ running: true, failures: 2 }), false)).not.toContain('problem')
  })

  it('interruption outranks a failure count, because it explains the count', () => {
    expect(groupLabel(facts({ count: 4, failures: 1 }), true)).toBe(
      '4 steps · stopped before it finished',
    )
  })
})

/**
 * R66's SECOND ANNOUNCEMENT.
 *
 * The surface used to pass a hardcoded `null` for this, under a comment saying the group announced
 * it itself. The group had no live region and no announcer, so the effect could never fire outside
 * `Announcer`'s own unit test — a screen-reader user heard that the agent had started and never
 * heard what it did. These pin the WIRING; the hook's own rules stay in `Announcer.test.tsx`.
 */
describe('a group reports what it amounted to, once, as it seals', () => {
  const withReporter = (
    parts: MessagePart[],
    onGroupSealed: (summary: string) => void,
    opts: { isRunning?: boolean; interrupted?: boolean } = {},
  ) => (
    <ChatRuntimeProvider
      messages={[{ id: 'a1', role: 'assistant', parts, seq: 1 }]}
      isRunning={opts.isRunning ?? false}
      onNew={vi.fn().mockResolvedValue(undefined)}
      onCancel={vi.fn().mockResolvedValue(undefined)}
    >
      <ChatThread
        interruptedMessageIds={opts.interrupted ? new Set(['a1']) : undefined}
        onGroupSealed={onGroupSealed}
      />
    </ChatRuntimeProvider>
  )

  /**
   * Drive the REAL transition: a step arrives pending, then the same step resolves. Rendering the
   * finished state directly is what the group must stay silent for — that is a reload, not
   * something the reader just watched happen.
   */
  const runThenSeal = (
    running: MessagePart[],
    sealedParts: MessagePart[],
    onGroupSealed: (summary: string) => void,
    opts: { interrupted?: boolean } = {},
  ) => {
    const { rerender } = render(withReporter(running, onGroupSealed, { isRunning: true }))
    rerender(withReporter(sealedParts, onGroupSealed, { isRunning: false, ...opts }))
  }

  it('reports the sealed label — the same sentence the trigger shows', () => {
    const onGroupSealed = vi.fn()
    runThenSeal(
      [stepPart(1, 'Reading your data', 'pending'), stepPart(2, 'Writing the page', 'pending')],
      [stepPart(1, 'Reading your data'), stepPart(2, 'Writing the page', 'failed')],
      onGroupSealed,
    )
    expect(onGroupSealed).toHaveBeenCalledTimes(1)
    expect(onGroupSealed).toHaveBeenCalledWith('2 steps · one problem')
  })

  it('says nothing while a step is still running', () => {
    const onGroupSealed = vi.fn()
    render(withReporter([stepPart(1, 'Reading your data', 'pending')], onGroupSealed, { isRunning: true }))
    expect(onGroupSealed).not.toHaveBeenCalled()
    // LIVENESS — the group IS on screen, so the silence is the running check rather than a group
    // that failed to render at all.
    expect(groups()).toHaveLength(1)
  })

  it('says nothing for a group that was ALREADY finished when the chat opened', () => {
    // THE ONE THAT MATTERS. R66 announces what just happened. A finished chat with past builds in
    // it renders sealed groups on mount, and announcing those meant a reader who opened a
    // conversation heard a summary of work nobody had just done — once per historical group, the
    // last of them winning the live region.
    const onGroupSealed = vi.fn()
    render(withReporter([stepPart(1, 'Reading your data'), stepPart(2, 'Writing the page')], onGroupSealed))
    expect(onGroupSealed).not.toHaveBeenCalled()
    expect(groups()).toHaveLength(1)
  })

  it('carries the stopped wording when the turn was interrupted', () => {
    const onGroupSealed = vi.fn()
    runThenSeal(
      [stepPart(1, 'Reading your data', 'pending')],
      [stepPart(1, 'Reading your data')],
      onGroupSealed,
      { interrupted: true },
    )
    expect(onGroupSealed).toHaveBeenCalledWith('1 step · stopped before it finished')
  })

  it('does not re-announce when the group is merely re-rendered', () => {
    const onGroupSealed = vi.fn()
    const running = [stepPart(1, 'Reading your data', 'pending')]
    const done = [stepPart(1, 'Reading your data')]
    const { rerender } = render(withReporter(running, onGroupSealed, { isRunning: true }))
    rerender(withReporter(done, onGroupSealed))
    rerender(withReporter(done, onGroupSealed))
    expect(onGroupSealed).toHaveBeenCalledTimes(1)
  })
})
