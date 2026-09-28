/**
 * "Another project is open" — the dialog that asks before another project's workspace is
 * released; the one-workspace rule and the refusal that raises this are `reclaim.py`'s.
 *
 * It is a CHOICE, not an error: no red, no alert glyph, no apology. Saving first is offered
 * because the platform can do it on the citizen's behalf, but it stays THEIR call — nothing
 * here saves automatically. `dirty === null` is UNKNOWN, not clean, so the copy hedges ("may
 * have unsaved changes") rather than calling work safe that nobody checked. Focus is part of
 * the contract: this appears unprompted over work in progress, so it takes focus, holds it and
 * gives it back, and the trap survives the busy window where all three buttons disable, focus
 * falls to `<body>` and the keydown handler stops firing, as `ProjectDescriptionEditor` does.
 */
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { FolderOpen, Hammer } from 'lucide-react'
import { BusyGlyph, WaitingLine } from '../ui/Waiting'
import type { HandoverStep, ReclaimBlocked } from '../../utils/buildSessionApi'

interface Props {
  blocked: ReclaimBlocked
  /**
   * THE PROJECT BEING STARTED, and the reason this prop exists: the question a citizen asks is
   * "can I build THIS one?", so the dialog answers that one first and the incumbent is the
   * obstacle rather than the subject. The refusal carries only the incumbent, so this name has
   * to be handed in by whoever made the call that was refused.
   *
   * `null` when the caller does not know it; the copy then names only the incumbent.
   */
  startingProjectName?: string | null
  /** Save the other project, then release it. Rejects if the save fails — the dialog stays
   *  open and says so, because a failed save that closed the workspace anyway is the exact
   *  data loss this whole dialog exists to prevent.
   *
   *  When `blocked.building`, the handler stops the build FIRST — the save and the release
   *  both refuse while an agent is writing, and a save that slipped through would store a
   *  half-finished tree as the version a Relaunch restores. */
  onSaveAndSwitch: () => Promise<void>
  /** Release without saving. The user was told; this is them accepting the cost — and when
   *  `blocked.building` the cost is larger, because it includes work the agent has not
   *  finished writing. The copy says so. */
  onSwitchAnyway: () => Promise<void>
  onCancel: () => void
  /**
   * WHICH STEP THE HAND-OVER HAS REACHED, or `null` before it starts.
   *
   * Owned by the caller rather than by this dialog, because the caller is what performs the
   * sequence — and the sequence outlives the press: it stops the other project, waits for that to
   * genuinely finish, saves, releases, starts this project, and only then opens the chat. A
   * dialog that guessed at its own progress would be narrating a story rather than reporting one.
   */
  step?: HandoverStep | null
}

/**
 * TWO SITUATIONS. A BUILDING project has an agent writing: no settled tree, both Save and
 * Release refused until it stops, and work-in-progress is given up. Otherwise its tree was not
 * questioned, so the copy says it "may have" unsaved changes and offers Save. Both lead with the
 * app being STARTED (not the one being left) and NAME the project whose changes are lost — the
 * other project is STOPPED, not moved, and no sentence here may imply otherwise.
 */
function copyFor(
  blocked: ReclaimBlocked,
  startingProjectName: string | null,
): {
  title: string
  body: string
  save: string
  discard: string
  /**
   * WHAT IS HAPPENING IN THERE RIGHT NOW, said BEFORE the citizen chooses. A separate sentence
   * rather than a third arm, because an assistant answering a question is not a build.
   *
   * `null` on the `building` arm: that copy already says the build is running and has to stop.
   */
  working: string | null
} {
  // The app being started, in the first line. Falls back to the plain phrasing when the caller
  // could not name it, rather than rendering an empty pair of quotes.
  const starting = startingProjectName ? `“${startingProjectName}”` : 'this app'
  const incumbent = `“${blocked.projectName}”`
  const oneAtATime = 'You can work on one app at a time.'

  // NO INFRASTRUCTURE VOCABULARY ANYWHERE IN THIS FUNCTION. Not "container", not "sandbox", not
  // "workspace slot" — a citizen asked for an app and is being told they can have one at a time.
  const working = blocked.agentWorking
    ? `The assistant is still working in ${incumbent} right now. Carrying on will stop it where it is.`
    : null

  if (blocked.building) {
    return {
      title: `Start ${starting}?`,
      body: `${oneAtATime} ${incumbent} is still being built, so it has to stop first. Stopping keeps everything the assistant has written into ${incumbent} so far — it stays where it is, and you can pick it up again later.`,
      save: `Save ${incumbent} and stop it`,
      discard: `Stop ${incumbent} without saving`,
      // Already said, in the arm's own words. See `working` above.
      working: null,
    }
  }

  return {
    title: `Start ${starting}?`,
    body: `${oneAtATime} ${incumbent} will stop so ${starting} can run, and it may have changes that are not saved yet. Save it first and it comes back exactly as you left it; stop without saving and those changes go.`,
    save: `Save ${incumbent} and stop it`,
    discard: `Stop ${incumbent} without saving`,
    working,
  }
}

/**
 * WHAT THE DIALOG SAYS WHILE IT WORKS, because these take real time — a STATUS SURFACE, not
 * just a question: a spinner alone for the thirty seconds a stop-then-start takes reads as a
 * hung dialog standing in front of a message the citizen has typed. Plain language, no
 * mechanism named. Ends at "Starting your app…": the navigate that opens the chat unmounts
 * this surface, so a fifth line could never be read — the chat narrates its own arrival.
 */
const STEP_SAYS: Record<HandoverStep, string> = {
  stopping: 'Closing the other app…',
  saving: 'Saving it first…',
  releasing: 'Putting it away…',
  starting: 'Starting your app…',
}

export default function ReclaimWorkspaceDialog({
  blocked,
  startingProjectName = null,
  onSaveAndSwitch,
  onSwitchAnyway,
  onCancel,
  step = null,
}: Props): React.ReactElement {
  const [busy, setBusy] = useState<null | 'save' | 'discard'>(null)
  const [error, setError] = useState<string | null>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const saveRef = useRef<HTMLButtonElement>(null)

  // Whatever had focus when this appeared — almost always the composer the user was typing
  // in. Captured once on mount and restored on unmount, so dismissing the dialog returns
  // them to the caret they left rather than to the top of the document.
  const returnFocusRef = useRef<Element | null>(null)
  useEffect(() => {
    returnFocusRef.current = document.activeElement
    saveRef.current?.focus()
    return () => {
      const target = returnFocusRef.current
      if (target instanceof HTMLElement && document.contains(target)) target.focus()
    }
  }, [])

  // A busy request disables all three buttons at once, so the browser blurs whichever held
  // focus and it lands on `<body>` — outside this card, where `onKeyDown` no longer fires and
  // both the Tab trap and Escape are silently dead for the rest of the request. Park focus on
  // the card itself (tabIndex={-1} makes it a valid target) so something inside always has it.
  useEffect(() => {
    if (busy) cardRef.current?.focus()
  }, [busy])

  // Escape cancels, except while a request is in flight — closing then would leave a save or
  // release running against a dialog that can no longer report what happened to it.
  // Tab/Shift+Tab cycle within the card; when every button is disabled the focusable list is
  // empty, so hold the trap on the card rather than bailing and letting Tab reach the page.
  const onKeyDownTrap = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key === 'Escape') {
      if (!busy) onCancel()
      return
    }
    if (e.key !== 'Tab') return
    const focusables = cardRef.current?.querySelectorAll<HTMLElement>('button:not([disabled])')
    if (!focusables || focusables.length === 0) {
      e.preventDefault()
      cardRef.current?.focus()
      return
    }
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  // try/finally, so a rejection re-arms the buttons instead of leaving the dialog disarmed and
  // unclosable — the failure mode `ProjectDeleteDialog` currently has.
  const run = async (which: 'save' | 'discard', fn: () => Promise<void>): Promise<void> => {
    setBusy(which)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That did not work. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  const copy = copyFor(blocked, startingProjectName)
  const Icon = blocked.building ? Hammer : FolderOpen

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope"
      role="dialog"
      aria-modal="true"
      aria-labelledby="reclaim-title"
    >
      <div className="absolute inset-0 bg-black/40" onClick={busy ? undefined : onCancel} />
      <div
        ref={cardRef}
        tabIndex={-1}
        onKeyDown={onKeyDownTrap}
        className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6 focus:outline-none"
      >
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-xl bg-bial-bg flex items-center justify-center flex-shrink-0">
            <Icon size={17} className="text-primary" />
          </div>
          <h3 id="reclaim-title" className="text-base font-bold text-tertiary">
            {copy.title}
          </h3>
        </div>

        <p className="text-sm text-neutral mt-3 leading-relaxed">{copy.body}</p>

        {/* SAID BEFORE THE CHOICE, not after it. Whether the other project's agent is working
            changes what the citizen is agreeing to, so it cannot arrive as a consequence. */}
        {copy.working !== null && (
          <p data-testid="reclaim-agent-working" className="mt-2 text-sm font-semibold text-tertiary leading-relaxed">
            {copy.working}
          </p>
        )}

        {/* THE NARRATION, in place of a silent spinner. `role="status"` so it is announced as it
            changes; permanently reserved space is not needed because the buttons below stay put. */}
        {busy !== null && step !== null && (
          <p data-testid="reclaim-step" role="status" className="mt-3 text-sm text-neutral">
            {/* THE ELAPSED COUNT IS THE POINT, not the glyph. Measured in production, this
                sequence runs stop 5s -> save 40s -> release 31s: seventy-six seconds during which
                the only thing that changed was a sentence, every thirty. Under
                `prefers-reduced-motion` — the default on the Windows VMs this was reported from —
                nothing changed at all, and the dialog was read as hung. */}
            {/* `active` KEYED TO THE STEP, not hardcoded true. `useElapsedSeconds` restarts on
                the false->true transition, so a constant `true` across stop -> save -> release ->
                start reports the age of the DIALOG rather than of the step being narrated — the
                exact thing that hook's docblock says it avoids. Remounting per step via `key` is
                what actually delivers it. */}
            <WaitingLine key={step} label={STEP_SAYS[step]} active />
          </p>
        )}

        {error ? (
          <p role="alert" className="text-sm text-danger mt-3 leading-relaxed">
            {error}
          </p>
        ) : null}

        <div className="flex flex-col gap-2.5 mt-5">
          {/* THE CLEAN ARM HAS NO SAVE BUTTON. `copy.save` is null exactly when the server
              confirmed there is nothing to save, and a Save offered there is a control whose only
              possible outcome is a no-op the person will read as a failure. */}
          {copy.save !== null && (
            <button
              ref={saveRef}
              type="button"
              disabled={busy !== null}
              onClick={() => void run('save', onSaveAndSwitch)}
              className="w-full flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy === 'save' ? <BusyGlyph size={15} /> : null} {copy.save}
            </button>
          )}
          <button
            // The focus target when there is no Save button — the dialog must still take focus on
            // open, or a keyboard user never learns it exists.
            ref={copy.save === null ? saveRef : undefined}
            type="button"
            disabled={busy !== null}
            onClick={() => void run('discard', onSwitchAnyway)}
            className="w-full flex items-center justify-center gap-2 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy === 'discard' ? <BusyGlyph size={15} /> : null}{' '}
            {copy.discard}
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={onCancel}
            className="w-full text-neutral hover:text-tertiary font-semibold py-2 rounded-xl transition text-sm disabled:opacity-50"
          >
            {/* "Keep building" rather than "Cancel" while a build is live: cancelling the
                DIALOG and cancelling the BUILD are two different things, and a user who has
                just been offered two Stop buttons should not have to guess which one this
                undoes. */}
            {blocked.building ? 'Keep building' : 'Cancel'}
          </button>
        </div>
      </div>
    </div>
  )
}
