/**
 * Parse the `bial:build-brief` fence out of an assistant turn (003-U3).
 *
 * The build brief travels as a fenced block inside the model's own text rather than as a tool
 * call: the relay's wire contract is frozen at two frame types (`{"delta":{"text"}}` / `[DONE]`)
 * with no room for tool frames, so a sentinel block is what lets "the brief is ready" cross it
 * with zero protocol change — and it arrives in the same turn that decides readiness.
 * `backend/src/api/v1/claude/prompts.py` is what instructs the model to emit it.
 *
 * DEGRADATION IS THE POINT. This parser sits between the model and the ONLY build trigger on the
 * page. A model that emits a slightly-wrong fence is a routine event; a user with no way to build
 * is not a routine outcome. So anything fence-SHAPED yields a confirmable proposal — a malformed
 * one is marked `degraded` so the card can hedge its wording, never dropped. Only text with no
 * fence at all yields `none`, which renders as an ordinary bubble (the no-code-reply learning:
 * an assistant turn is never lost).
 *
 * Nothing here auto-fires a build: there is deliberately no variant meaning "start". The user
 * confirms every brief, which is what makes the degraded path's guesswork safe.
 */

/**
 * The fence info string. Mirrors `BUILD_BRIEF_FENCE_TAG` in the backend's protocol prompt —
 * change both or neither. Pinned by a test on each side.
 */
export const BUILD_BRIEF_FENCE_TAG = 'bial:build-brief'

/** A well-formed single fence. `text` is the prose around it (the fence itself is stripped). */
export interface BuildBriefFound {
  kind: 'brief'
  brief: string
  text: string
}

/**
 * Fence-shaped but not to contract — unterminated, duplicated, or empty. Carries the best
 * available brief so the card still renders with a working build action.
 */
export interface BuildBriefDegraded {
  kind: 'degraded'
  brief: string
  text: string
}

/** No fence — an ordinary assistant turn (a question, an answer, chitchat). */
export interface BuildBriefAbsent {
  kind: 'none'
}

export type BuildBriefResult = BuildBriefFound | BuildBriefDegraded | BuildBriefAbsent

/**
 * Matches an OPEN fence line: ``` + our tag, alone on its line.
 *
 * `[^\S\r\n]*$` is "horizontal whitespace only, then end of line" — a plain `\s*` would let the
 * tag be followed by a NEWLINE and arbitrary content on the info line, and anchoring the end is
 * what stops `bial:build-brief-v2` (a tag we do not speak) from matching as ours. Single-pass
 * character classes, no nesting — no backtracking (the ReDoS learning).
 */
const OPEN_FENCE = new RegExp(`^\`\`\`${BUILD_BRIEF_FENCE_TAG}[^\\S\\r\\n]*$`, 'm')
/** The closing fence: ``` alone on its line (trailing horizontal whitespace tolerated). */
const CLOSE_FENCE = /^```[^\S\r\n]*$/m

/** Collapse the 3+ newlines left behind by excising a block, and trim the ends. */
function tidy(text: string): string {
  return text.replace(/\n{3,}/g, '\n\n').trim()
}

/**
 * Extract the build brief from an assistant turn's text.
 *
 * @param text the assistant turn's full text (may be mid-stream — an unterminated fence is
 *   expected while streaming and degrades rather than throwing).
 */
export function parseBuildBrief(text: string): BuildBriefResult {
  if (!text) return { kind: 'none' }

  // `String.match` with a non-global regex yields the same match object as the regex's own
  // lookup, `index` included — and keeps the read left-to-right.
  const open = text.match(OPEN_FENCE)
  if (open?.index === undefined) return { kind: 'none' }

  const bodyStart = open.index + open[0].length + 1 // +1 for the newline ending the info line
  const rest = text.slice(bodyStart)
  const close = rest.match(CLOSE_FENCE)
  const before = text.slice(0, open.index)

  if (close?.index === undefined) {
    // Unterminated: the stream was cut, or the model omitted the closer. Everything after the
    // open fence is the brief — the user still gets a proposal to confirm.
    return { kind: 'degraded', brief: rest.trim(), text: tidy(before) }
  }

  const brief = rest.slice(0, close.index).trim()
  const after = rest.slice(close.index + close[0].length)
  const outside = tidy(`${before}\n${after}`)

  // A second fence means the model broke the one-block rule; an empty one means it emitted a
  // brief with no content. Both are honoured as a DEGRADED proposal (first fence wins) rather
  // than dropped — the user reads the brief on the card before confirming either way.
  if (!brief || OPEN_FENCE.test(after)) {
    return { kind: 'degraded', brief, text: outside }
  }
  return { kind: 'brief', brief, text: outside }
}
