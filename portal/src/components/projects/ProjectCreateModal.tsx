/**
 * Create-a-project modal. Client-side guards that mirror the server's rules so the user
 * is corrected before a round-trip, not after a 422:
 *   - name is required and capped at 8 WORDS (#158 §14) — the server enforces the same
 *     rule with the same splitting, and 120 chars remains only as a paste backstop,
 *   - description is REQUIRED and WORD-bounded at 15-120 (#191) — the server enforces the
 *     same rule with the same splitting, and 2000 chars remains only as a paste backstop.
 * The submit button stays disabled while any bound is unmet, AND the submit handler
 * re-checks, so a programmatic out-of-bounds value can never reach the network.
 *
 * When the server does reject, we surface the message the thrown `ApiError` carries
 * — which `readApiError` already pulled from whichever of the three envelopes the
 * backend chose — never a synthetic "Failed to create project (422)."
 *
 * TWO SCREENS, ONE FORM (#191 slice 4, R31/R35/R36). Submitting the form does not create
 * a project directly — it first asks the server whether something that does what the
 * typed description says already exists (`checkDuplicateProjects`). Zero matches (the
 * default, day-one case) is the fall-through: it creates immediately, exactly as before
 * this slice. One or more confident matches swap the SAME dialog's content to a second
 * screen instead of unmounting the form, so "Go back" returns to the name/description the
 * citizen already typed rather than to a blank one. "Create project anyway" stays
 * available beneath the matches at all times (R36) — this is a courtesy, never a gate.
 */
import { useState } from 'react'
import type { ClipboardEvent } from 'react'
import {
  countWords,
  MAX_PROJECT_NAME_WORDS,
  MIN_PROJECT_DESCRIPTION_WORDS,
  MAX_PROJECT_DESCRIPTION_WORDS,
} from '../../utils/words'
import { X, Info, ExternalLink } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import {
  createProject,
  checkDuplicateProjects,
  reportDuplicateCheckResolution,
  type Project,
} from '../../utils/projectApi'
import type { MarketplaceEntry } from '../../utils/marketplaceApi'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover'

// The CHARACTER bounds are now only paste backstops at the column widths — the limits a
// person is told about are WORD counts (#158 §14, #191), counted by the rule the server
// shares (`src/core/words.py` <-> `utils/words.ts`). `maxLength` keeps an unbounded paste
// out of the columns; the counters and the disabled button enforce the rules that matter.
const NAME_MAX = 120
const DESCRIPTION_MAX = 2000

// The worked example behind the info control (#191 R16) — the issue's own text, so the
// example a citizen sees matches the one referenced in the requirement itself.
const DESCRIPTION_EXAMPLE =
  'Ground staff log VIP movement requests for each terminal. A duty supervisor approves ' +
  "or rejects them, and the day's approved movements appear on a shared dashboard."

/** A silent, best-effort analytics call (#191 R39) — never lets a failed log line surface
 *  as an error banner over a flow that has nothing left for the citizen to retry. */
function reportResolution(resolution: 'opened_existing' | 'created_anyway'): void {
  reportDuplicateCheckResolution(resolution).catch(() => {
    /* R39 is telemetry, not a contract with the citizen — a dropped event is not a failure */
  })
}

/** One possible-duplicate match, in the same card shape `MarketplacePage`'s own
 *  `EntryCard` uses (name / description-or-fallback / "Built by X" / external "Open app"
 *  link) — the response is the identical `MarketplaceEntry` wire shape, so the reader sees
 *  the same card either way rather than learning two visual languages for one fact. Not
 *  imported from `MarketplacePage.tsx` because that card is a private, unexported function
 *  there — this is a deliberately small, local duplicate of its JSX rather than a new
 *  cross-page dependency for four lines of markup. */
function DuplicateEntryCard({ entry }: { entry: MarketplaceEntry }): React.JSX.Element {
  return (
    <div className="bg-white border border-bial-border rounded-xl p-4 flex flex-col gap-2">
      <div className="flex flex-col gap-0.5">
        <h4 className="text-sm font-bold text-tertiary">{entry.name}</h4>
        {entry.builderDisplayName && (
          <p className="text-[11px] text-neutral">Built by {entry.builderDisplayName}</p>
        )}
      </div>
      {entry.description ? (
        <p className="text-xs text-neutral leading-relaxed">{entry.description}</p>
      ) : (
        <p className="text-xs text-neutral/60 italic">No description yet.</p>
      )}
      <a
        href={entry.url}
        target="_blank"
        rel="noopener noreferrer"
        // Navigation happens regardless (no `preventDefault`) — the resolution report is a
        // side effect of the click, never a gate on opening the app it names.
        onClick={() => reportResolution('opened_existing')}
        className="inline-flex items-center gap-1.5 text-xs font-semibold text-primary hover:underline mt-1"
      >
        <ExternalLink size={12} />
        Open app
      </a>
    </div>
  )
}

export interface ProjectCreateModalProps {
  onClose: () => void
  onCreated: (project: Project) => void
}

export default function ProjectCreateModal({ onClose, onCreated }: ProjectCreateModalProps): React.JSX.Element {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Which of the two screens the dialog shows. The form's own state (`name`/`description`)
  // is untouched by this — swapping back to 'form' after 'duplicates' must restore exactly
  // what the citizen typed, not a fresh instance of this component.
  const [screen, setScreen] = useState<'form' | 'duplicates'>('form')
  const [duplicates, setDuplicates] = useState<MarketplaceEntry[]>([])
  // A paste over DESCRIPTION_MAX is silently truncated by the textarea's own native
  // `maxLength` before `onChange` ever sees it — computed in `onPasteDescription`, before
  // that truncation happens, from the clipboard text and the current selection.
  const [descriptionPasteTruncated, setDescriptionPasteTruncated] = useState(false)

  const onPasteDescription = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = e.clipboardData.getData('text')
    const target = e.currentTarget
    const resultingLength =
      target.value.length - (target.selectionEnd - target.selectionStart) + pasted.length
    setDescriptionPasteTruncated(resultingLength > DESCRIPTION_MAX)
  }

  const trimmedName = name.trim()
  const nameTooLong = name.length > NAME_MAX
  const nameWords = countWords(name)
  const tooManyWords = nameWords > MAX_PROJECT_NAME_WORDS

  const descriptionTooLong = description.length > DESCRIPTION_MAX
  const descriptionWords = countWords(description)
  const descriptionOutOfBounds =
    descriptionWords < MIN_PROJECT_DESCRIPTION_WORDS || descriptionWords > MAX_PROJECT_DESCRIPTION_WORDS
  const descriptionInvalid = descriptionTooLong || descriptionOutOfBounds

  const canSubmit =
    trimmedName.length > 0 && !nameTooLong && !tooManyWords && !descriptionInvalid && !busy

  const doCreate = async (): Promise<void> => {
    setBusy(true)
    setError(null)
    try {
      const project = await createProject({ name: trimmedName, description: description.trim() })
      onCreated(project)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
      setBusy(false)
    }
  }

  const submit = async (): Promise<void> => {
    // Belt-and-braces: the button is disabled when invalid, but a test (or a paste)
    // can still drive the handler — never let an out-of-bounds value hit the network.
    if (trimmedName.length === 0 || nameTooLong || tooManyWords || descriptionInvalid || busy) return
    setBusy(true)
    setError(null)
    let matches: MarketplaceEntry[]
    try {
      matches = await checkDuplicateProjects(description.trim())
    } catch {
      // R37: the duplicate check is a courtesy, never a gate. The server's own internal
      // search/embedding failures already degrade to zero matches without raising — this
      // mirrors that fail-open posture for a client-side failure of the check call itself
      // (a network blip, an unexpected error), rather than surfacing a distinct error
      // banner for a step the citizen never asked for and should not be blocked by.
      matches = []
    }
    if (matches.length > 0) {
      setDuplicates(matches)
      setScreen('duplicates')
      setBusy(false)
      return
    }
    await doCreate()
  }

  const createAnyway = (): void => {
    reportResolution('created_anyway')
    void doCreate()
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        // Escape, the overlay click and the close button all arrive here. `busy` holds it
        // open mid-request, which is what the hand-rolled overlay's guard used to do. This
        // closes the WHOLE modal regardless of which screen is showing — "Go back" is the
        // only control that moves between screens without leaving the dialog.
        if (!next && !busy) onClose()
      }}
    >
      {/* NOT shadcn's `bg-black/80`, and not the kit's 12px blur either. A flat
          scrim erases the page; a heavy blur costs you the row you were about to click. The
          panel earns attention from its own shadow and white, so the page behind it only
          needs softening. `-webkit-` stays for Safari: without it this degrades to a flat
          16% scrim, which is acceptable rather than broken. Overlay only — never the list
          behind it, because `backdrop-filter` is GPU work over everything underneath.

          Passed as an override on the VENDORED dialog rather than a hand-rolled
          `fixed inset-0`, so the panel also gets `role="dialog"`, `aria-modal`, a focus trap
          and Escape — none of which the hand-rolled shell had. */}
      <DialogContent
        hideClose
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        className="font-manrope bg-white rounded-2xl shadow-2xl w-full max-w-md p-6 gap-0 border-0"
      >
        <div className="flex items-start justify-between">
          <div>
            <DialogTitle className="text-base font-bold text-tertiary">
              {screen === 'duplicates' ? 'This might already exist' : 'New project'}
            </DialogTitle>
            <p className="text-sm text-neutral mt-0.5">
              {screen === 'duplicates'
                ? "These published apps sound similar to what you're describing."
                : 'A project owns one app, its description, and its chats.'}
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
            className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition disabled:opacity-50"
          >
            <X size={18} />
          </button>
        </div>

        {screen === 'duplicates' ? (
          <div className="mt-4">
            <div className="flex flex-col gap-3 max-h-72 overflow-y-auto">
              {duplicates.map((entry, i) => (
                <DuplicateEntryCard key={`${entry.url}-${i}`} entry={entry} />
              ))}
            </div>

            {error !== null && (
              <div role="alert" className="mt-3 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5">
                <p className="text-xs text-red-600">{error}</p>
              </div>
            )}

            <div className="flex gap-3 mt-5">
              <button
                type="button"
                disabled={busy}
                onClick={createAnyway}
                className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {busy ? <BusyGlyph size={15} /> : null} Create project anyway
              </button>
              <button
                type="button"
                onClick={() => setScreen('form')}
                disabled={busy}
                className="px-4 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50"
              >
                Go back
              </button>
            </div>
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault()
              void submit()
            }}
          >
            <label className="block mt-5">
              <span className="text-xs font-semibold text-tertiary">Name</span>
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={NAME_MAX}
                placeholder="e.g. VIP Movement Tracker"
                disabled={busy}
                className="mt-1.5 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary disabled:opacity-50"
              />
              <div className="flex items-baseline justify-between mt-1">
                {/* The expectation stated BEFORE the user trips it, not only after. */}
                <span className="text-[11px] text-neutral">Keep it short — about 6 to 8 words.</span>
                <span
                  className={`text-[11px] tabular-nums ${tooManyWords ? 'text-danger font-semibold' : 'text-neutral'}`}
                >
                  {nameWords}/{MAX_PROJECT_NAME_WORDS} words
                </span>
              </div>
            </label>

            <label className="block mt-2">
              <span className="flex items-center gap-1.5">
                <span className="text-xs font-semibold text-tertiary">What should this app do?</span>
                {/* Keyboard-focusable, toggle-ON-CLICK (not hover-only, #191 R16) — a real
                    button wrapped by Radix's Popover trigger, so Tab reaches it and
                    Enter/Space/click all open it the same way. */}
                <Popover>
                  <PopoverTrigger asChild>
                    <button
                      type="button"
                      aria-label="Show an example description"
                      className="text-neutral hover:text-primary transition"
                    >
                      <Info size={13} />
                    </button>
                  </PopoverTrigger>
                  <PopoverContent className="w-72 text-xs leading-relaxed text-neutral">
                    {DESCRIPTION_EXAMPLE}
                  </PopoverContent>
                </Popover>
              </span>
              <textarea
                value={description}
                onChange={(e) => {
                  setDescription(e.target.value)
                  setDescriptionPasteTruncated(false)
                }}
                onPaste={onPasteDescription}
                placeholder="Who uses it, and what do they do with it?"
                maxLength={DESCRIPTION_MAX}
                rows={4}
                aria-label="What should this app do?"
                disabled={busy}
                className="mt-1.5 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary resize-none disabled:opacity-50"
              />
              <div className="flex items-baseline justify-between mt-1">
                <span className="text-[11px] text-neutral">
                  Between {MIN_PROJECT_DESCRIPTION_WORDS} and {MAX_PROJECT_DESCRIPTION_WORDS} words.
                </span>
                <span
                  className={`text-[11px] tabular-nums ${descriptionOutOfBounds && description.length > 0 ? 'text-danger font-semibold' : 'text-neutral'}`}
                >
                  {descriptionWords}/{MAX_PROJECT_DESCRIPTION_WORDS} words
                </span>
              </div>
              {descriptionPasteTruncated && (
                <p role="status" className="text-[11px] text-danger mt-1">
                  Pasted text was cut to {DESCRIPTION_MAX} characters.
                </p>
              )}
              {/* THE WRITE-SURFACE NOTICE (#191 R17), mirroring the one already in
                  `ProjectDescriptionEditor` — stated here too because this is the OTHER
                  place the field is written from. */}
              <p className="text-[11px] text-neutral mt-1">
                Colleagues see this in the Marketplace, and it&apos;s how the platform checks
                whether something similar already exists.
              </p>
            </label>

            {error !== null && (
              <div role="alert" className="mt-3 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5">
                <p className="text-xs text-red-600">{error}</p>
              </div>
            )}

            <div className="flex gap-3 mt-5">
              <button
                type="submit"
                disabled={!canSubmit}
                className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {busy ? <BusyGlyph size={15} /> : null} Create project
              </button>
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="px-4 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50"
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
