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
 * is not a routine outcome. So anything fence-SHAPED that carries a brief yields a confirmable
 * proposal — a malformed one is marked `degraded` so the card can hedge its wording, never
 * dropped. Text with no fence — or a fence with nothing IN it — yields `none` and renders as an
 * ordinary bubble (the no-code-reply learning: an assistant turn is never lost).
 *
 * `degraded` IS BUILDABLE, and that is what makes the empty case `none` rather than `degraded`:
 * the card arms itself on kind alone, so a degraded-but-empty brief renders a working Build button
 * that starts a real build on an empty prompt. "Fence-shaped" earns a hedge; it does not earn a
 * build out of nothing.
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
 * Fence-shaped but not to contract — unterminated, duplicated, or structurally unresolvable.
 * Carries the best available brief (never an empty one) so the card still renders with a working
 * build action, hedged.
 */
export interface BuildBriefDegraded {
  kind: 'degraded'
  brief: string
  text: string
}

/**
 * Nothing to build: an ordinary assistant turn (a question, an answer, chitchat), or a fence that
 * turned out to be empty. The turn still renders — the caller falls back to the raw parts — so a
 * malfunctioning model costs the user a card, never the reply.
 */
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

// The three line shapes the scan below distinguishes. Applied per-LINE (no /m), so `$` is the end
// of the line and `[^\S\n]` absorbs a trailing `\r` from CRLF text along with spaces and tabs.
/** A bare ``` — closes a nested block, or closes ours. */
const BARE_FENCE = /^```[^\S\n]*$/
/** ``` + an info string (```json, ```ts) — unambiguously OPENS a nested block, never closes one. */
const NESTED_OPEN = /^```[^\S\n]*\S/
/** Any fence line at all, used to spot a remainder that should have been part of the brief. */
const ANY_FENCE = /^```/m

/** Collapse the 3+ newlines left behind by excising a block, and trim the ends. */
function tidy(text: string): string {
  return text.replace(/\n{3,}/g, '\n\n').trim()
}

/**
 * Where the brief's own closing fence is, or null if it never comes.
 *
 * DEPTH IS WHY THIS IS A SCAN AND NOT A `match`. Closing at the first ``` truncated every brief
 * that contained a fenced block — a schema sketch, a sample row, a config snippet — because the
 * first ``` it met was the nested block's CLOSER, not the brief's. The brief was cut there and
 * still reported as clean, so the card rendered hedge-free, the user confirmed, and the build ran
 * on half an instruction. Nothing was visible on either side.
 *
 * An info-string fence (```json) can only open, so it is safe to count; a BARE ``` is genuinely
 * ambiguous (it may open an info-less block or close ours) and is read as ours at depth 0 — the
 * common case. `parseBuildBrief` catches the other reading by checking what is left over.
 */
function findCloser(rest: string): { briefEnd: number; afterStart: number } | null {
  let depth = 0
  let offset = 0
  for (const line of rest.split('\n')) {
    const lineEnd = offset + line.length
    if (BARE_FENCE.test(line)) {
      if (depth === 0) return { briefEnd: offset, afterStart: lineEnd }
      depth -= 1
    } else if (NESTED_OPEN.test(line)) {
      depth += 1
    }
    offset = lineEnd + 1 // +1 for the \n `split` consumed
  }
  return null
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
  const before = text.slice(0, open.index)
  const closer = findCloser(rest)

  if (closer === null) {
    // Unterminated: the stream was cut, or the model omitted the closer. Everything after the
    // open fence is the brief — the user still gets a proposal to confirm.
    const streamed = rest.trim()
    return streamed ? { kind: 'degraded', brief: streamed, text: tidy(before) } : { kind: 'none' }
  }

  const brief = rest.slice(0, closer.briefEnd).trim()
  const after = rest.slice(closer.afterStart)
  const outside = tidy(`${before}\n${after}`)

  // An empty fence has nothing to build. It is NOT a degraded proposal: the card arms on kind, so
  // that would render a working Build button that starts a real build on an empty prompt.
  if (!brief) return { kind: 'none' }

  // A fence still standing in the LEFTOVER means the structure did not resolve: either the model
  // broke the one-block rule (a second brief), or the ``` taken as the closer actually opened an
  // info-less block and the brief is truncated at it. Both readings are valid markdown and neither
  // can be told from the other — so hedge rather than present a possibly-severed brief as clean.
  // The user reads the brief on the card before confirming, which is what makes the guess safe.
  if (ANY_FENCE.test(after)) return { kind: 'degraded', brief, text: outside }
  return { kind: 'brief', brief, text: outside }
}
