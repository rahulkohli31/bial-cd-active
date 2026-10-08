/**
 * ONE ROW INSIDE AN ACTIVITY GROUP. The row vocabulary is VERB + TARGET + STATE, and the row can
 * read exactly two things: the server's friendly label and the state. Nothing else reaches it —
 * `convertMessage` never copies `detail`, `args` or `result` onto the part, so that wall is
 * upstream of this file and this component could not leak a file path if it tried; a promise at
 * the draw site is only as good as the next person editing the draw site.
 *
 * Every guarantee `ToolActivityLine` carries comes through unchanged, because the row IS
 * `ToolActivityLine` — the reduced-motion gate, the sr-only "failed" text (failure by shape and
 * text, never colour alone, WCAG 1.4.1), the `relative` containment that stops that span
 * stretching the page by ~11,000px, and a constant height across states so a row never reflows.
 */
import type { ToolCallMessagePartComponent } from '@assistant-ui/react'

import type { ActivityArgs } from './runtime/convertMessage'
import { ToolActivityLine } from './ToolActivityLine'
import { UNRECOGNISED_STEP, rowState } from './ActivityGroup'

const ActivityRow: ToolCallMessagePartComponent = (part) => {
  // A tool call with no result carries its message's status, so this is "the reply is still
  // being written" — the same fact the group reads.
  const streaming = part.status?.type === 'running'
  const args = (part.args ?? {}) as Partial<ActivityArgs>
  // An absent or empty label renders the unrecognised-tool phrase — never an empty row, and never
  // the tool's own name.
  const label = args.label?.trim() || UNRECOGNISED_STEP
  return <ToolActivityLine label={label} state={rowState(args.state, streaming)} />
}

export default ActivityRow
