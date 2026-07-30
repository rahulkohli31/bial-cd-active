/**
 * ModeSwitcher (F5/U6) — the ONE compact in-composer Ask / Plan / Write switch, shared by
 * both sites and fully CONTROLLED. It owns no mode state: the trigger reflects `value` and
 * never moves optimistically. Each mount supplies its own `onSelect` so the two data
 * contracts stay intact — the chat mount routes it through the server-confirmed `switchMode`
 * flow, the project mount sets local draft state — while the pill + radio menu stay identical.
 *
 * ⌥P is a DOCUMENT-LEVEL shortcut that `preventDefault`s: on macOS Option+P is a dead key
 * that would type `π` into the focused textarea, so a global handler (not a trigger-scoped
 * one, which is dead from the composer) both swallows the character and opens the menu.
 *
 * Desktop-first. The pill keeps a ≥36px touch target and lives in the composer row, which
 * degrades gracefully (wraps) when space is tight. After a mode is chosen, focus returns to
 * the composer textarea (`composerRef`) so the citizen keeps typing.
 */
import * as React from 'react'
import { ChevronDown, Hammer, HelpCircle, MessageSquare, type LucideIcon } from 'lucide-react'

import { cn } from '@/lib/utils'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { type ConversationMode } from '../../utils/turnStreamApi'

export interface ModeSwitcherProps {
  /** The current mode — the SINGLE source of truth for the trigger (fully controlled). */
  value: ConversationMode
  /** Chosen mode. The chat mount routes this through the server; the project mount sets local draft. */
  onSelect: (mode: ConversationMode) => void
  /** Disabled while a turn streams (chat) — the switch is between-turns only. */
  disabled?: boolean
  /** Focus returns here after a mode is chosen so the citizen keeps typing. */
  composerRef?: React.RefObject<HTMLTextAreaElement | null>
}

const MODES: ReadonlyArray<{
  value: ConversationMode
  label: string
  description: string
  Icon: LucideIcon
}> = [
  { value: 'ask', label: 'Ask', description: 'Ask about your app — nothing changes.', Icon: HelpCircle },
  { value: 'plan', label: 'Plan', description: 'Plan it together — you confirm first.', Icon: MessageSquare },
  { value: 'write', label: 'Write', description: 'Build the changes into your app.', Icon: Hammer },
]

export function ModeSwitcher({ value, onSelect, disabled = false, composerRef }: ModeSwitcherProps) {
  const [open, setOpen] = React.useState(false)
  const active = MODES.find((m) => m.value === value) ?? MODES[1] // Plan is the sticky default

  // ⌥P: a document-level shortcut. `event.code` is layout-independent, so it reads 'KeyP'
  // even as macOS composes the dead-key character into 'π'; preventDefault swallows that
  // character AND we open the menu, wherever focus happens to be.
  React.useEffect(() => {
    if (disabled) return undefined
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.altKey && !event.metaKey && !event.ctrlKey && event.code === 'KeyP') {
        event.preventDefault()
        setOpen((prev) => !prev)
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [disabled])

  const handleValueChange = (next: string) => {
    if (next === 'ask' || next === 'plan' || next === 'write') onSelect(next)
  }

  const ActiveIcon = active.Icon

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          title="Switch mode (⌥P)"
          aria-keyshortcuts="Alt+P"
          aria-label={`Mode: ${active.label}. Switch mode (Option+P).`}
          className={cn(
            'inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-bial-border bg-white px-2.5 py-2',
            'text-xs font-worksans font-medium text-tertiary transition',
            'hover:border-primary hover:text-primary',
            'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary/30',
            'disabled:pointer-events-none disabled:opacity-40',
            'data-[state=open]:border-primary data-[state=open]:text-primary'
          )}
        >
          <ActiveIcon size={13} />
          <span>{active.label}</span>
          <ChevronDown size={12} className="opacity-60 transition-transform data-[state=open]:rotate-180" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-72"
        onCloseAutoFocus={(event) => {
          // Return focus to the composer so the citizen keeps typing (not back to the pill).
          if (composerRef?.current) {
            event.preventDefault()
            composerRef.current.focus()
          }
        }}
      >
        <DropdownMenuLabel className="flex items-center justify-between gap-4 font-worksans text-xs font-semibold text-neutral">
          <span>Mode</span>
          <kbd className="rounded border border-bial-border bg-surface-muted px-1.5 py-0.5 font-sans text-[10px] font-medium text-neutral">
            ⌥P
          </kbd>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup value={value} onValueChange={handleValueChange}>
          {MODES.map(({ value: v, label, description, Icon }) => (
            <DropdownMenuRadioItem key={v} value={v} className="flex-col items-start gap-0.5 py-2">
              <span className="flex items-center gap-1.5 text-sm font-semibold text-tertiary">
                <Icon size={13} />
                {label}
              </span>
              <span className="text-xs text-neutral">{description}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
