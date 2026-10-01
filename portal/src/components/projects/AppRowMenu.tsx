import { useRef, useState } from 'react'
import { MoreHorizontal, ExternalLink, Copy, Settings } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '../ui/dropdown-menu'
import { getDeployment } from '../../utils/deployApi'
import { useCopyOutcome } from '../../hooks/useCopyOutcome'

/**
 * The `⋯` menu an application carries in the list and on its tile — ONE component, so the two
 * views cannot come to offer different actions on the same application.
 *
 * IT IS A SIBLING OF THE NAME BUTTON, NEVER A DESCENDANT. The row's open affordance is a real
 * `<button>` whose stretched `::after` covers the whole row; a menu trigger inside it would be a
 * button inside a button. The caller lifts this above that overlay with `z-10`, exactly as the
 * bare delete control it replaces was lifted.
 *
 * ALWAYS VISIBLE, NEVER HOVER-ONLY. The tile's delete control used to appear on hover, which is
 * an affordance a keyboard has no route to and a touch screen never triggers at all.
 *
 * The menu offers the way into the live application and its address; everything operational
 * (Restart, Take down, Delete) stays in Settings, on the tab that owns what it changes. The
 * application's name is the way into the workspace. Live means the row's `isServing` and nothing
 * else. The address is read each time the menu opens and never kept between opens, so both
 * address entries stay disabled until that read answers with a usable one.
 */

export interface AppRowMenuProps {
  /** Named in the trigger's accessible label, so a page of menus is not a page of "More". */
  appName: string
  projectId: string
  isServing: boolean
  onSettings: () => void
  /** Distinguishes the row's menu from the tile's in the DOM — one testid each. */
  where: 'row' | 'tile'
}

const ITEM = 'gap-2 rounded-sm px-2 py-1.5 text-sm text-primary-900 focus:bg-surface-muted'

/** The address only if it is absolute http(s); anything else is never opened or copied. */
function usableAddress(value: string | null): string | null {
  if (value === null) return null
  try {
    const { protocol } = new URL(value)
    return protocol === 'https:' || protocol === 'http:' ? value : null
  } catch {
    return null
  }
}

/** Divs, not spans, so the accessible name keeps a space between the label and its note. */
function ItemText({ label, note }: { label: string; note?: string }) {
  return (
    <div className="flex flex-col">
      <div>{label}</div>
      {note !== undefined && <div className="text-[11px] text-neutral">{note}</div>}
    </div>
  )
}

export default function AppRowMenu({
  appName,
  projectId,
  isServing,
  onSettings,
  where,
}: AppRowMenuProps) {
  const [open, setOpen] = useState(false)
  const [address, setAddress] = useState<string | null>(null)
  const { outcome, copy, reset } = useCopyOutcome()
  // Bumped on every open and close, so an answer from an earlier open is dropped.
  const openToken = useRef(0)

  const onOpenChange = (next: boolean): void => {
    setOpen(next)
    const token = ++openToken.current
    setAddress(null)
    reset()
    if (!next || !isServing) return
    void getDeployment(projectId)
      .then((view) => usableAddress(view.liveUrl))
      // A failed read is shown as "Address unavailable", and reopening the menu retries.
      .catch(() => null)
      .then((url) => {
        if (openToken.current === token) setAddress(url)
      })
  }

  const note = !isServing ? 'Not live yet' : address === null ? 'Address unavailable' : undefined

  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          data-testid={`app-menu-${where}`}
          aria-label={`More actions for ${appName}`}
          className="relative z-10 flex h-[26px] w-[26px] flex-shrink-0 items-center justify-center rounded-lg text-neutral transition hover:bg-surface-muted hover:text-primary-900 data-[state=open]:bg-surface-muted data-[state=open]:text-primary-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
        >
          <MoreHorizontal size={16} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="min-w-[220px] max-w-[280px] rounded-md border-bial-border bg-white p-1 shadow-lg"
      >
        {note !== undefined ? (
          <DropdownMenuItem disabled className={ITEM}>
            <ExternalLink size={15} />
            <ItemText label="Open Application" note={note} />
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem asChild className={ITEM}>
            <a href={address ?? undefined} target="_blank" rel="noopener noreferrer">
              <ExternalLink size={15} />
              Open Application
            </a>
          </DropdownMenuItem>
        )}
        {isServing && (
          <DropdownMenuItem
            disabled={address === null}
            className={ITEM}
            // Kept open so the outcome below is read where the press happened.
            onSelect={(event) => {
              event.preventDefault()
              if (address !== null) void copy(address)
            }}
          >
            <Copy size={15} />
            {address === null ? (
              <ItemText label="Copy Production URL" note="Address unavailable" />
            ) : (
              'Copy Production URL'
            )}
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator className="bg-bial-border" />
        <DropdownMenuItem onSelect={onSettings} className={ITEM}>
          <Settings size={15} />
          Settings…
        </DropdownMenuItem>
        {isServing && (
          <div
            role="status"
            data-testid={`app-menu-${where}-copy-outcome`}
            className="px-2 text-[11px] leading-relaxed"
          >
            {outcome === 'copied' && <p className="py-1 text-primary-900">Production URL copied</p>}
            {outcome === 'refused' && address !== null && (
              <p className="py-1 text-danger">
                Could not copy the address
                <span className="block break-all font-mono text-neutral">{address}</span>
              </p>
            )}
          </div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
