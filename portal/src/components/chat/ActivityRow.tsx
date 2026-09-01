/**
 * ONE ROW INSIDE AN ACTIVITY GROUP (R35b, R36's rendering half, R66).
 *
 * The row vocabulary is VERB + TARGET + STATE, and the row is allowed to read exactly two things:
 * the server's friendly label and the state. Nothing else is available to it — `convertMessage`
 * never copies `detail`, `args` or `result` onto the part, so R36's wall is upstream of this file
 * and this component could not leak a file path if it tried. That is deliberate: a promise at the
 * draw site is only as good as the next person editing the draw site.
 *
 * Every guarantee `ToolActivityLine` already carries comes through unchanged, because the row IS
 * `ToolActivityLine` — the reduced-motion gate, the sr-only "failed" text node (failure by shape
 * and text, never colour alone, WCAG 1.4.1), the `relative` containment that stops that sr-only
 * span anchoring to the document and stretching the page by ~11,000px, and a constant height
 * across states so a row does not reflow as it resolves.
 */
import type { ToolCallMessagePartComponent } from '@assistant-ui/react'

import type { ActivityArgs, ActivityState } from './runtime/convertMessage'
import { ToolActivityLine, type ToolActivityState } from './ToolActivityLine'
import { UNRECOGNISED_STEP } from './ActivityGroup'

function rowState(state: ActivityState | undefined): ToolActivityState {
  if (state === 'ok') return 'ok'
  if (state === 'failed') return 'failed'
  return 'started'
}

const ActivityRow: ToolCallMessagePartComponent = (part) => {
  const args = (part.args ?? {}) as Partial<ActivityArgs>
  // An absent or empty label renders the unrecognised-tool phrase — never an empty row, and never
  // the tool's own name.
  const label = args.label?.trim() || UNRECOGNISED_STEP
  return <ToolActivityLine label={label} state={rowState(args.state)} />
}

export default ActivityRow
