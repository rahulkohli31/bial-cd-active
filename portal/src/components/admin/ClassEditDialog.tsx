import { useId, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { ApiError, errorText } from '../../utils/apiError'
import {
  MAX_DESCRIPTION,
  MAX_TITLE,
  MAX_WEIGHT,
  scoredTotal,
  shareOf,
} from '../../utils/adminClassificationApi'
import type { ClassFields, ClassKind, ClassificationClass } from '../../utils/adminClassificationApi'
import { cn } from '../../lib/utils'
import { Alert, AlertDescription } from '../ui/alert'
import { Button } from '../ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../ui/dialog'
import { Input } from '../ui/input'
import { Label } from '../ui/label'
import { Switch } from '../ui/switch'
import { Textarea } from '../ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import { BusyGlyph } from '../ui/Waiting'

export const WHOLE_NUMBER_ERROR = `Enter a whole number from 0 to ${MAX_WEIGHT}.`

/** A whole number from 0 to `max`, or `null`. */
export function wholeNumber(draft: string, max: number): number | null {
  const text = draft.trim()
  if (!/^\d+$/.test(text)) return null
  const value = Number(text)
  return value <= max ? value : null
}

const OTHER_COLOURS = ['#0D7377', '#2F868A', '#529C9F', '#79B4B6', '#A6CFD0']
const COUNT_WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine']

const fieldLabel = 'mb-1.5 block text-[12.5px] font-bold leading-normal text-tertiary'
const helper = 'mt-1.5 text-[11.5px] text-neutral'
const fieldError = 'mt-1.5 text-[11.5px] text-red-600'
const field = 'border-bial-border text-primary-900 shadow-none md:text-sm'

function othersSentence(before: number, after: number, others: ClassificationClass[]): string {
  const lead = `Total weight ${after === before ? 'stays' : 'becomes'} ${after}`
  if (after === 0) return `${lead}, so no class adds to the score.`
  if (others.length === 0) return `${lead}. This is the only scored class.`
  if (after === before) return `${lead}, so the other classes keep their shares.`
  const moves = others.map((c) => [shareOf(c.weight ?? 0, before), shareOf(c.weight ?? 0, after)])
  const [from, to] = moves[0]
  const uniform = from !== null && moves.every(([a, b]) => a === from && b === to)
  if (!uniform || to === null) return `${lead}. The bar shows each class’s new share.`
  if (from === to) return `${lead}, so the other classes keep their shares.`
  const direction = from > to ? 'drop' : 'rise'
  return others.length === 1
    ? `${lead}, so the other class ${direction}s from ${from} to ${to}.`
    : `${lead}, so the other ${COUNT_WORDS[others.length] ?? others.length} classes ${direction} from ${from} to ${to} each.`
}

function SharePreview({
  title,
  weight,
  active,
  editingKey,
  classes,
}: {
  title: string
  weight: number
  active: boolean
  editingKey: string | null
  classes: ClassificationClass[]
}) {
  const others = classes.filter((c) => c.key !== editingKey && c.active && c.kind === 'scored')
  const before = scoredTotal(classes)
  const after = scoredTotal(others) + (active ? weight : 0)
  const segments = [
    ...others.map((c, i) => ({ key: c.key, title: c.title, weight: c.weight ?? 0, colour: OTHER_COLOURS[i % OTHER_COLOURS.length], own: false })),
    ...(active ? [{ key: editingKey ?? 'new', title: title.trim() || 'This class', weight, colour: '#B45309', own: true }] : []),
  ]

  return (
    <div className="flex-grow rounded-[10px] border border-bial-border bg-surface-muted px-3.5 py-3">
      <p className="text-[13px] font-bold text-tertiary">
        A Yes here is worth {active ? (shareOf(weight, after) ?? 0) : 0} of 100
      </p>
      <p className="mt-0.5 text-[11.5px] text-neutral">{othersSentence(before, after, others)}</p>
      {after > 0 && (
        <>
          <div className="mt-2.5 flex overflow-hidden rounded-full" aria-hidden>
            {segments.map((s) => (
              <div
                key={s.key}
                data-testid="share-segment"
                className="h-3"
                style={{ width: `${(100 * s.weight) / after}%`, background: s.colour }}
              />
            ))}
          </div>
          <ul data-testid="share-legend" className="mt-2 flex flex-wrap gap-x-3.5 gap-y-1.5 text-[11.5px] text-primary-900">
            {segments.map((s) => (
              <li key={s.key} className="flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-[2px]" style={{ background: s.colour }} />
                <span className={cn(s.own && 'font-bold text-amber-800')}>
                  {s.title} {shareOf(s.weight, after)}%
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

export interface ClassEditDialogProps {
  /** The class being edited, or `null` to add one. */
  editing: ClassificationClass | null
  /** The live configuration's classes, for the share preview. */
  classes: ClassificationClass[]
  onClose: () => void
  /** Saves the class; a rejection keeps the dialog open and shows why. */
  onSubmit: (fields: ClassFields) => Promise<void>
}

/** Add or edit one class: title, the agent's instruction, kind, weight with its live share, and Active. */
export default function ClassEditDialog({ editing, classes, onClose, onSubmit }: ClassEditDialogProps) {
  const ids = useId()
  const titleRef = useRef<HTMLInputElement>(null)
  const [title, setTitle] = useState(editing?.title ?? '')
  const [description, setDescription] = useState(editing?.description ?? '')
  const [kind, setKind] = useState<ClassKind>(editing?.kind ?? 'scored')
  const [weightDraft, setWeightDraft] = useState(editing?.weight == null ? '' : String(editing.weight))
  const [active, setActive] = useState(editing?.active ?? true)
  const [duplicate, setDuplicate] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const cleanTitle = title.trim()
  const cleanDescription = description.trim()
  const weight = wholeNumber(weightDraft, MAX_WEIGHT)
  const titleError =
    duplicate ?? (cleanTitle.length > MAX_TITLE ? `Keep the title to ${MAX_TITLE} characters or fewer.` : null)
  const descriptionError =
    cleanDescription.length > MAX_DESCRIPTION
      ? `Keep the description to ${MAX_DESCRIPTION.toLocaleString('en-US')} characters or fewer.`
      : null
  const weightMissing = kind === 'scored' && weight === null
  const valid = cleanTitle !== '' && cleanDescription !== '' && !titleError && !descriptionError && !weightMissing

  const submit = async () => {
    if (!valid || busy) return
    setBusy(true)
    setFailure(null)
    try {
      await onSubmit({
        title: cleanTitle,
        description: cleanDescription,
        kind,
        weight: kind === 'scored' ? weight : null,
        active,
      })
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'duplicate_title') setDuplicate(caught.message)
      else setFailure(errorText(caught))
      setBusy(false)
    }
  }

  const titleErrorId = `${ids}-title-error`
  const descriptionHelpId = `${ids}-description-help`
  const weightHelpId = `${ids}-weight-help`
  const kindLabelId = `${ids}-kind`

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !busy) onClose()
      }}
    >
      <DialogContent
        hideClose
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        className="max-w-[640px] gap-0 rounded-2xl border-0 bg-white p-0 font-manrope shadow-2xl sm:rounded-2xl"
        onOpenAutoFocus={(event) => {
          event.preventDefault()
          titleRef.current?.focus()
        }}
      >
        <div className="flex items-start justify-between gap-3 border-b border-bial-border px-6 pb-4 pt-5">
          <div>
            <DialogTitle className="text-[17px] font-bold leading-normal tracking-normal text-tertiary">
              {editing ? 'Edit class' : 'Add class'}
            </DialogTitle>
            <DialogDescription className="mt-1 text-[12.5px] leading-normal text-neutral">
              Applies to apps sent for publishing after you save. Apps already decided keep their result.
            </DialogDescription>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
            className="flex h-[30px] w-[30px] flex-shrink-0 items-center justify-center rounded-lg text-neutral transition hover:bg-bial-bg hover:text-tertiary disabled:opacity-50"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex flex-col gap-4 px-6 pb-2 pt-4">
          <div>
            <Label htmlFor={`${ids}-title`} className={fieldLabel}>
              Title
            </Label>
            <Input
              id={`${ids}-title`}
              ref={titleRef}
              value={title}
              onChange={(e) => {
                setTitle(e.target.value)
                setDuplicate(null)
              }}
              aria-invalid={titleError !== null}
              aria-describedby={titleError ? titleErrorId : undefined}
              className={cn('h-9 px-3 text-sm font-medium', field, titleError && 'border-red-400')}
            />
            {titleError && (
              <p id={titleErrorId} className={fieldError}>
                {titleError}
              </p>
            )}
          </div>

          <div>
            <Label htmlFor={`${ids}-description`} className={fieldLabel}>
              Description
            </Label>
            <Textarea
              id={`${ids}-description`}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              aria-invalid={descriptionError !== null}
              aria-describedby={descriptionHelpId}
              className={cn(
                'h-[92px] resize-none px-3 py-2.5 text-[13.5px] font-medium leading-[1.6] md:text-[13.5px]',
                field,
                descriptionError && 'border-red-400',
              )}
            />
            <p id={descriptionHelpId} className={descriptionError ? fieldError : helper}>
              {descriptionError ?? 'The deployment classification agent follows this when it checks an app.'}
            </p>
          </div>

          <div>
            <div id={kindLabelId} className={fieldLabel}>
              Kind
            </div>
            <ToggleGroup
              type="single"
              value={kind}
              aria-labelledby={kindLabelId}
              onValueChange={(next) => {
                if (next === 'scored' || next === 'hard_block') setKind(next)
              }}
              className="w-fit justify-start gap-0 overflow-hidden rounded-lg border border-bial-border"
            >
              {(
                [
                  ['scored', 'Scored'],
                  ['hard_block', 'Hard block'],
                ] as const
              ).map(([value, label]) => (
                <ToggleGroupItem
                  key={value}
                  value={value}
                  className="h-[34px] rounded-none bg-white px-4 text-[12.5px] font-semibold text-neutral hover:text-tertiary data-[state=on]:bg-primary data-[state=on]:text-white data-[state=on]:shadow-none"
                >
                  {label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
            <p className={cn(helper, 'leading-normal')}>
              Scored: a Yes adds to the score. Hard block: a Yes always sends the app to an administrator, and the owner
              can&apos;t change it.
            </p>
          </div>

          {kind === 'scored' && (
            <div className="flex items-start gap-5">
              <div>
                <Label htmlFor={`${ids}-weight`} className={fieldLabel}>
                  Weight
                </Label>
                <Input
                  id={`${ids}-weight`}
                  inputMode="numeric"
                  value={weightDraft}
                  onChange={(e) => setWeightDraft(e.target.value)}
                  aria-invalid={weightMissing}
                  aria-describedby={weightHelpId}
                  className={cn('h-9 w-[88px] px-3 text-sm font-semibold', field, weightMissing && 'border-red-400')}
                />
                <p id={weightHelpId} className={cn(weightMissing ? fieldError : helper, 'max-w-[88px]')}>
                  {weightMissing ? WHOLE_NUMBER_ERROR : `0 to ${MAX_WEIGHT}`}
                </p>
              </div>
              {weight !== null && (
                <SharePreview
                  title={title}
                  weight={weight}
                  active={active}
                  editingKey={editing?.key ?? null}
                  classes={classes}
                />
              )}
            </div>
          )}

          <div className="flex items-center gap-2.5">
            <Switch size="lg" checked={active} onCheckedChange={setActive} aria-label="Active" />
            <span className="text-[13px] text-primary-900">
              {active ? (
                <>
                  <b>Active</b> · the agent checks this class
                </>
              ) : (
                <>
                  <b>Off</b> · the agent does not check this class
                </>
              )}
            </span>
          </div>

          {failure && (
            <Alert variant="destructive" className="rounded-xl px-3 py-2.5">
              <AlertDescription className="text-xs">{failure}</AlertDescription>
            </Alert>
          )}
        </div>

        <div className="mt-2.5 flex items-center justify-end gap-2.5 border-t border-bial-border px-6 py-3.5">
          <Button
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={busy}
            className="h-9 rounded-lg border-bial-border bg-white px-3.5 text-[13.5px] font-semibold text-primary-900 shadow-none"
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => void submit()}
            disabled={!valid || busy}
            className="h-9 rounded-lg px-[18px] text-[13.5px] font-semibold shadow-none"
          >
            {busy && <BusyGlyph size={14} />}
            {editing ? 'Save' : 'Add class'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
