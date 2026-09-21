/**
 * BIAL CHAT — a general assistant beside the app builder, and for now a screen with one state.
 *
 * A greeting, a line under it, and a composer that takes text and will not send. The gate is the
 * portal's OWN "you may not send yet" mechanism (`Composer`'s `gate`), which is what paints the
 * send control its pale ground and puts the reason under the box — rather than a disabled style
 * invented for this page. A demo that swallows a typed message without a word is worse than one
 * that says plainly what it is.
 *
 * THE COMPOSER IS THE LIBRARY'S, NOT A DRAWING OF ONE. `useChatRuntime` performs no I/O of its
 * own — every network call on the chat surface lives in that surface's callbacks — so the real
 * runtime with an empty transcript and two handlers that do nothing renders the genuine
 * `ComposerPrimitive` box, dropzone, chip list and attachment control with nothing behind it. The
 * composer suites mount this same configuration.
 *
 * NO TRANSCRIPT, NO HISTORY, NO BACKEND. What this looks like once a conversation starts — the
 * composer dropping to the bottom of the screen — is a separate design, not a hidden half of this
 * one.
 */
import { useEffect, useState } from 'react'
import { motion } from 'motion/react'

import Backdrop from '../components/assistant/Backdrop'
import {
  firstName,
  headlineParts,
  lastGreeting,
  pickGreeting,
  rememberGreeting,
} from '../components/assistant/greetings'
import Composer from '../components/chat/Composer'
import ChatRuntimeProvider from '../components/chat/runtime/ChatRuntimeProvider'
import { DURATION, LAYOUT_EASE } from '../lib/motion'
import { getStoredUser } from '../utils/auth'
import type { ChatMessage } from '../utils/messageTypes'

/** Module-level so the runtime is handed the same empty array on every render. */
const NO_MESSAGES: readonly ChatMessage[] = []

/**
 * Why Send waits. One line, because it is the only thing on this screen that is not the citizen's
 * own words, and because the pale send control already carries "not yet" — what it cannot carry
 * on its own is *when*.
 */
const GATE = { blocked: true, reason: 'Preview — sending switches on in a coming release.' }

/**
 * The three callbacks the runtime and the composer require, none of which can run: `gate` refuses
 * every send before any of them is reached. They exist so the real components mount, and they
 * resolve rather than throw so that a future wiring changes one file and not this contract.
 */
const doesNothing = () => Promise.resolve()

export default function AssistantPage() {
  const [urgent, setUrgent] = useState<string | null>(null)

  // PICKED ONCE, ON MOUNT, AND `useState` RATHER THAN `useMemo` IS THE WHOLE POINT. A greeting
  // chosen during render would change under any re-render — a keystroke in the composer is
  // enough — and a heading that rewrites itself while someone is typing under it reads as a
  // glitch. `useMemo` is a performance hint React is allowed to discard and recompute; a state
  // initialiser is the only hook that promises to run exactly once for the life of the mount.
  const [greeting] = useState(() => {
    const name = firstName(getStoredUser()?.display_name)
    return { name, ...pickGreeting({ hour: new Date().getHours(), name, exclude: lastGreeting() }) }
  })

  // Remembered after the pick rather than inside it, so choosing a greeting stays a pure function
  // of its arguments and the one thing here that can throw sits on its own.
  useEffect(() => rememberGreeting(greeting.headline), [greeting])

  const parts = headlineParts(greeting.headline, greeting.name)

  return (
    // `min-h-full`, NEVER `h-full`, AND NOTHING CLIPS HERE. A fixed height plus `overflow-hidden`
    // centred the stack in a box it could outgrow: at 1024x300 with a full composer the greeting
    // was cut off above and the gate note below, with no scrollbar to reach either, because the
    // clip was on this element rather than on the shell that scrolls. A minimum height fills the
    // screen when the content is short and grows past it when it is not, which leaves the shell's
    // own `overflow-y-auto` free to do its job. The backdrop clips itself.
    <div className="relative min-h-full bg-bial-bg font-manrope" data-testid="assistant-page">
      <Backdrop />

      <div className="relative flex min-h-full flex-col items-center justify-center px-6 py-12">
        {/* ONE MOTION ON THE WHOLE STACK, not a stagger across its three parts. `lib/motion.ts`
            allows two curves and nothing longer than 260ms; a three-step entrance runs past 400ms
            and puts this screen outside the vocabulary every other surface keeps to. */}
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: DURATION.layout, ease: LAYOUT_EASE }}
          className="w-full max-w-thread"
        >
          <h1
            data-testid="assistant-greeting"
            className="text-center text-[clamp(30px,4.2vw,44px)] font-extrabold leading-[1.14] tracking-[-1px] text-tertiary"
          >
            {parts.map((segment, index) =>
              segment.accent ? (
                // The one word in colour on the whole screen.
                <em key={`${index}-${segment.text}`} className="mr-[0.04em] text-primary">
                  {segment.text}
                </em>
              ) : (
                <span key={`${index}-${segment.text}`}>{segment.text}</span>
              ),
            )}
          </h1>

          <p className="mt-2.5 text-center text-[clamp(13px,1.1vw,15px)] leading-relaxed text-status-grey-fg wide:mt-3">
            {greeting.sub}
          </p>

          <div className="mt-6 wide:mt-[30px]">
            <ChatRuntimeProvider
              messages={NO_MESSAGES}
              isRunning={false}
              onNew={doesNothing}
              onCancel={doesNothing}
            >
              <Composer
                conversationId={null}
                placeholder="Message BIAL Chat"
                onSubmit={doesNothing}
                isRunning={false}
                gate={GATE}
                onUrgent={setUrgent}
                // No ground and no gutter of its own: the box sits directly on the platform's,
                // which is what the board draws.
                frameClassName="flex w-full flex-col gap-1.5"
                // `text-neutral` is 4.37:1 on this page's ground and fails AA. See the prop.
                noteClassName="text-status-grey-fg"
              />
            </ChatRuntimeProvider>
          </div>

          {/* MOUNTED ALWAYS, USUALLY EMPTY. A live region inserted together with its text is
              missed outright by several reader-and-browser combinations — `Announcer.tsx` records
              the three places this project already learned that — so the region has to be sitting
              in the accessibility tree before there is anything to put in it. A refused file is
              the only urgent thing this screen can produce, since nothing sends.

              `status.red-fg`, not `text-danger`: #EF4444 measures 3.40:1 on this page's ground and
              fails AA for body text, where #B91C1C measures 5.85:1. */}
          <p
            role="alert"
            data-testid="assistant-urgent"
            className={`text-xs text-status-red-fg ${urgent ? 'mt-1.5' : ''}`}
          >
            {urgent ?? ''}
          </p>
        </motion.div>
      </div>
    </div>
  )
}
