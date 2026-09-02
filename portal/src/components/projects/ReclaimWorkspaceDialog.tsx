/**
 * "Another project is open" — the #83 choice.
 *
 * A user gets one running workspace at a time. Opening a second project needs the first to
 * give up its container, and the container is where unsaved work lives. This used to happen
 * silently inside the incoming request: the other project was torn down, its unsaved work went
 * with it, and nobody was told. `manager.py`'s `finish_turn_sandbox` states the bargain the
 * platform actually keeps — "a user who loses work must have been told, twice" — and both
 * tellings fire on LEAVING the app, which switching projects is not. This dialog is that
 * missing telling.
 *
 * It is a CHOICE, not an error, and the copy says so: no red, no alert glyph, no apology. The
 * constraint is ordinary (one thing open at a time, like an app on a phone) and the remedy is
 * one click. Saving first is offered because the platform can do it on the user's behalf — the
 * work is one call away from durable — but it stays THEIR call, which is the whole point of
 * KTD-5e. Nothing here saves automatically.
 *
 * `dirty === null` is UNKNOWN, not clean: the server either reached the workspace and could
 * not ask it, or could not reach it at all. The copy hedges ("may have unsaved changes")
 * rather than promising, because telling someone their work is safe when nobody checked is
 * the one wrong answer available here.
 *
 * FOCUS IS PART OF THE CONTRACT, not a nicety. This is a modal that appears unprompted, in
 * front of work the user is mid-way through, and offers an irreversible choice — so it has to
 * take focus (or a keyboard user never learns it exists), hold it (or Tab wanders onto the
 * page behind and they act on a control the overlay is hiding), and give it back on close.
 * The trap has to survive the busy window specifically: all three buttons disable during a
 * save, focus falls to `<body>`, and the keydown handler stops firing — which is how #86
 * shipped a dialog whose trap silently disarmed at the one moment it mattered. See
 * `ProjectDescriptionEditor`, whose implementation this follows deliberately rather than
 * inventing a second one.
 */
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { FolderOpen, Hammer, Loader2 } from 'lucide-react'
import type { ReclaimBlocked } from '../../utils/buildSessionApi'

interface Props {
  blocked: ReclaimBlocked
  /**
   * THE PROJECT BEING STARTED — issue #161's framing half, and the reason this prop exists.
   *
   * Observed on a BIAL desk with the client watching: the modal opened with *"'Car pool apps' is
   * still open"* — the app the citizen was NOT working on. The question they are actually asking
   * is "can I build THIS one?", so the dialog has to answer that one first; the incumbent is the
   * obstacle, not the subject. The refusal itself carries only the incumbent, so the name of the
   * project being started has to be handed in by whoever made the call that was refused.
   *
   * `null` when the caller genuinely does not know it — a surface with no project resolved yet.
   * The copy then falls back to naming only the incumbent, which is what it always did.
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
}

/**
 * FOUR SITUATIONS NOW, NOT TWO, AND THE NEW ONE ARRIVES BECAUSE THE SERVER CHANGED.
 *
 * An idle project holds a workspace with a settled tree and the question is whether to save it. A
 * BUILDING project has an agent writing into it: there is no settled tree to describe, the server
 * refuses both Save and Release until the build stops, and what a person gives up by proceeding is
 * work in progress rather than work already done.
 *
 * The idle case then splits three ways on the tri-state, and the third arm is new. The old code
 * collapsed it — `dirty === true ? 'has unsaved changes' : 'may have unsaved changes'` — which was
 * CORRECT while a clean incumbent could never reach this dialog, because the server reclaimed it
 * silently. R94 removed that, so `dirty === false` now arrives, and the old ternary would tell a
 * person their confirmed-clean project "may have unsaved changes". The three arms:
 *
 *   true  → "has unsaved changes", with Save offered
 *   false → a clean stop: NO unsaved-work claim, and NO Save button for work that does not exist
 *   null  → "may have unsaved changes" (R62 — the platform says when it could not check)
 *
 * ═══ TWO THINGS THE COPY MUST NOT DO, BOTH FROM LIVE OBSERVATION ═══
 *
 * ISSUE #161, FRAMING. Lead with the app they are STARTING, not the one they are leaving. The
 * observed modal opened with the name of the app the citizen was not working on, and the question
 * they were asking was about the other one.
 *
 * ISSUE #161, AMBIGUITY. "Switch without saving" beside a build was found genuinely ambiguous by a
 * non-technical audience: it does not say whether the unsaved work being dropped belongs to the app
 * they are starting or the one being stopped. The button and the sentence above it NAME the project
 * whose changes are lost. This audience could not reason it out from context, and the issue records
 * that they did not.
 *
 * ═══ R95 — WHAT IS TRUE, SAID PLAINLY ═══
 *
 * The other project is STOPPED, not moved. Its saved work is untouched, and starting it again later
 * rebuilds it from its own saved state. NOTHING TRAVELS BETWEEN PROJECTS, and no sentence here may
 * imply that anything does — including softeners like "move your work over" or "bring it with you".
 */
function copyFor(
  blocked: ReclaimBlocked,
  startingProjectName: string | null,
): {
  title: string
  body: string
  /** `null` when there is nothing to save — the clean arm offers no Save button at all. */
  save: string | null
  discard: string
} {
  // The app being started, in the first line. Falls back to the plain phrasing when the caller
  // could not name it, rather than rendering an empty pair of quotes.
  const starting = startingProjectName ? `“${startingProjectName}”` : 'this app'
  const incumbent = `“${blocked.projectName}”`
  const oneAtATime = 'You can work on one app at a time.'

  if (blocked.building) {
    return {
      title: `Start ${starting}?`,
      body: `${oneAtATime} ${incumbent} is still being built, so it has to stop first. Stopping keeps everything the assistant has written into ${incumbent} so far — it stays where it is, and you can pick it up again later.`,
      save: `Save ${incumbent} and stop it`,
      discard: `Stop ${incumbent} without saving`,
    }
  }

  if (blocked.dirty === false) {
    // A CLEAN STOP. No unsaved-work claim, and no Save button — offering to save work that does
    // not exist is how a person learns the dialog does not know what it is talking about.
    return {
      title: `Start ${starting}?`,
      body: `${oneAtATime} ${incumbent} will stop so ${starting} can run. Everything saved in ${incumbent} stays exactly as it is, and starting it again later brings it back.`,
      save: null,
      discard: `Stop ${incumbent}`,
    }
  }

  const unsaved = blocked.dirty === true ? 'has changes that are not saved yet' : 'may have changes that are not saved yet'
  return {
    title: `Start ${starting}?`,
    body: `${oneAtATime} ${incumbent} will stop so ${starting} can run, and it ${unsaved}. Save it first and it comes back exactly as you left it; stop without saving and those changes go.`,
    save: `Save ${incumbent} and stop it`,
    discard: `Stop ${incumbent} without saving`,
  }
}

export default function ReclaimWorkspaceDialog({
  blocked,
  startingProjectName = null,
  onSaveAndSwitch,
  onSwitchAnyway,
  onCancel,
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
              {busy === 'save' ? <Loader2 size={15} className="animate-spin" /> : null} {copy.save}
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
            {busy === 'discard' ? <Loader2 size={15} className="animate-spin" /> : null}{' '}
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
