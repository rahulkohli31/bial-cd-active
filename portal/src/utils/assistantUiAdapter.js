/**
 * Bridges assistant-ui's ChatModelAdapter interface to this app's existing
 * /api/claude SSE endpoint (fetchClaudeStream, from useClaudeAPI.js) — the
 * SEPARATE, unmodified backend integration this app already has. This file
 * only translates between assistant-ui's message/streaming shape and the
 * existing REST/SSE contract; no backend or fetchClaudeStream code changes.
 *
 * Text-only (Phase 1): non-text content parts (image/file/tool-call) are
 * dropped by contentToText — attachment support is a later phase.
 *
 * `conversationId` is REQUIRED by POST /v1/claude under project-first (a 400
 * otherwise) — the server uses it to fold the project's description (and,
 * for a builder chat, the current app code) into the system prompt.
 */
import { fetchClaudeStream } from '../hooks/useClaudeAPI'
import { describeSaveFailure, isConversationGone } from './chatErrors'
import { notifyUsageChanged } from './usage'

function contentToText(content) {
  return content
    .filter((p) => p.type === 'text')
    .map((p) => p.text)
    .join('')
}

// Mirrors the build-suggestion heuristic from the pre-migration hand-rolled
// fireMessage: only after the conversation has some depth, and either it's
// gone long enough or the assistant itself signals readiness.
function shouldSuggestBuild(priorMessages, finalText) {
  const userCount = priorMessages.filter((m) => m.role === 'user').length
  const totalCount = priorMessages.length + 1 // + the assistant reply just produced
  return (
    userCount >= 3 &&
    (totalCount >= 6 ||
      /ready to build|shall we proceed|want me to create|build this for you|sounds like a plan/i.test(finalText))
  )
}

/**
 * Factory: build a ChatModelAdapter bound to one conversation's persistence
 * context. Each chat page builds its own instance via useMemo (different
 * system prompt, different chatId/projectId refs).
 *
 * @param {object} deps
 * @param {string} deps.systemPrompt
 * @param {() => string | null} deps.getChatId - current conversation id (ref read, always fresh)
 * @param {() => string | null} deps.getProjectId - current project id (ref read; folded into every appendMessage header, per project-first)
 * @param {(id, message, header) => Promise<void>} deps.appendMessage
 * @param {(text: string) => string} deps.deriveTitle
 * @param {() => void} deps.onAuthFailed - clearSession + navigate('/login'), same as today
 * @param {(id) => boolean} deps.isConversationStillActive - the activeChatIdRef guard, reused
 * @param {(message: string) => void} deps.onError - surfaces a message via the EXISTING red banner (not Thread's built-in error UI)
 * @param {() => void} deps.onConversationGone - navigate away (a 404 means the project/chat was deleted elsewhere)
 * @param {() => boolean} deps.ctxLevelFull - the existing hard-block context-length guardrail
 * @param {(chatId) => void} [deps.dropTransientQuery] - rewrites away the ?projectId=&kind= query once the row exists
 * @param {() => void} [deps.refreshHistory] - re-fetch the sidebar list after a successful persist
 * @param {(assistantText: string, shouldSuggestBuild: boolean) => void} [deps.onAssistantTurnComplete] - the build-suggestion trigger stays page-owned (drives page-local modal state + the "already fired" ref), this just supplies the computed signal
 * @param {(chatId: string) => void} [deps.onRunStart] - fires once a run is actually going to attempt a send (after the ctxLevelFull guard), so the page can gate its sidebar's per-chat delete button for the turn's lifetime
 * @param {(chatId: string) => void} [deps.onRunEnd] - fires on every exit path (success, error, or cancel) so the delete-gate above always clears
 */
export function createClaudeChatModelAdapter({
  systemPrompt,
  getChatId,
  getProjectId,
  appendMessage,
  deriveTitle,
  onAuthFailed,
  isConversationStillActive,
  onError,
  onConversationGone,
  ctxLevelFull,
  dropTransientQuery,
  refreshHistory,
  onAssistantTurnComplete,
  onRunStart,
  onRunEnd,
}) {
  // Guards against double-persisting the same user turn if run() is ever
  // re-invoked for the same message id (e.g. a future regenerate feature) —
  // today's hand-rolled fireMessage persists exactly once per send, so this
  // preserves that invariant under assistant-ui's own message-append semantics.
  const persistedUserIds = new Set()

  return {
    async *run({ messages, abortSignal }) {
      const chatId = getChatId()
      if (!chatId) return

      onRunStart?.(chatId)
      try {
        if (ctxLevelFull()) {
          onError('This conversation has reached its maximum length. Start a new chat to keep going.')
          return
        }

        const projectId = getProjectId()
        const lastUser = messages[messages.length - 1]
        const userText = contentToText(lastUser.content)
        // `messages` (no separate system-role entry — the system prompt travels
        // via the request body, not this array) already includes the just-sent
        // user turn, so its own 0-based index — and thus its persistence seq —
        // is messages.length - 1; a length of 1 means it's the first turn ever.
        const userSeq = messages.length - 1
        const isFirstTurn = messages.length === 1

        if (!persistedUserIds.has(lastUser.id)) {
          persistedUserIds.add(lastUser.id)
          try {
            await appendMessage(
              chatId,
              { role: 'user', parts: [{ type: 'text', text: userText }], seq: userSeq },
              isFirstTurn ? { title: deriveTitle(userText), projectId } : { projectId },
            )
          } catch (err) {
            // If the user has already navigated to a different chat by the
            // time this rejects, onError/onConversationGone would otherwise
            // apply a stale-chat failure to whatever chat is now on screen —
            // a red banner (or a forced navigation) for an error that has
            // nothing to do with the currently active conversation.
            if (isConversationStillActive(chatId)) {
              onError(describeSaveFailure(err))
              if (isConversationGone(err)) onConversationGone?.()
            }
            return
          }
          dropTransientQuery?.(chatId)
          refreshHistory?.()
        }

        const apiMessages = messages.map((m) => ({
          role: m.role,
          content: contentToText(m.content),
        }))

        // Bridge fetchClaudeStream's push-based onChunk callback into a
        // pull-based async generator (assistant-ui requires run() to YIELD,
        // not return-via-callback). onChunk's `fullText` is ALREADY the
        // cumulative running text on every call, so each yield is just the
        // latest snapshot — no re-accumulation needed, sidestepping a whole
        // class of delta-summing bugs.
        let wake = null
        const pending = []
        let done = false
        let thrown = null

        const streamDone = fetchClaudeStream({
          body: {
            model: 'claude-opus-4-7',
            max_tokens: 64000,
            system: systemPrompt,
            messages: apiMessages,
            conversationId: chatId,
          },
          onChunk: (_delta, fullText) => {
            pending.push(fullText)
            wake?.()
          },
          signal: abortSignal,
        })
          .catch((err) => {
            thrown = err
          })
          .finally(() => {
            done = true
            wake?.()
          })

        let finalText = ''
        try {
          while (true) {
            while (pending.length === 0 && !done) {
              await new Promise((resolve) => {
                wake = resolve
              })
            }
            while (pending.length > 0) {
              finalText = pending.shift()
              yield { content: [{ type: 'text', text: finalText }] }
            }
            if (done) break
          }
        } finally {
          // Ensure fetchClaudeStream's own promise (401-retry/429/suspended/
          // network handling) is fully settled before persisting below —
          // otherwise a race could persist an assistant turn despite the
          // underlying request having actually errored.
          await streamDone
        }

        // A genuine user Cancel shares the same abortSignal plumbing as
        // fetchClaudeStream's pre-existing logout/unmount handling, which by
        // design resolves normally with whatever text had streamed so far
        // (see useClaudeAPI.js's "Aborting mid-stream is expected — return
        // what we have") rather than throwing — its original callers just
        // discarded that text on unmount. This adapter is a new caller that
        // DOES persist `finalText`, so without this check a cancelled reply
        // would silently get saved as a normal completed turn (usage-notified
        // and build-suggestion-evaluated too) even though assistant-ui's own
        // UI already marks the message incomplete/cancelled. Bail out before
        // any of that so a cancel actually discards the in-flight reply.
        if (abortSignal.aborted) return

        if (thrown) {
          if (thrown.code === 'AUTH_REFRESH_FAILED') {
            onAuthFailed()
            return
          }
          // fetchClaudeStream already builds the exact right user-facing text
          // (daily-limit 429, suspended 403, network errors, etc.) — just
          // relay it. Never rethrow: an uncaught error here would trigger
          // assistant-ui's own error UI, which this app uses its existing red
          // banner instead of.
          onError(thrown.message || 'Something went wrong. Please try again.')
          return
        }

        // A turn completed (even if streamed zero text) → server-side usage
        // advanced; nudge the navbar badge, mirroring useClaudeAPI's sendMessage.
        notifyUsageChanged()

        if (finalText && isConversationStillActive(chatId)) {
          try {
            await appendMessage(
              chatId,
              { role: 'assistant', parts: [{ type: 'text', text: finalText }], seq: userSeq + 1 },
              {},
            )
            refreshHistory?.()
          } catch {
            onError('Your reply could not be saved.')
          }
          onAssistantTurnComplete?.(finalText, shouldSuggestBuild(messages, finalText))
        }
      } finally {
        onRunEnd?.(chatId)
      }
    },
  }
}
