/**
 * THE BROWSER'S "THIS CHAT IS GETTING LONG" WARNING.
 *
 * ══ IT WARNS. IT NEVER REFUSES. ══
 *
 * The hard boundary is the SERVER's — `enforce_context_limit` refuses the turn at the route
 * with a sentence of its own, before anything is persisted. That is deliberate and it is the
 * lesson of what this file replaces: the two-page portal enforced the whole guardrail in the
 * browser, so when `ChatPage.tsx` was deleted the boundary went with it, an administrator was
 * left setting a number nothing read, and a citizen's first news of the limit was a failed turn
 * with no reason. A guard only the client holds is not a guard.
 *
 * So this file's job is smaller and honest: warn EARLY ENOUGH that the citizen can finish their
 * thought and start a new chat, rather than being stopped mid-sentence.
 *
 * ══ THE ESTIMATE IS A FLOOR, AND THE DIRECTION MATTERS ══
 *
 * The browser sees the RENDERED transcript — prose and attachments. The server measures what
 * actually goes on the wire, which also includes every tool call and every tool result a Build
 * turn generated. Those never reach the projection, so this number is a LOWER BOUND on the
 * server's.
 *
 * The consequence, stated rather than glossed: in a Build chat with heavy tool traffic the
 * warning can arrive later than it ideally would. It cannot arrive too late to matter, because
 * the server's refusal is the thing that actually protects the conversation and that one is
 * never late. What this must never do is the opposite — claim room in a chat the server would
 * refuse — which is why it carries the same `SYSTEM_PROMPT_RESERVE` the server holds back, and
 * why the two files spell the same four-characters-to-the-token ratio.
 *
 * Every constant below is the twin of one in `backend/src/services/usage/` — `context_window.py`
 * for the estimate's ratios, `limits.py` for the window numbers.
 * They are two readings of one scale; change one and change the other.
 */
import { getStoredUser } from './auth'
import type { ProfileLimits } from './auth'
import type { ChatMessage } from './messageTypes'

/** Twin of `context_window.CHARS_PER_TOKEN`. */
export const CHARS_PER_TOKEN = 4

/**
 * Twin of `context_window.NOMINAL_BINARY_TOKENS`. An image or PDF is worth roughly a thousand
 * tokens however many megabytes it is, so it is charged flat. Its byte length is the wrong
 * number by orders of magnitude.
 */
export const NOMINAL_BINARY_TOKENS = 1_600

/**
 * Twin of `limits.SYSTEM_PROMPT_RESERVE` — room for the per-run system prompt, which
 * neither side can see from here. Carried in the browser's number too so the warning cannot sit
 * further from the wall than the refusal does.
 */
export const SYSTEM_PROMPT_RESERVE = 8_000

/** Twins of `limits.DEFAULT_CONTEXT_SOFT` / `DEFAULT_CONTEXT_HARD`, used only when a session
 *  predates the profile carrying them. */
export const DEFAULT_CONTEXT_SOFT = 150_000
export const DEFAULT_CONTEXT_HARD = 200_000

// `Number.isInteger` is typed `(x: unknown) => boolean` rather than a predicate, so it does not
// narrow. This wraps the identical runtime check in a real one.
function isPositiveInt(n: unknown): n is number {
  return typeof n === 'number' && Number.isInteger(n) && n > 0
}

/**
 * The signed-in user's effective thresholds. The login/refresh profile carries the
 * server-resolved `limits`; the constants above stand in when a session predates them.
 *
 * The `soft < hard` clamp mirrors the server's `effective_context` defensively — a warning that
 * sat AT the wall would fire for the first time in the same breath as the refusal, which is the
 * one moment it is no use.
 */
export function getContextLimits(): { soft: number; hard: number } {
  const limits: Partial<ProfileLimits> = getStoredUser()?.limits || {}
  const hard = isPositiveInt(limits.contextHardLimit) ? limits.contextHardLimit : DEFAULT_CONTEXT_HARD
  let soft = isPositiveInt(limits.contextSoftLimit) ? limits.contextSoftLimit : DEFAULT_CONTEXT_SOFT
  if (soft >= hard) soft = Math.max(1, hard - 1)
  return { soft, hard }
}

/**
 * What this conversation is worth, in tokens, as far as the browser can see.
 *
 * EVERY attachment counts on EVERY turn, which is a real change from the estimator this
 * replaces. That one charged image and PDF parts only in the newest message, because the old
 * relay sent binaries only for the newest turn. The turn engine rehydrates every stored
 * attachment in the history on every turn (`load_history`'s rehydrator) — Foundry has no Files
 * API, so the bytes go up again each time. Charging them once would under-count a
 * picture-heavy chat by however many pictures it holds.
 *
 * Office attachments are counted by their extracted TEXT, not the nominal: that text is sticky
 * prose on the wire, and a 200 KB spreadsheet extraction is ~50k tokens rather than 1,600.
 */
export function estimateConversationTokens(messages: readonly ChatMessage[]): number {
  let tokens = 0
  for (const message of messages) {
    for (const part of message?.parts || []) {
      if (part?.type === 'text') {
        tokens += Math.ceil((part.text || '').length / CHARS_PER_TOKEN)
      } else if (part?.type === 'file' && part.kind === 'office') {
        tokens += Math.ceil((part.text || '').length / CHARS_PER_TOKEN)
      } else if (part?.type === 'file') {
        tokens += NOMINAL_BINARY_TOKENS
      }
      // Everything else — plan cards, steps, build banners — is chrome the browser draws, not
      // content the model is sent. The server's own measurement never sees them either.
    }
  }
  return tokens + SYSTEM_PROMPT_RESERVE
}

export interface ContextState {
  /** The browser's floor estimate, reserve included. */
  estimate: number
  soft: number
  hard: number
  /** Past the warn threshold — the one thing that drives any UI. */
  gettingLong: boolean
  /** The line shown when it is. Null the rest of the time, which is nearly always. */
  message: string | null
}

/**
 * Silent until it is useful, then one sentence — the same discipline `composerCap` follows.
 *
 * The wording names the action rather than the condition, because "start a new chat" is the
 * only thing the reader can do about it and nothing else here is theirs to act on. It says
 * their work survives for the same reason the server's refusal does: the reason someone
 * hesitates to start a new chat is the fear that the app goes with the conversation.
 */
export function contextState(messages: readonly ChatMessage[]): ContextState {
  const { soft, hard } = getContextLimits()
  const estimate = estimateConversationTokens(messages)
  const gettingLong = estimate >= soft
  return {
    estimate,
    soft,
    hard,
    gettingLong,
    message: gettingLong
      ? 'This chat is getting long. Start a new chat soon to keep things quick — your app and everything you have built stays exactly as it is.'
      : null,
  }
}
