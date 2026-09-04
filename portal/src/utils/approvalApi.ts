/**
 * Typed client for the owner-facing approval flow (`POST /api/apps/:appId/withdraw`),
 * mirroring `projectApi.ts`: every call is `fn(args, deps = {})` forwarding `deps` to
 * `authFetch`, responses arrive as `unknown` and pass through a narrower that throws
 * `ApiError` on a structurally-invalid row — never cast, never `any`.
 *
 * THERE IS NO `submitForReview` HERE ANY MORE, and its absence is the point (R15a: one
 * route into the queue). The citizen-callable submit route was retired backend-side in
 * U8 — it was the only writer of the pending status that attached NO declaration, so a
 * queue item could arrive with nothing for an administrator to read. An app now enters
 * the queue exactly one way: the publish request itself routes it (`deployApi.startDeploy`
 * → `outcome: "routed_for_review"`). Re-adding a submit verb here would rebuild the second
 * differently-worded way in that requirement forbids; `approvalApi.test.ts` guards its
 * absence rather than having deleted the coverage.
 *
 * `withdraw` is the escape hatch that replaced re-submitting (P6): an owner pulls their
 * own PENDING submission back to draft. It sends NO body — the server knows which
 * submission is pending — and its 409 carries self-describing server copy, so the control
 * renders `err.message` directly without client-side string matching.
 *
 * IT IS THE ONLY VERB LEFT HERE, and that is the second point. There was an app-scoped
 * `getApprovalStatus` beside it, the typed client for `GET /apps/:id/status`, written for the
 * approval card at the foot of the chat; the canvas's `Removals` board took that card out and
 * nothing reached the getter afterwards. The publish and review surfaces read the lifecycle off
 * the PROJECT-scoped deploy status instead (`deployApi`), so both share one poll lifetime and
 * cannot end up telling the citizen two different things — a second, app-scoped poll re-added
 * here is exactly what would break that. The SERVER route is untouched; `approvalApi.test.ts`
 * guards the getter's absence rather than having deleted the coverage. */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import type { AppStatus, AuthFetchDeps } from './projectApi'

/** What a successful withdrawal left behind (POST /apps/:id/withdraw): the app, back at
 *  draft. The queue item is REMOVED, not replaced — an administrator mid-review sees it
 *  disappear rather than change underneath them. */
export interface WithdrawResult {
  appId: string
  status: AppStatus
}

function toAppStatus(value: unknown): AppStatus {
  if (
    value === 'draft' ||
    value === 'pending' ||
    value === 'approved' ||
    value === 'rejected' ||
    value === 'disabled'
  ) {
    return value
  }
  // Unknown variants are dropped HERE, at the boundary, so the control's
  // `assertNever` switch stays unreached in practice (fail-first).
  throw new ApiError('The server returned an app status we could not read.', 500)
}

function toWithdrawResult(value: unknown): WithdrawResult {
  if (!isRecord(value) || typeof value.appId !== 'string' || value.appId === '') {
    throw new ApiError('The server returned an app we could not read.', 500)
  }
  return { appId: value.appId, status: toAppStatus(value.status) }
}

/**
 * Pull the owner's own PENDING submission back out of the queue (P6) — no body; the
 * server knows which submission is pending. Clears the pin, the declaration and the
 * lineage, and leaves the app at `draft`; the approved pin and the immutable submission
 * blob survive. A 409 means it was not pending any more (an administrator got there
 * first), and carries the server's own copy.
 */
export async function withdrawSubmission(
  appId: string,
  deps: AuthFetchDeps = {},
): Promise<WithdrawResult> {
  const res = await authFetch(
    `/api/apps/${encodeURIComponent(appId)}/withdraw`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to withdraw the submission')
  return toWithdrawResult(await res.json())
}
