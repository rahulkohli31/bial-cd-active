import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { AlertCircle, CircleHelp, RefreshCw } from 'lucide-react'
import { errorText } from '../../utils/apiError'
import {
  MAX_THRESHOLD,
  addClassificationClass,
  editClassificationClass,
  fetchClassificationConfig,
  scoredTotal,
  shareOf,
  updateClassificationPolicy,
} from '../../utils/adminClassificationApi'
import type { ClassFields, ClassificationClass, ClassificationConfig } from '../../utils/adminClassificationApi'
import { dayMonth } from '../../utils/projectDates'
import { Badge } from '../ui/badge'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Label } from '../ui/label'
import { Progress } from '../ui/progress'
import { Switch } from '../ui/switch'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../ui/tooltip'
import { BusyGlyph } from '../ui/Waiting'
import AdminDataTable from './AdminDataTable'
import ClassEditDialog, { WHOLE_NUMBER_ERROR, wholeNumber } from './ClassEditDialog'

const SEARCHABLE = new Set(['title'])
const OWNERS = 'owners'

const dash = <span className="text-neutral">—</span>

function lastChanged(c: ClassificationClass): string {
  return [dayMonth(c.updatedAt), c.updatedByName].filter(Boolean).join(' · ')
}

function classColumns({
  total,
  pending,
  onToggle,
  onEdit,
}: {
  total: number
  pending: ReadonlySet<string>
  onToggle: (c: ClassificationClass) => void
  onEdit: (c: ClassificationClass) => void
}): ColumnDef<ClassificationClass>[] {
  const share = (c: ClassificationClass) =>
    c.kind === 'scored' && c.active ? shareOf(c.weight ?? 0, total) : null
  return [
    {
      id: 'title',
      accessorFn: (c) => c.title,
      header: 'Class',
      cell: ({ row: { original: c } }) => (
        <div className="flex items-center gap-1">
          <span className="text-[13.5px] font-semibold text-tertiary">{c.title}</span>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                aria-label={`What counts as ${c.title}`}
                className="flex h-[22px] w-[22px] items-center justify-center rounded-full text-slate-400 transition hover:text-neutral focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
              >
                <CircleHelp size={15} />
              </button>
            </TooltipTrigger>
            <TooltipContent
              side="bottom"
              align="start"
              className="w-[360px] rounded-lg bg-tertiary px-3 py-2.5 text-xs leading-[1.55] text-white shadow-xl"
            >
              {c.description}
            </TooltipContent>
          </Tooltip>
        </div>
      ),
    },
    {
      id: 'kind',
      accessorFn: (c) => c.kind,
      header: 'Kind',
      meta: { className: 'w-[152px]' },
      cell: ({ row: { original: c } }) =>
        c.kind === 'hard_block' ? (
          <Badge variant="outline" className="border-status-red-fg/20 bg-red-50 py-px text-[10.5px] uppercase leading-[14px] text-status-red-fg">
            Hard block
          </Badge>
        ) : (
          <Badge variant="outline" className="border-transparent bg-slate-100 py-px text-[10.5px] uppercase leading-[14px] text-slate-600">
            Scored
          </Badge>
        ),
    },
    {
      id: 'weight',
      accessorFn: (c) => c.weight ?? -1,
      header: 'Weight',
      meta: { className: 'w-[112px] text-[12.5px] text-primary-900' },
      cell: ({ row: { original: c } }) => (c.weight === null ? dash : c.weight),
    },
    {
      id: 'share',
      accessorFn: (c) => share(c) ?? -1,
      header: 'Share of score',
      meta: { className: 'w-[182px] text-[12.5px] text-primary-900' },
      cell: ({ row: { original: c } }) => {
        const value = share(c)
        if (value === null) return dash
        return (
          <div className="flex items-center gap-2">
            <Progress value={value} aria-label={`${c.title}: share of the score`} className="h-1.5 w-[70px] bg-[#EEF2F6]" />
            <span className="tabular-nums">{value}%</span>
          </div>
        )
      },
    },
    {
      id: 'active',
      header: 'Active',
      enableSorting: false,
      meta: { className: 'w-[112px]' },
      cell: ({ row: { original: c } }) => (
        <Switch
          size="lg"
          className="flex"
          checked={c.active}
          aria-label={`Active: ${c.title}`}
          aria-disabled={pending.has(c.key)}
          onCheckedChange={() => onToggle(c)}
        />
      ),
    },
    {
      id: 'updatedAt',
      accessorFn: (c) => c.updatedAt,
      header: 'Last changed',
      meta: { className: 'w-[172px] text-[12.5px] text-neutral' },
      cell: ({ row: { original: c } }) => lastChanged(c),
    },
    {
      id: 'edit',
      header: '',
      enableSorting: false,
      meta: { className: 'w-[82px]' },
      cell: ({ row: { original: c } }) => (
        <button
          type="button"
          onClick={() => onEdit(c)}
          aria-label={`Edit ${c.title}`}
          className="text-[12.5px] font-semibold text-primary transition hover:text-primary-dark"
        >
          Edit
        </button>
      ),
    },
  ]
}

function ThresholdCard({ threshold, onSave }: { threshold: number; onSave: (value: number) => Promise<void> }) {
  const id = useId()
  const [draft, setDraft] = useState(String(threshold))
  const [invalid, setInvalid] = useState(false)

  const commit = () => {
    const value = wholeNumber(draft, MAX_THRESHOLD)
    setInvalid(value === null)
    if (value === null || value === threshold) return
    // The panel has already reported the failure; the field goes back to the saved value.
    onSave(value).catch(() => setDraft(String(threshold)))
  }

  return (
    <div className="rounded-xl border border-bial-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Label htmlFor={`${id}-threshold`} className="text-sm font-bold leading-normal text-tertiary">
          Publish without review up to a score of
        </Label>
        <Input
          id={`${id}-threshold`}
          inputMode="numeric"
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value)
            setInvalid(false)
          }}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') e.currentTarget.blur()
          }}
          aria-invalid={invalid}
          aria-describedby={`${id}-help`}
          className={`h-[34px] w-[60px] px-2.5 text-sm font-bold text-primary-900 shadow-none md:text-sm ${invalid ? 'border-red-400' : 'border-bial-border'}`}
        />
        <span className="text-sm font-bold text-neutral">/ 100</span>
      </div>
      <p id={`${id}-help`} className={`mt-2 text-xs ${invalid ? 'text-red-600' : 'text-neutral'}`}>
        {invalid ? WHOLE_NUMBER_ERROR : 'Higher scores go to admin review. Hard blocks always do.'}
      </p>
    </div>
  )
}

export interface ClassificationPanelProps {
  onToast: (msg: string, severity?: 'ok' | 'problem') => void
}

/** The Deployment Classification tab: the threshold, whether owners can change the agent's answers, and the classes. */
export default function ClassificationPanel({ onToast }: ClassificationPanelProps) {
  const ownersId = useId()
  const [config, setConfig] = useState<ClassificationConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [dialog, setDialog] = useState<{ editing: ClassificationClass | null } | null>(null)
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set())
  const queue = useRef<Promise<unknown>>(Promise.resolve())

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      setConfig(await fetchClassificationConfig())
    } catch (e) {
      setLoadError(errorText(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // Writes go one at a time, so every answer applied already includes each write sent before it.
  const save = useCallback(async (write: () => Promise<ClassificationConfig>) => {
    const answer = queue.current.then(write)
    queue.current = answer.catch(() => undefined)
    setConfig(await answer)
  }, [])

  const saveLocked = useCallback(
    async (lock: string, write: () => Promise<ClassificationConfig>, done: string) => {
      setPending((p) => new Set(p).add(lock))
      try {
        await save(write)
        onToast(done)
      } catch (e) {
        onToast(errorText(e), 'problem')
      } finally {
        setPending((p) => new Set([...p].filter((k) => k !== lock)))
      }
    },
    [onToast, save],
  )

  const saveThreshold = async (threshold: number) => {
    try {
      await save(() => updateClassificationPolicy({ threshold }))
      onToast(`Apps now publish without review up to a score of ${threshold}.`)
    } catch (e) {
      onToast(errorText(e), 'problem')
      throw e
    }
  }

  const submitClass = async (fields: ClassFields) => {
    const editing = dialog?.editing ?? null
    await save(() => (editing ? editClassificationClass(editing.key, fields) : addClassificationClass(fields)))
    setDialog(null)
    onToast(editing ? `Saved “${fields.title}”.` : `Added “${fields.title}”.`)
  }

  const classes = config?.classes
  const total = classes ? scoredTotal(classes) : 0
  const columns = useMemo(
    () =>
      classColumns({
        total,
        pending,
        onEdit: (c) => setDialog({ editing: c }),
        onToggle: (c) => {
          if (pending.has(c.key)) return
          void saveLocked(
            c.key,
            () => editClassificationClass(c.key, { active: !c.active }),
            c.active ? `The agent no longer checks “${c.title}”.` : `The agent now checks “${c.title}”.`,
          )
        },
      }),
    [total, pending, saveLocked],
  )

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-sm text-neutral">
        <BusyGlyph size={16} /> Loading the classification settings…
      </div>
    )
  }

  if (loadError !== null || config === null) {
    return (
      <div className="py-16 text-center">
        <AlertCircle size={20} className="mx-auto mb-3 text-red-500" />
        <p className="text-sm font-semibold text-tertiary">Couldn’t load the classification settings</p>
        <p className="mt-1 text-xs text-neutral">{loadError}</p>
        <button
          onClick={() => void load()}
          className="mt-4 inline-flex items-center gap-1.5 rounded-xl border border-bial-border px-4 py-2 text-sm font-medium text-tertiary transition hover:bg-bial-bg"
        >
          <RefreshCw size={14} /> Retry
        </button>
      </div>
    )
  }

  const { threshold, ownersCanChangeAnswers } = config.policy
  const count = config.classes.length

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex flex-col gap-5">
        <div className="grid gap-4 md:grid-cols-2">
          <ThresholdCard key={threshold} threshold={threshold} onSave={saveThreshold} />
          <div className="rounded-xl border border-bial-border p-4">
            <div className="flex items-center gap-3">
              <Label htmlFor={ownersId} className="flex-grow text-sm font-bold leading-normal text-tertiary">
                Owners can change the agent&apos;s answers
              </Label>
              <Switch
                id={ownersId}
                size="lg"
                checked={ownersCanChangeAnswers}
                aria-disabled={pending.has(OWNERS)}
                onCheckedChange={(next) => {
                  if (pending.has(OWNERS)) return
                  void saveLocked(
                    OWNERS,
                    () => updateClassificationPolicy({ ownersCanChangeAnswers: next }),
                    next ? "Owners can now change the agent's answers." : "Owners can no longer change the agent's answers.",
                  )
                }}
              />
            </div>
            <p data-testid="owners-state" className="mt-2 text-xs text-neutral">
              {ownersCanChangeAnswers ? (
                <>
                  <b className="text-emerald-700">On</b> · the owner&apos;s answers set the score
                </>
              ) : (
                <>
                  <b className="text-tertiary">Off</b> · the agent&apos;s answers set the score
                </>
              )}
            </p>
          </div>
        </div>

        <AdminDataTable<ClassificationClass>
          columns={columns}
          rows={config.classes}
          getRowId={(c) => c.key}
          searchable={SEARCHABLE}
          searchLabel="Search classes"
          searchPlaceholder="Search classes…"
          emptyMessage="No classes yet."
          toolbarStart={() => (
            <h2 className="text-sm font-bold text-tertiary">Classes the deployment classification agent checks</h2>
          )}
          action={
            <Button
              type="button"
              onClick={() => setDialog({ editing: null })}
              className="h-[34px] rounded-lg px-3.5 text-[13px] font-semibold shadow-none"
            >
              Add class
            </Button>
          }
          summary={`${count} ${count === 1 ? 'class' : 'classes'} · total weight ${total} · changes apply to apps sent for publishing after you save`}
        />
      </div>

      {dialog && (
        <ClassEditDialog
          editing={dialog.editing}
          classes={config.classes}
          onClose={() => setDialog(null)}
          onSubmit={submitClass}
        />
      )}
    </TooltipProvider>
  )
}
