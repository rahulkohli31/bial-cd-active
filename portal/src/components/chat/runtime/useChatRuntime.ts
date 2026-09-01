/**
 * THE RUNTIME — the single junction between the stream reader, the reload projection and every
 * rendered element.
 *
 * `useExternalStoreRuntime`, deliberately, and not `useLocalRuntime` or the AI-SDK runtime: those
 * two OWN the message array and mint ids. The reverted migration's lesson stands — the library is
 * a render model, and the hydrated server transcript is the truth for ordering, identity and
 * history. Everything this hook passes is read-only from the library's point of view.
 *
 * ══ EVERY CAPABILITY IS OFF BY OMISSION, EXCEPT TWO ══
 *
 * A capability in this library is not a setting you switch off. It is DERIVED from which callbacks
 * and adapters you hand over, which means the way to keep one off is to pass nothing — and the way
 * one wakes up by accident is somebody adding a callback to fix an unrelated problem. Verified
 * derivations, read out of the installed 0.15.17:
 *
 *   switchToBranch, delete        ← `setMessages`      (ONE prop, TWO capabilities)
 *   edit, reload, refetchThread   ← onEdit / onReload / onRefetchThread
 *   cancel                        ← onCancel                          ← WE PASS THIS
 *   speech, dictation, voice,
 *   attachments, feedback         ← adapters.*
 *   queue                         ← queue
 *   unstable_copy                 ← unstable_capabilities.copy (default true)
 *
 * `cancel` is the one capability this surface WANTS: registering `onCancel` is what puts R55's
 * relocated stop on the runtime. `unstable_copy` is passed explicitly even though `true` is already
 * the default, so the intent is legible and a future change of default shows up in a diff rather
 * than in production.
 *
 * THE WRITTEN LIST HAS TWO `true` ENTRIES. Anyone writing the exact-equality test from a shorter
 * sentence — "everything off except copy" — gets a red suite, and the tempting fix is to drop
 * `onCancel`, which silently deletes the stop path. `EXPECTED_CAPABILITIES` below is the list, the
 * test compares against it with `toEqual`, and this paragraph is why.
 *
 * ══ THE THIRD LIBRARY OPINION: `canSend` / `isSendDisabled` (R51a) ══
 *
 * R51a names three things the library forms a view about — whether a turn is running, whether a
 * message can be sent, whether an attachment is allowed. `isRunning` is answered HERE, by passing
 * it as a first-class field. Attachments are answered by registering no adapter. `canSend` is
 * answered by NOT USING THE LIBRARY'S SEND BUTTON AT ALL.
 *
 * That last one is not stylistic. `createActionButton` renders `<button disabled={props.disabled ||
 * !callback}>`, and `useComposerSend` returns no callback while `isRunning && !capabilities.queue`
 * — and `queue` is never registered. So every library Send button renders a HARD `disabled` for the
 * whole of every turn, which is exactly the focus-dropping bug R45 and R64 forbid. `isSendDisabled`
 * stays unwired because it gates a code path nothing executes — and that stays true only while
 * the composer's Send is ours.
 */
import { useMemo } from 'react'
import {
  useExternalStoreRuntime,
  type AppendMessage,
  type AssistantRuntime,
} from '@assistant-ui/react'

import type { ChatMessage } from '../../../utils/messageTypes'
import { assertUniqueIds, convertMessage } from './convertMessage'

/**
 * THE CAPABILITY LIST, written down (R51a).
 *
 * Fourteen keys, matching `RuntimeCapabilities` exactly. Two are `true`. A test compares
 * `runtime.thread.getState().capabilities` against this with `toEqual` — exact equality, never
 * `toMatchObject`, because `toMatchObject` passes when a capability we never listed wakes up,
 * which is the entire failure this guard exists to catch.
 */
export const EXPECTED_CAPABILITIES = {
  // ── the two we want ──
  /** R55. Registered by passing `onCancel`; dropping it deletes the stop path. */
  cancel: true,
  /** Explicit though it is the default, so a change of default is visible in a diff. */
  unstable_copy: true,

  // ── the twelve that stay off, by passing nothing ──
  switchToBranch: false,
  switchBranchDuringRun: false,
  edit: false,
  reload: false,
  refetchThread: false,
  delete: false,
  speech: false,
  dictation: false,
  voice: false,
  attachments: false,
  feedback: false,
  queue: false,
} as const

export interface ChatRuntimeOptions {
  /**
   * The transcript, server-owned. Both the live assembly and the reload projection produce this
   * same shape, which is what makes them render identically.
   */
  messages: readonly ChatMessage[]
  /**
   * Flows straight to `thread.isRunning`. ONE field replaces the several per-page booleans that
   * used to decide whether a spinner drew.
   *
   * Omitting it is not neutral: `thread.isRunning` then falls back to a last-message-status
   * heuristic over `messages`, which is a different question with a different answer.
   */
  isRunning: boolean
  /** Send. The library never owns this path — it hands us the composed message and stops. */
  onNew: (message: AppendMessage) => Promise<void>
  /** R55's relocated stop. Passing it is what registers `cancel`. */
  onCancel: () => Promise<void>
}

export function useChatRuntime({
  messages,
  isRunning,
  onNew,
  onCancel,
}: ChatRuntimeOptions): AssistantRuntime {
  // Fail loudly on a duplicate id BEFORE the runtime sees the array. Its own behaviour is to keep
  // the last occurrence and `console.warn`, which costs a whole turn and reports it nowhere
  // anyone is looking.
  //
  // Memoised on the array identity, not on a deep compare: the streaming path already produces a
  // new array per update (and a new object only for the message that changed — see convertMessage
  // trap 4), so array identity is exactly the right cache key and a deep compare would be paying
  // for a guarantee the caller already provides.
  useMemo(() => assertUniqueIds(messages), [messages])

  return useExternalStoreRuntime<ChatMessage>({
    messages,
    isRunning,
    onNew,
    onCancel,
    convertMessage,
    unstable_capabilities: { copy: true },

    // ── DELIBERATELY ABSENT, and each absence is a capability ──
    //
    // setMessages     — switches on BOTH `switchToBranch` and `delete`. The most likely accidental
    //                   addition on this list: it is what someone reaches for to "fix a rerender".
    // onEdit          — `edit`. No message editing on this surface.
    // onReload        — `reload`. No regenerate.
    // onRefetchThread — `refetchThread`.
    // queue           — `queue`. Its absence is ALSO why the library's Send is unusable; see the
    //                   docblock. Adding it would make that button work and would be the wrong fix.
    // adapters        — `speech`, `dictation`, `voice`, `attachments`, `feedback`. Attachments in
    //                   particular stay ours: the library renders a chip, it does not decide which
    //                   content is re-sent, which binaries are inlined, the cache-breakpoint
    //                   ceiling, or how fences are escaped (R51).
    // isSendDisabled  — see the docblock. It gates a code path nothing here executes.
    // unstable_enableToolInvocations — would run tool callbacks TWICE on top of our own step
    //                   dispatch. Its default is already `false`; naming it here is documentation,
    //                   not configuration.
  })
}
