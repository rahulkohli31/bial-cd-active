/**
 * Reading the observation beacons a render actually sent (U4).
 *
 * The page suites that cover the marks run the REAL `utils/observe` module — its
 * once-per-project-id-per-page-load guard IS what they are about, and a mocked module would
 * prove only that a function was called. So they replace the TRANSPORT instead, and this is the
 * one place that knows what a beacon looks like on the wire.
 *
 * Consequence for any suite using this: module state is per page load and is never reset, so each
 * test must use its OWN project id — a shared one would let one test's mark silence the next.
 */
import type { Mock } from 'vitest'
import type { authFetch } from '../../utils/api'

/**
 * The observation bodies posted through a mocked `authFetch`, in order.
 *
 * PARAMETERIZED on the real `authFetch`, because a bare `Mock` leaves its args `any` — and an
 * `as RequestInit` cast applied to an `any` looks like verification while checking nothing, and
 * would survive the signature it claims to track being changed underneath it.
 */
export function beaconsFrom(mocked: Mock<typeof authFetch>): unknown[] {
  return mocked.mock.calls
    .filter(([url]) => url === '/api/observations')
    .map(([, opts]) => JSON.parse(String(opts?.body)))
}
