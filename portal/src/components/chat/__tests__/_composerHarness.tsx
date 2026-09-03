/**
 * A COMPOSER, UNDER A RUNTIME — the harness every composer suite mounts through (plan 002, U5).
 *
 * The composer is the library's box now, and every library composer primitive resolves against
 * `useAui()`. So a suite that renders `<Composer/>` bare gets "You are using a component or hook
 * that requires an AuiProvider" rather than a composer — which is not a testing inconvenience, it
 * is the shape of the thing under test: on both real screens the composer sits inside a provider,
 * and a test that could render it without one would be testing something the product does not
 * mount.
 *
 * THE RUNTIME HERE IS THE REAL ONE, not a double. `ChatRuntimeProvider` is what the conversation
 * surface mounts, with the same adapter and the same capability derivation, so a suite that passes
 * here is exercising the composer the citizen gets.
 */
import type { ReactNode } from 'react'
import { vi } from 'vitest'
import ChatRuntimeProvider from '../runtime/ChatRuntimeProvider'

export function ComposerHarness({ children }: { children: ReactNode }) {
  return (
    <ChatRuntimeProvider
      messages={[]}
      isRunning={false}
      onNew={vi.fn().mockResolvedValue(undefined)}
      onCancel={vi.fn().mockResolvedValue(undefined)}
    >
      {children}
    </ChatRuntimeProvider>
  )
}
