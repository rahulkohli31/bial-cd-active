import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/react'
import ReclaimWorkspaceDialog from '../ReclaimWorkspaceDialog'

afterEach(cleanup)

const BLOCKED = {
  projectId: 'p-a',
  projectName: 'Lost & Found',
  dirty: true as boolean | null,
  building: false, agentWorking: false,
  isSharedView: false,
}
/** The refusal a project whose agent is mid-write produces: `building`, and `dirty` null
 *  because the server deliberately did not probe a tree being written to. */
const BUILDING = { ...BLOCKED, dirty: null, building: true }

function setup(over = {}) {
  const props = {
    blocked: BLOCKED,
    // The dialog leads with the app being STARTED — the unnamed case has its own test below.
    startingProjectName: 'Visitor Log',
    onSaveAndSwitch: vi.fn().mockResolvedValue(undefined),
    onSwitchAnyway: vi.fn().mockResolvedValue(undefined),
    onCancel: vi.fn(),
    ...over,
  }
  render(<ReclaimWorkspaceDialog {...props} />)
  return props
}

describe('ReclaimWorkspaceDialog — naming, both actions, and a failed save', () => {
  it('names the project holding the workspace, so the user knows what they are choosing about', () => {
    setup()
    expect(screen.getByRole('dialog').textContent).toMatch(/Lost & Found/)
    expect(screen.getByRole('dialog').textContent).toMatch(/has changes that are not saved yet/i)
  })

  it('HEDGES when dirty is unknown — never claims work is safe that nobody checked', () => {
    setup({ blocked: { ...BLOCKED, dirty: null } })
    const text = screen.getByRole('dialog').textContent ?? ''
    expect(text).toMatch(/may have changes that are not saved yet/i)
    expect(text).not.toMatch(/\bhas changes that are not saved yet/i)
  })

  it('offers save-and-switch as the primary action', async () => {
    const props = setup()
    fireEvent.click(screen.getByRole('button', { name: /save “Lost & Found” and stop it/i }))
    await waitFor(() => expect(props.onSaveAndSwitch).toHaveBeenCalledTimes(1))
    expect(props.onSwitchAnyway).not.toHaveBeenCalled()
  })

  it('lets the user switch without saving — they were told; the choice is theirs', async () => {
    const props = setup()
    fireEvent.click(screen.getByRole('button', { name: /stop “Lost & Found” without saving/i }))
    await waitFor(() => expect(props.onSwitchAnyway).toHaveBeenCalledTimes(1))
    expect(props.onSaveAndSwitch).not.toHaveBeenCalled()
  })

  it('a FAILED save re-arms the buttons and says why, instead of wedging the dialog', async () => {
    // The failure mode ProjectDeleteDialog has: busy set, no finally, so a rejection leaves the
    // modal open, disarmed and unclosable. Here a failed save must leave the user able to retry
    // — and must NOT have released the workspace, which is the whole point of saving first.
    const props = setup({
      onSaveAndSwitch: vi.fn().mockRejectedValue(new Error('Could not save your work')),
    })
    fireEvent.click(screen.getByRole('button', { name: /save “Lost & Found” and stop it/i }))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Could not save your work')
    const retry = screen.getByRole('button', { name: /save “Lost & Found” and stop it/i }) as HTMLButtonElement
    expect(retry.disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }))
    expect(props.onCancel).toHaveBeenCalled()
  })
})

/**
 * KEYBOARD, not clicks: a mouse never notices that Tab escapes, that Escape does nothing, or
 * that focus was never taken in the first place. These drive the dialog the way a keyboard
 * user does.
 */
describe('ReclaimWorkspaceDialog — focus and keyboard', () => {
  const card = (): HTMLElement => screen.getByRole('dialog').querySelector('[tabindex="-1"]')!

  it('takes focus on the primary action, so a keyboard user learns it appeared', () => {
    setup()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /save “Lost & Found” and stop it/i }))
  })

  it('Escape cancels', () => {
    const props = setup()
    fireEvent.keyDown(card(), { key: 'Escape' })
    expect(props.onCancel).toHaveBeenCalledTimes(1)
  })

  it('Escape does NOT cancel mid-request — closing would orphan a save in flight', async () => {
    let release = (): void => {}
    const props = setup({
      onSaveAndSwitch: vi.fn(() => new Promise<void>((r) => { release = r })),
    })
    fireEvent.click(screen.getByRole('button', { name: /save “Lost & Found” and stop it/i }))
    await waitFor(() =>
      expect((screen.getByRole('button', { name: /^cancel$/i }) as HTMLButtonElement).disabled).toBe(true),
    )
    fireEvent.keyDown(card(), { key: 'Escape' })
    expect(props.onCancel).not.toHaveBeenCalled()
    release()
  })

  it('Tab CYCLES inside the dialog instead of reaching the page behind it', () => {
    setup()
    const save = screen.getByRole('button', { name: /save “Lost & Found” and stop it/i })
    const cancel = screen.getByRole('button', { name: /^cancel$/i })

    cancel.focus() // last focusable
    fireEvent.keyDown(card(), { key: 'Tab' })
    expect(document.activeElement).toBe(save) // wrapped forward, not onto the page

    save.focus() // first focusable
    fireEvent.keyDown(card(), { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(cancel) // wrapped backward
  })

  it('HOLDS focus while every button is disabled, so the trap survives', async () => {
    // All three buttons share one `disabled={busy}`, so mid-save the dialog has zero focusable
    // elements. Without the card fallback the browser drops focus to <body>, the keydown
    // handler stops firing, and the trap is silently dead for the rest of the request.
    let release = (): void => {}
    setup({ onSaveAndSwitch: vi.fn(() => new Promise<void>((r) => { release = r })) })
    fireEvent.click(screen.getByRole('button', { name: /save “Lost & Found” and stop it/i }))

    await waitFor(() => expect(document.activeElement).toBe(card()))
    fireEvent.keyDown(card(), { key: 'Tab' })
    expect(document.activeElement).toBe(card()) // still inside, not on <body>
    release()
  })

  it('gives focus back to whatever raised it — the composer the user was typing in', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()

    const { unmount } = render(
      <ReclaimWorkspaceDialog
        blocked={BLOCKED}
        onSaveAndSwitch={vi.fn().mockResolvedValue(undefined)}
        onSwitchAnyway={vi.fn().mockResolvedValue(undefined)}
        onCancel={vi.fn()}
      />,
    )
    expect(document.activeElement).not.toBe(composer)

    unmount()
    expect(document.activeElement).toBe(composer)
    composer.remove()
  })
})

/**
 * The BUILDING variant states different facts, not a different tone — a project mid-write has no
 * settled tree to describe, so the idle copy's claims ("has unsaved changes", a working Save)
 * would be false here.
 */
describe('ReclaimWorkspaceDialog — a project that is still being built', () => {
  it('never claims unsaved changes — there is no settled tree to describe', () => {
    setup({ blocked: BUILDING })
    const text = screen.getByRole('dialog').textContent ?? ''
    expect(text).toMatch(/still being built/i)
    expect(text).not.toMatch(/unsaved changes/i)
  })

  it('offers STOP, because save and release both refuse while the agent writes', () => {
    // A "stop without saving" label beside a build is ambiguous about whose work is dropped,
    // and this is the case where it costs most: a build is running, so it genuinely does not
    // say WHOSE work goes. Both buttons name the project being stopped.
    setup({ blocked: BUILDING })
    expect(screen.getByRole('button', { name: /save “Lost & Found” and stop it/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /stop “Lost & Found” without saving/i })).toBeTruthy()
    // …and the verb is STOP, never "switch": save and release both refuse while the agent writes,
    // so a button promising a switch would promise something the server declines.
    expect(screen.queryByRole('button', { name: /switch/i })).toBeNull()
  })

  it('says "Keep building", not "Cancel" — two Stop buttons make Cancel ambiguous', () => {
    const props = setup({ blocked: BUILDING })
    const keep = screen.getByRole('button', { name: /keep building/i })
    fireEvent.click(keep)
    expect(props.onCancel).toHaveBeenCalledTimes(1)
  })

  it('still says the ordinary thing for an idle project', () => {
    setup()
    const text = screen.getByRole('dialog').textContent ?? ''
    expect(text).toMatch(/has changes that are not saved yet/i)
    expect(text).not.toMatch(/still being built/i)
    expect(screen.getByRole('button', { name: /^cancel$/i })).toBeTruthy()
  })

  it('routes both stop buttons to the same handlers the idle variant uses', async () => {
    // One flow, two labels — the ordering (stop → save → release) lives in the page, so the
    // dialog must not grow a second pair of callbacks that could diverge from it.
    const props = setup({ blocked: BUILDING })
    fireEvent.click(screen.getByRole('button', { name: /save “Lost & Found” and stop it/i }))
    await waitFor(() => expect(props.onSaveAndSwitch).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: /stop “Lost & Found” without saving/i }))
    await waitFor(() => expect(props.onSwitchAnyway).toHaveBeenCalledTimes(1))
  })
})

/**
 * THE CLEAN ARM — new, because the server changed. `dirty === false` could not reach this dialog
 * before (the guard reclaimed a clean incumbent silently), so the old tri-state copy
 * (`dirty === true ? 'has unsaved changes' : 'may have unsaved changes'`) becomes a lie here,
 * telling a person their confirmed-clean project "may have unsaved changes".
 */
describe('ReclaimWorkspaceDialog — a CLEAN incumbent', () => {
  const CLEAN = { ...BLOCKED, dirty: false as boolean | null }

  it('★ claims no unsaved work, in either the definite or the hedged wording', () => {
    // ASSERTING ONLY THAT THE DIALOG OPENS PASSES AGAINST THE WRONG COPY — that is what makes this
    // the assertion that matters. Both retired phrasings are checked, because the ternary that
    // produced them had two arms and only one of them was obviously wrong.
    setup({ blocked: CLEAN })
    const text = screen.getByRole('dialog').textContent ?? ''

    expect(text).not.toMatch(/has changes that are not saved/i)
    expect(text).not.toMatch(/may have changes that are not saved/i)
    expect(text).not.toMatch(/unsaved/i)
  })

  it('★ offers NO Save button for work that does not exist', () => {
    // A Save whose only possible outcome is a no-op teaches a person the dialog does not know what
    // it is talking about.
    setup({ blocked: CLEAN })

    expect(screen.queryByRole('button', { name: /save/i })).toBeNull()
    expect(screen.getByRole('button', { name: /stop “Lost & Found”/i })).toBeTruthy()
  })

  it('takes focus on the stop control when there is no Save to focus', async () => {
    // The primary action moved, and focus has to move with it — otherwise a keyboard user never
    // learns the dialog appeared, which is the whole reason focus is part of this contract.
    setup({ blocked: CLEAN })
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('button', { name: /stop “Lost & Found”/i })),
    )
  })

  it('still says the app STOPS, and never that anything moves', () => {
    setup({ blocked: CLEAN })
    const text = screen.getByRole('dialog').textContent ?? ''

    expect(text).toMatch(/will stop/i)
    // Nothing travels between projects, and no softener may imply it does.
    expect(text).not.toMatch(/\bmove[ds]?\b/i)
    expect(text).not.toMatch(/\btransfer/i)
    expect(text).not.toMatch(/bring it with/i)
  })
})

describe('ReclaimWorkspaceDialog — naming the project being started', () => {
  it('★ names the project being STARTED first, not the incumbent', () => {
    // ASSERT ORDER, NOT MERE PRESENCE: both names appear either way, and the bug was which one
    // came first.
    setup({ startingProjectName: 'Visitor Log' })
    const text = screen.getByRole('dialog').textContent ?? ''

    expect(text.indexOf('Visitor Log')).toBeGreaterThanOrEqual(0)
    expect(text.indexOf('Visitor Log')).toBeLessThan(text.indexOf('Lost & Found'))
  })

  it('★ names whose changes are lost, on the control AND in the sentence above it', () => {
    // "Switch without saving" beside a build is ambiguous: it does not say whether the unsaved
    // work being dropped belongs to the app they are starting or the one being stopped.
    setup({ startingProjectName: 'Visitor Log' })

    const discard = screen.getByRole('button', { name: /stop “Lost & Found” without saving/i })
    expect(discard).toBeTruthy()
    expect(screen.getByRole('dialog').textContent).toMatch(/“Lost & Found” will stop/i)
  })

  it('falls back to a plain phrase rather than quoting an empty name', () => {
    // A surface that has not resolved its project yet hands `null`. Rendering `“”` there would be
    // the framing fix introducing the exact defect the unattributed-slot arm exists to avoid.
    setup({ startingProjectName: null })
    const text = screen.getByRole('dialog').textContent ?? ''

    expect(text).not.toMatch(/[“"]\s*[”"]/)
    expect(text).toMatch(/start this app\?/i)
  })
})
