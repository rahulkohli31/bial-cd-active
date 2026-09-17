/**
 * One application a colleague shared with the reader, as a list row and as a grid tile — the
 * same `AppListRow`/`AppTile` halves the owner's list uses, with a recipient's columns and a
 * recipient's one control.
 *
 * NO STATUS CHIP, DELIBERATELY. A shared application is not the recipient's production app, so
 * "Live" or "Taken offline" would be a claim the platform cannot honour on their behalf.
 *
 * NO MENU EITHER — the trailing slot holds Open and nothing else. Everything in the `⋯` menu is
 * an owner's action, and the extracted halves take their trailing content as a required prop
 * with no owner-aware branch, so a recipient cannot be handed one by omission.
 */
import { ArrowRight } from 'lucide-react'
import AppListRow from './AppListRow'
import AppTile from './AppTile'
import { dayMonth, listDate } from '../../utils/projectDates'
import { COLUMN } from '../../utils/listView'
import type { SharedProject } from '../../utils/sharingApi'

/** What the list shows for a colleague with no display name stored — the same neutral words the
 *  filter labels them with, so one person is not two names across the page. */
export const NAMELESS_COLLEAGUE = 'A colleague'

export function sharerName(displayName: string | null): string {
  return displayName ?? NAMELESS_COLLEAGUE
}

/** Up to two initials for the mark beside a sharer's name. Two colleagues can share both — this
 *  is decoration, never what tells them apart; the filter does that, and it matches on the id. */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  return parts.slice(0, 2).map((part) => part.charAt(0).toUpperCase()).join('') || '?'
}

function Sharer({ displayName, prefix }: { displayName: string | null; prefix?: string }): React.JSX.Element {
  const name = sharerName(displayName)
  return (
    <span className="flex items-center gap-2 min-w-0">
      <span
        aria-hidden
        className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-primary/10 text-[10px] font-bold text-primary-900"
      >
        {initials(name)}
      </span>
      <span className="truncate text-xs text-tertiary">
        {prefix === undefined ? name : `${prefix} ${name}`}
      </span>
    </span>
  )
}

/** The recipient's one action. A real control rather than a hover hint: a hover-only affordance
 *  has no keyboard route and no touch route at all. */
function OpenButton({ name, onOpen }: { name: string; onOpen: () => void }): React.JSX.Element {
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`Open ${name}`}
      className="relative z-10 flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg border border-bial-border bg-white px-3 text-xs font-semibold text-tertiary transition hover:border-primary/40 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
    >
      Open
      <ArrowRight size={13} />
    </button>
  )
}

export interface SharedAppProps {
  project: SharedProject
  onOpen: () => void
}

export function SharedAppRow({ project, onOpen }: SharedAppProps): React.JSX.Element {
  const name = project.projectName || 'Untitled application'
  return (
    <AppListRow
      testId="shared-app-row"
      name={name}
      description={project.projectDescription}
      onOpen={onOpen}
      columns={
        <>
          <div className="hidden sm:block w-[168px] flex-shrink-0">
            <Sharer displayName={project.sharedByDisplayName} />
          </div>
          {/* The same fixed-width, left-aligned, tabular treatment the owner's dates get: the
              three together are what make a column of dates a ruler down the page. */}
          <p className={`hidden sm:block ${COLUMN.date} text-xs text-neutral tabular-nums whitespace-nowrap`}>
            {listDate(project.sharedAt)}
          </p>
          <p className={`hidden sm:block ${COLUMN.date} text-xs text-neutral tabular-nums whitespace-nowrap`}>
            {listDate(project.projectUpdatedAt)}
          </p>
        </>
      }
      trailing={<OpenButton name={name} onOpen={onOpen} />}
    />
  )
}

export function SharedAppTile({ project, onOpen }: SharedAppProps): React.JSX.Element {
  const name = project.projectName || 'Untitled application'
  return (
    <AppTile
      testId="shared-app-tile"
      name={name}
      description={project.projectDescription}
      onOpen={onOpen}
      trailing={<OpenButton name={name} onOpen={onOpen} />}
      foot={
        <div className="flex flex-col gap-1.5">
          <Sharer displayName={project.sharedByDisplayName} prefix="Shared by" />
          {/* The tile has no room for column headings, so the two dates say which is which. */}
          <span className="text-[11px] text-neutral tabular-nums whitespace-nowrap">
            shared {dayMonth(project.sharedAt)} · updated {dayMonth(project.projectUpdatedAt)}
          </span>
        </div>
      }
    />
  )
}
