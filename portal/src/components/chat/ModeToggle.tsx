/**
 * ModeToggle (U13/R6) — the Ask / Plan / Write switch. The MODE IS SERVER-OWNED sticky
 * state: this control reflects `mode` (from the conversation header) and requests a
 * switch through the atomic endpoint; it never assumes the switch landed — the parent
 * refreshes from the server's answer. Disabled while a turn streams (the switch is only
 * legal between turns; the server enforces it, this just doesn't offer the dead click).
 */

import { useState } from 'react'

import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import { switchMode, type ConversationMode } from '../../utils/turnStreamApi'

export interface ModeToggleProps {
  conversationId: string
  mode: ConversationMode
  /** True while a turn streams — switching is between-turns only. */
  disabled?: boolean
  /** Called with the server-confirmed mode after a successful switch. */
  onSwitched: (mode: ConversationMode) => void
  /** Called when the server refuses (e.g. mid-turn 409) — the toggle already reverted. */
  onRefused?: (message: string) => void
}

const MODES: Array<{ value: ConversationMode; label: string }> = [
  { value: 'ask', label: 'Ask' },
  { value: 'plan', label: 'Plan' },
  { value: 'write', label: 'Write' },
]

export function ModeToggle({
  conversationId,
  mode,
  disabled = false,
  onSwitched,
  onRefused,
}: ModeToggleProps) {
  const [busy, setBusy] = useState(false)

  const handleChange = (value: string) => {
    // Radix fires with '' when the active item is clicked again — a no-op, not a switch.
    if (value === '' || value === mode) return
    if (value !== 'ask' && value !== 'plan' && value !== 'write') return
    setBusy(true)
    switchMode(conversationId, value)
      .then((confirmed) => onSwitched(confirmed))
      .catch((err: unknown) => {
        const message =
          err instanceof Error ? err.message : 'The mode could not be switched. Try again.'
        onRefused?.(message)
      })
      .finally(() => setBusy(false))
  }

  return (
    <ToggleGroup
      type="single"
      value={mode}
      onValueChange={handleChange}
      disabled={disabled || busy}
      aria-label="Chat mode"
      className="mode-toggle"
    >
      {MODES.map(({ value, label }) => (
        <ToggleGroupItem key={value} value={value} aria-label={`${label} mode`} size="sm">
          {label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}
